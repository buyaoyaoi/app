from __future__ import annotations

from app.db import Session
from app.email import status
from app.handler.steps.context import EmailProcessingContext, StepResult
from app.handler.steps.pgp_encryption import (
    prepare_pgp_message,
    ForwardPGPStrategy,
    ReplyPGPStrategy,
)
from app.log import LOG
from app.models import EmailLog
from app.pgp_utils import PGPException


class PGPEncryptionStep:
    name = "pgp_encryption"

    def process(self, ctx: EmailProcessingContext) -> StepResult:
        if ctx.phase in ("forward", "forward_per_mailbox"):
            return self._process_forward(ctx)
        return self._process_reply(ctx)

    def _process_forward(self, ctx: EmailProcessingContext) -> StepResult:
        from email_handler import add_header

        alias = ctx.alias
        mailbox = ctx.mailbox
        user = ctx.user

        if mailbox.pgp_enabled() and user.is_premium() and not alias.disable_pgp:
            LOG.d("Encrypt message using mailbox %s", mailbox)
            strategy = ForwardPGPStrategy()

            try:
                ctx.msg = strategy.encrypt(
                    ctx.msg,
                    mailbox.pgp_finger_print,
                    mailbox.pgp_public_key,
                    can_sign=True,
                )
            except PGPException:
                LOG.w(
                    "Cannot encrypt message %s -> %s. %s %s",
                    ctx.contact,
                    alias,
                    mailbox,
                    user,
                )
                ctx.msg, _ = strategy.on_failure(ctx.msg)

        return StepResult()

    def _process_reply(self, ctx: EmailProcessingContext) -> StepResult:
        contact = ctx.contact
        user = ctx.user
        email_log = ctx.email_log

        if contact.pgp_finger_print and user.is_premium():
            LOG.d("Encrypt message for contact %s", contact)
            strategy = ReplyPGPStrategy()

            try:
                ctx.msg = strategy.encrypt(
                    ctx.msg,
                    contact.pgp_finger_print,
                    contact.pgp_public_key,
                    can_sign=False,
                )
            except PGPException:
                LOG.e(
                    "Cannot encrypt message %s -> %s. %s %s",
                    ctx.alias,
                    contact,
                    ctx.mailbox,
                    user,
                )
                ctx.msg, should_continue = strategy.on_failure(ctx.msg)
                if not should_continue:
                    EmailLog.delete(email_log.id, commit=True)
                    return StepResult(early_return=(False, status.E402))

        Session.commit()
        return StepResult()