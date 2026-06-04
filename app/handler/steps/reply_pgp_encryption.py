from typing import Optional, Tuple

from email.message import Message

import sentry_sdk

from app import config
from app.email import headers, status
from app.email_utils import add_header
from app.handler.steps import EmailProcessingStep, EmailProcessingContext
from app.handler.steps.pgp_encryption import PgpEncryptionFallbackChain, GnupgEncryptionStrategy, PgpyEncryptionStrategy
from app.log import LOG
from app.models import EmailLog
from app.pgp_utils import PGPException, create_pgp_context


class ReplyPgpEncryptionStep:
    name = "reply_pgp_encryption"

    def __init__(self):
        self.fallback_chain = PgpEncryptionFallbackChain([
            GnupgEncryptionStrategy(),
            PgpyEncryptionStrategy(),
        ])

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        contact = context.contact
        user = context.user
        alias = context.alias
        msg = context.msg
        email_log = context.email_log

        if not (contact.pgp_finger_print and user.is_premium()):
            return None

        LOG.d("Encrypt message for contact %s", contact)
        try:
            ctx = create_pgp_context()
            encrypted_msg, error = self.fallback_chain.encrypt(
                msg,
                contact.pgp_finger_print,
                contact.pgp_public_key,
                can_sign=False,
                ctx=ctx,
            )
            if encrypted_msg:
                context.msg = encrypted_msg
                return None
            else:
                LOG.e(
                    "Cannot encrypt message %s -> %s. %s %s", alias, contact, context.mailbox, user
                )
                EmailLog.delete(email_log.id, commit=True)
                return (False, status.E402)
        except PGPException:
            LOG.e(
                "Cannot encrypt message %s -> %s. %s %s", alias, contact, context.mailbox, user
            )
            EmailLog.delete(email_log.id, commit=True)
            return (False, status.E402)

    def can_skip(self, context: EmailProcessingContext) -> bool:
        if context.contact is None or context.user is None:
            return True
        return not (context.contact.pgp_finger_print and context.user.is_premium())