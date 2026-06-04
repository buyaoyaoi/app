from dataclasses import dataclass
from typing import Callable

from app.db import Session
from app.email import status
from app.email_utils import add_header
from app.handler.email_processing_context import EmailProcessingContext
from app.log import LOG
from app.models import EmailLog
from app.pgp_utils import PGPException


class PgpEncryptionStrategy:
    def handle(self, context: EmailProcessingContext):
        raise NotImplementedError


@dataclass
class PgpEncryptionStep:
    strategy: PgpEncryptionStrategy

    def process(self, context: EmailProcessingContext):
        return self.strategy.handle(context)


@dataclass
class ForwardPgpEncryptionStrategy(PgpEncryptionStrategy):
    prepare_pgp_message: Callable[..., object]

    def handle(self, context: EmailProcessingContext):
        if (
            context.mailbox.pgp_enabled()
            and context.user.is_premium()
            and not context.alias.disable_pgp
        ):
            LOG.d("Encrypt message using mailbox %s", context.mailbox)
            try:
                context.msg = self.prepare_pgp_message(
                    context.msg,
                    context.mailbox.pgp_finger_print,
                    context.mailbox.pgp_public_key,
                    can_sign=True,
                )
            except PGPException:
                LOG.w(
                    "Cannot encrypt message %s -> %s. %s %s",
                    context.contact,
                    context.alias,
                    context.mailbox,
                    context.user,
                )
                context.msg = add_header(
                    context.msg,
                    f"""PGP encryption fails with {context.mailbox.email}'s PGP key""",
                )
        return None


@dataclass
class ReplyPgpEncryptionStrategy(PgpEncryptionStrategy):
    prepare_pgp_message: Callable[..., object]

    def handle(self, context: EmailProcessingContext):
        if context.contact.pgp_finger_print and context.user.is_premium():
            LOG.d("Encrypt message for contact %s", context.contact)
            try:
                context.msg = self.prepare_pgp_message(
                    context.msg,
                    context.contact.pgp_finger_print,
                    context.contact.pgp_public_key,
                )
            except PGPException:
                LOG.e(
                    "Cannot encrypt message %s -> %s. %s %s",
                    context.alias,
                    context.contact,
                    context.mailbox,
                    context.user,
                )
                EmailLog.delete(context.email_log.id, commit=True)
                return False, status.E402

        Session.commit()
        return None
