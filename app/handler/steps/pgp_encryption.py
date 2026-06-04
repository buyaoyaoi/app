from typing import Protocol, Optional, Tuple, runtime_checkable
from email.message import Message
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email import encoders
from email.encoders import encode_noop
from io import BytesIO

import sentry_sdk
from sl_pgp import PgpContext

from app import config, pgp_utils
from app.email import headers
from app.email_utils import copy, delete_all_headers_except, add_header
from app.handler.steps import EmailProcessingStep, EmailProcessingContext
from app.log import LOG
from app.message_utils import message_to_bytes
from app.pgp_utils import PGPException, sign_data, sign_data_with_pgpy, create_pgp_context


@runtime_checkable
class PgpEncryptionStrategy(Protocol):
    def encrypt(
        self,
        msg: Message,
        pgp_fingerprint: str,
        public_key: str,
        can_sign: bool,
        ctx: Optional[PgpContext],
    ) -> Message:
        ...

    def get_name(self) -> str:
        ...


class GnupgEncryptionStrategy:
    name = "gnupg"

    def get_name(self) -> str:
        return self.name

    def encrypt(
        self,
        msg: Message,
        pgp_fingerprint: str,
        public_key: str,
        can_sign: bool,
        ctx: Optional[PgpContext],
    ) -> Message:
        if ctx is None:
            ctx = create_pgp_context()

        clone_msg = copy(msg)

        msg_bytes = message_to_bytes(clone_msg)
        encrypted_data = pgp_utils.encrypt_file(
            BytesIO(msg_bytes), pgp_fingerprint, ctx
        )
        return self._build_encrypted_message(msg, encrypted_data, can_sign, ctx)


    def _build_encrypted_message(
        self,
        orig_msg: Message,
        encrypted_data: bytes,
        can_sign: bool,
        ctx: PgpContext,
    ) -> Message:
        msg = MIMEMultipart("encrypted", protocol="application/pgp-encrypted")

        clone_msg = copy(orig_msg)
        for i in reversed(range(len(clone_msg._headers))):
            header_name = clone_msg._headers[i][0].lower()
            if header_name.lower() not in headers.MIME_HEADERS:
                msg[header_name] = clone_msg._headers[i][1]

        delete_all_headers_except(clone_msg, headers.MIME_HEADERS)

        if clone_msg[headers.CONTENT_TYPE] is None:
            LOG.d("Content-Type missing")
            clone_msg[headers.CONTENT_TYPE] = "text/plain"

        if clone_msg[headers.MIME_VERSION] is None:
            LOG.d("Mime-Version missing")
            clone_msg[headers.MIME_VERSION] = "1.0"

        first = MIMEApplication(
            _subtype="pgp-encrypted", _encoder=encoders.encode_7or8bit, _data=""
        )
        first.set_payload("Version: 1")
        msg.attach(first)

        if can_sign and config.PGP_SENDER_PRIVATE_KEY:
            LOG.d("Sign msg")
            clone_msg = sign_msg(clone_msg, ctx)

        second = MIMEApplication(
            "octet-stream", _encoder=encoders.encode_7or8bit, name="encrypted.asc"
        )
        second.add_header("Content-Disposition", 'inline; filename="encrypted.asc"')
        second.set_payload(encrypted_data)
        msg.attach(second)

        return msg


class PgpyEncryptionStrategy:
    name = "pgpy"

    def get_name(self) -> str:
        return self.name

    def encrypt(
        self,
        msg: Message,
        pgp_fingerprint: str,
        public_key: str,
        can_sign: bool,
        ctx: Optional[PgpContext],
    ) -> Message:
        if ctx is None:
            ctx = create_pgp_context()

        clone_msg = copy(msg)
        msg_bytes = message_to_bytes(clone_msg)

        from init_app import load_pgp_public_keys
        load_public_key_and_check(public_key, ctx)

        encrypted = pgp_utils.encrypt_file_with_pgpy(msg_bytes, public_key, ctx)
        return self._build_encrypted_message(msg, str(encrypted), can_sign, ctx)

    def _build_encrypted_message(
        self,
        orig_msg: Message,
        encrypted_data: str,
        can_sign: bool,
        ctx: PgpContext,
    ) -> Message:
        msg = MIMEMultipart("encrypted", protocol="application/pgp-encrypted")

        clone_msg = copy(orig_msg)
        for i in reversed(range(len(clone_msg._headers))):
            header_name = clone_msg._headers[i][0].lower()
            if header_name.lower() not in headers.MIME_HEADERS:
                msg[header_name] = clone_msg._headers[i][1]

        delete_all_headers_except(clone_msg, headers.MIME_HEADERS)

        if clone_msg[headers.CONTENT_TYPE] is None:
            LOG.d("Content-Type missing")
            clone_msg[headers.CONTENT_TYPE] = "text/plain"

        if clone_msg[headers.MIME_VERSION] is None:
            LOG.d("Mime-Version missing")
            clone_msg[headers.MIME_VERSION] = "1.0"

        first = MIMEApplication(
            _subtype="pgp-encrypted", _encoder=encoders.encode_7or8bit, _data=""
        )
        first.set_payload("Version: 1")
        msg.attach(first)

        if can_sign and config.PGP_SENDER_PRIVATE_KEY:
            LOG.d("Sign msg")
            clone_msg = sign_msg(clone_msg, ctx)

        second = MIMEApplication(
            "octet-stream", _encoder=encoders.encode_7or8bit, name="encrypted.asc"
        )
        second.add_header("Content-Disposition", 'inline; filename="encrypted.asc"')
        second.set_payload(encrypted_data)
        msg.attach(second)

        return msg


