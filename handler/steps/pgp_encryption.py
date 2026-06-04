from typing import Protocol
from app.handler.steps.protocol import EmailProcessingStep
from app.handler.steps.context import EmailProcessingContext
from app.pgp_utils import PgpContext, PGPException, create_pgp_context, load_public_key_and_check
from app import pgp_utils, config
from app.email import headers
from app.log import LOG
from app.email_utils import delete_all_headers_except, copy, add_header
from app.message_utils import message_to_bytes
from email.message import Message
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email import encoders
from io import BytesIO
from app.models import EmailLog

class FallbackRequiredError(Exception):
    pass

class PgpEncryptionStrategy(Protocol):
    def execute(self, msg: Message, msg_bytes: bytes, pgp_fingerprint: str, public_key: str, ctx: PgpContext) -> Message:
        ...

class PrimaryGnuPgStrategy:
    def execute(self, msg: Message, msg_bytes: bytes, pgp_fingerprint: str, public_key: str, ctx: PgpContext) -> Message:
        encrypted_data = pgp_utils.encrypt_file(BytesIO(msg_bytes), pgp_fingerprint, ctx)
        second = MIMEApplication("octet-stream", _encoder=encoders.encode_7or8bit, name="encrypted.asc")
        second.add_header("Content-Disposition", 'inline; filename="encrypted.asc"')
        second.set_payload(encrypted_data)
        msg.attach(second)
        return msg

class PublicKeyCheckStrategy:
    def execute(self, msg: Message, msg_bytes: bytes, pgp_fingerprint: str, public_key: str, ctx: PgpContext) -> Message:
        LOG.w("Cannot encrypt using python-gnupg, check if public key is valid and try with pgpy")
        load_public_key_and_check(public_key, ctx)
        raise FallbackRequiredError()

class FallbackPgpyStrategy:
    def execute(self, msg: Message, msg_bytes: bytes, pgp_fingerprint: str, public_key: str, ctx: PgpContext) -> Message:
        encrypted = pgp_utils.encrypt_file_with_pgpy(msg_bytes, public_key, ctx)
        LOG.i(f"encryption works with pgpy and not with python-gnupg, public key {public_key}")
        second = MIMEApplication("octet-stream", _encoder=encoders.encode_7or8bit, name="encrypted.asc")
        second.add_header("Content-Disposition", 'inline; filename="encrypted.asc"')
        second.set_payload(str(encrypted))
        msg.attach(second)
        return msg

class PgpEncryptionExecutor:
    def __init__(self):
        self.strategies = [
            PrimaryGnuPgStrategy(),
            PublicKeyCheckStrategy(),
            FallbackPgpyStrategy()
        ]

    def encrypt_message(self, msg: Message, msg_bytes: bytes, pgp_fingerprint: str, public_key: str, ctx: PgpContext) -> Message:
        last_exception = None
        for strategy in self.strategies:
            try:
                return strategy.execute(msg, msg_bytes, pgp_fingerprint, public_key, ctx)
            except FallbackRequiredError:
                continue
            except PGPException as e:
                last_exception = e
                continue
        if last_exception:
            raise last_exception
        raise PGPException("All encryption strategies failed")

class PgpEncryptionStep(EmailProcessingStep):
    def __init__(self):
        self.executor = PgpEncryptionExecutor()

    def process(self, context: EmailProcessingContext) -> None:
        for pm in context.processing_messages:
            if pm.stop_processing:
                continue

            if context.is_reply:
                self._process_reply_msg(context, pm)
            else:
                self._process_forward_msg(context, pm)

    def _prepare_pgp_message(self, orig_msg: Message, pgp_fingerprint: str, public_key: str, can_sign: bool = False) -> Message:
        from app.email_handler import sign_msg

        msg = MIMEMultipart("encrypted", protocol="application/pgp-encrypted")
        ctx = create_pgp_context()

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

        first = MIMEApplication(_subtype="pgp-encrypted", _encoder=encoders.encode_7or8bit, _data="")
        first.set_payload("Version: 1")
        msg.attach(first)

        if can_sign and config.PGP_SENDER_PRIVATE_KEY:
            LOG.d("Sign msg")
            clone_msg = sign_msg(clone_msg, ctx)

        msg_bytes = message_to_bytes(clone_msg)
        
        # Use strategy executor
        # The executor modifies and returns the `msg` container.
        msg = self.executor.encrypt_message(msg, msg_bytes, pgp_fingerprint, public_key, ctx)

        return msg

    def _process_forward_msg(self, context: EmailProcessingContext, pm) -> None:
        user = context.user
        alias = context.alias
        contact = context.contact
        mailbox = pm.mailbox

        if mailbox.pgp_enabled() and user.is_premium() and not alias.disable_pgp:
            LOG.d("Encrypt message using mailbox %s", mailbox)
            try:
                pm.msg = self._prepare_pgp_message(
                    pm.msg, mailbox.pgp_finger_print, mailbox.pgp_public_key, can_sign=True
                )
            except PGPException:
                LOG.w("Cannot encrypt message %s -> %s. %s %s", contact, alias, mailbox, user)
                pm.msg = add_header(
                    pm.msg,
                    f"PGP encryption fails with {mailbox.email}'s PGP key",
                )

    def _process_reply_msg(self, context: EmailProcessingContext, pm) -> None:
        user = context.user
        alias = context.alias
        contact = context.contact
        mailbox = pm.mailbox
        email_log = pm.email_log
        
        from app.email import status

        if contact.pgp_finger_print and user.is_premium():
            LOG.d("Encrypt message for contact %s", contact)
            try:
                pm.msg = self._prepare_pgp_message(
                    pm.msg, contact.pgp_finger_print, contact.pgp_public_key
                )
            except PGPException:
                LOG.e("Cannot encrypt message %s -> %s. %s %s", alias, contact, mailbox, user)
                EmailLog.delete(email_log.id, commit=True)
                context.add_action_result(False, status.E402)
                pm.stop_processing = True
