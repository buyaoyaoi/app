from __future__ import annotations

from email.message import Message
from typing import Optional, Protocol, Tuple

from app import config, pgp_utils
from app.email import headers
from app.email_utils import (
    delete_all_headers_except,
    copy,
    message_to_bytes,
    add_header,
)
from app.log import LOG
from app.pgp_utils import (
    PGPException,
    sign_data_with_pgpy,
    sign_data,
    load_public_key_and_check,
    create_pgp_context,
)
from email import encoders
from email.encoders import encode_noop
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from io import BytesIO
from sl_pgp import PgpContext


class PGPEncryptionStrategy(Protocol):
    """Strategy protocol for PGP encryption with degradation."""

    def encrypt(self, msg: Message, fingerprint: str, public_key: str, can_sign: bool) -> Message:
        ...

    def on_failure(self, msg: Message) -> Tuple[Message, bool]:
        """Handle encryption failure. Returns (msg, should_continue)."""
        ...


class ForwardPGPStrategy:
    """Forward phase PGP strategy: degrade gracefully by adding a note."""

    def encrypt(self, msg: Message, fingerprint: str, public_key: str, can_sign: bool) -> Message:
        return prepare_pgp_message(msg, fingerprint, public_key, can_sign=can_sign)

    def on_failure(self, msg: Message) -> Tuple[Message, bool]:
        msg = add_header(
            msg,
            f"""PGP encryption fails with the mailbox's PGP key""",
        )
        return msg, True


class ReplyPGPStrategy:
    """Reply phase PGP strategy: degrade by rolling back the email log."""

    def encrypt(self, msg: Message, fingerprint: str, public_key: str, can_sign: bool) -> Message:
        return prepare_pgp_message(msg, fingerprint, public_key, can_sign=can_sign)

    def on_failure(self, msg: Message) -> Tuple[Message, bool]:
        return msg, False


def prepare_pgp_message(
    orig_msg: Message,
    pgp_fingerprint: str,
    public_key: str,
    can_sign: bool = False,
    ctx: "PgpContext | None" = None,
) -> Message:
    msg = MIMEMultipart("encrypted", protocol="application/pgp-encrypted")

    if ctx is None:
        ctx = create_pgp_context()

    clone_msg = copy(orig_msg)

    for i in reversed(range(len(clone_msg._headers))):
        header_name = clone_msg._headers[i][0].lower()
        if header_name.lower() not in headers.MIME_HEADERS:
            msg[header_name] = clone_msg._headers[i][1]

    delete_all_headers_except(
        clone_msg,
        headers.MIME_HEADERS,
    )

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

    msg_bytes = message_to_bytes(clone_msg)
    try:
        encrypted_data = pgp_utils.encrypt_file(
            BytesIO(msg_bytes), pgp_fingerprint, ctx
        )
        second.set_payload(encrypted_data)
    except PGPException:
        LOG.w(
            "Cannot encrypt using python-gnupg, check if public key is valid and try with pgpy"
        )
        load_public_key_and_check(public_key, ctx)

        encrypted = pgp_utils.encrypt_file_with_pgpy(msg_bytes, public_key, ctx)
        second.set_payload(str(encrypted))
        LOG.i(
            f"encryption works with pgpy and not with python-gnupg, public key {public_key}"
        )

    msg.attach(second)

    return msg


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