class PgpEncryptionFallbackChain:
    strategies: List[PgpEncryptionStrategy]

    def __init__(self, strategies: List[PgpEncryptionStrategy]):
        self.strategies = strategies

    def encrypt(
        self,
        msg: Message,
        pgp_fingerprint: str,
        public_key: str,
        can_sign: bool,
        ctx: Optional[PgpContext],
    ) -> Tuple[Message, Optional[str]]:
        last_error = None
        for strategy in self.strategies:
            try:
                result = strategy.encrypt(msg, pgp_fingerprint, public_key, can_sign, ctx)
                LOG.d(f"PGP encryption successful with strategy: {strategy.get_name()}")
                return result, None
            except PGPException as e:
                LOG.w(f"PGP encryption failed with strategy {strategy.get_name()}: {e}")
                last_error = str(e)
                continue
            except Exception as e:
                LOG.w(f"Unexpected error with strategy {strategy.get_name()}: {e}")
                last_error = str(e)
                continue

        return None, last_error


from typing import List
from app.pgp_utils import load_public_key_and_check


@sentry_sdk.trace
def sign_msg(msg: Message, ctx: PgpContext) -> Message:
    container = MIMEMultipart(
        "signed", protocol="application/pgp-signature", micalg="pgp-sha256"
    )
    container.attach(msg)

    signature = MIMEApplication(
        _subtype="pgp-signature", name="signature.asc", _data="", _encoder=encode_noop
    )
    signature.add_header("Content-Disposition", 'attachment; filename="signature.asc"')

    try:
        payload = sign_data(message_to_bytes(msg).replace(b"\n", b"\r\n"), ctx)

        if not payload:
            raise PGPException("Empty signature by gnupg")

        signature.set_payload(payload)
    except Exception:
        LOG.e("Cannot sign, try using pgpy")
        payload = sign_data_with_pgpy(message_to_bytes(msg).replace(b"\n", b"\r\n"))

        if not payload:
            raise PGPException("Empty signature by pgpy")

        signature.set_payload(payload)

    container.attach(signature)

    return container


class PgpEncryptionStep:
    name = "pgp_encryption"

    def __init__(self):
        self.fallback_chain = PgpEncryptionFallbackChain([
            GnupgEncryptionStrategy(),
            PgpyEncryptionStrategy(),
        ])

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        mailbox = context.mailbox
        user = context.user
        alias = context.alias

        if not (mailbox.pgp_enabled() and user.is_premium() and not alias.disable_pgp):
            return None

        LOG.d("Encrypt message using mailbox %s", mailbox)

        try:
            ctx = create_pgp_context()
            encrypted_msg, error = self.fallback_chain.encrypt(
                context.msg,
                mailbox.pgp_finger_print,
                mailbox.pgp_public_key,
                can_sign=True,
                ctx=ctx,
            )
            if encrypted_msg:
                context.msg = encrypted_msg
            else:
                LOG.w(
                    "Cannot encrypt message %s -> %s. %s %s",
                    context.contact,
                    alias,
                    mailbox,
                    user,
                )
                context.msg = add_header(
                    context.msg,
                    f"""PGP encryption fails with {mailbox.email}'s PGP key""",
                )
        except PGPException:
            LOG.w(
                "Cannot encrypt message %s -> %s. %s %s",
                context.contact,
                alias,
                mailbox,
                user,
            )
            context.msg = add_header(
                context.msg,
                f"""PGP encryption fails with {mailbox.email}'s PGP key""",
            )

        return None

    def can_skip(self, context: EmailProcessingContext) -> bool:
        if context.mailbox is None:
            return True
        mailbox = context.mailbox
        user = context.user
        alias = context.alias
        return not (mailbox.pgp_enabled() and user.is_premium() and not alias.disable_pgp)