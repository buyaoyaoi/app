from typing import List, Tuple, Optional, Set
from email.message import Message
from aiosmtpd.smtp import Envelope

import sentry_sdk

from app.email import status
from app.handler.steps import EmailProcessingContext
from app.handler.steps.alias_resolution import AliasResolutionStep
from app.handler.steps.contact_management import ContactManagementStep
from app.handler.steps.security_validation import (
    SecurityValidationStep,
    CycleEmailCheckStep,
    MailboxValidationStep,
)
from app.handler.steps.header_rewrite import HeaderRewriteStep
from app.handler.steps.pgp_encryption import PgpEncryptionStep
from app.handler.steps.delivery import (
    MailboxPreparationStep,
    AliasAsMailboxCheckStep,
    DeliveryStep,
    SpamCheckStep,
)
from app.handler.steps.reply_alias_resolution import ReplyAliasResolutionStep
from app.handler.steps.reply_security_validation import (
    ReplyMailboxValidationStep,
    ReplySecurityValidationStep,
)
from app.handler.steps.reply_header_rewrite import (
    ReplyHeaderRewriteStep,
    ReplyFromHeaderStep,
    ReplyMessageIdStep,
)
from app.handler.steps.reply_pgp_encryption import ReplyPgpEncryptionStep
from app.handler.steps.reply_delivery import (
    ReplyEmailLogStep,
    ReplySpamCheckStep,
    ReplyDeliveryStep,
)
from app.log import LOG


class EmailProcessingPipeline:
    __slots__ = ("steps", "context", "phase", "mailbox_steps")

    def __init__(
        self,
        steps: List,
        context: EmailProcessingContext,
        phase: str = "forward",
        mailbox_steps: Optional[List] = None,
    ):
        self.steps = steps
        self.context = context
        self.phase = phase
        self.mailbox_steps = mailbox_steps or []

    @classmethod
    def for_forward(
        cls,
        envelope: Envelope,
        msg: Message,
        rcpt_to: str,
    ) -> "EmailProcessingPipeline":
        context = EmailProcessingContext(
            envelope=envelope,
            msg=msg,
            rcpt_to=rcpt_to,
            phase="forward",
        )

        steps = [
            AliasResolutionStep(),
            CycleEmailCheckStep(),
            ContactManagementStep(),
            SecurityValidationStep(),
            MailboxValidationStep(),
        ]

        mailbox_steps = [
            MailboxPreparationStep(),
            AliasAsMailboxCheckStep(),
            SpamCheckStep(),
            HeaderRewriteStep(),
            PgpEncryptionStep(),
            DeliveryStep(),
        ]

        return cls(steps=steps, context=context, phase="forward", mailbox_steps=mailbox_steps)

    @classmethod
    def for_reply(
        cls,
        envelope: Envelope,
        msg: Message,
        rcpt_to: str,
        notified_mailboxes: Set[int],
    ) -> "EmailProcessingPipeline":
        context = EmailProcessingContext(
            envelope=envelope,
            msg=msg,
            rcpt_to=rcpt_to,
            phase="reply",
        )
        context.notified_mailboxes = notified_mailboxes

        steps = [
            ReplyAliasResolutionStep(),
            ReplyMailboxValidationStep(),
            ReplySecurityValidationStep(),
            ReplyEmailLogStep(),
            ReplySpamCheckStep(),
            ReplyHeaderRewriteStep(),
            ReplyPgpEncryptionStep(),
            ReplyFromHeaderStep(),
            ReplyMessageIdStep(),
            ReplyDeliveryStep(),
        ]

        return cls(steps=steps, context=context, phase="reply")

    @sentry_sdk.trace
    def execute(self) -> List[Tuple[bool, str]]:
        results: List[Tuple[bool, str]] = []

        for step in self.steps:
            step_name = step.name
            LOG.d(f"Executing step: {step_name} in {self.phase} phase")

            if step.can_skip(self.context):
                LOG.d(f"Skipping step: {step_name}")
                continue

            result = step.process(self.context)

            if result is not None:
                is_success, smtp_status = result
                results.append((is_success, smtp_status))
                LOG.d(
                    f"Step {step_name} returned result: is_success={is_success}, status={smtp_status}"
                )
                if not is_success:
                    self._rollback()
                    return results

        return results

    def execute_for_each_mailbox(self) -> List[Tuple[bool, str]]:
        results: List[Tuple[bool, str]] = []

        if self.context.mailboxes is None:
            return results

        from app.email_utils import copy
        original_msg = self.context.msg

        for mailbox in self.context.mailboxes:
            if not mailbox.verified:
                LOG.d("%s unverified, do not forward", mailbox)
                results.append((False, status.E517))
                continue

            self.context.mailbox = mailbox
            self.context.msg = copy(original_msg)

            for step in self.mailbox_steps:
                step_name = step.name
                LOG.d(f"Executing mailbox step: {step_name} for mailbox {mailbox.email}")

                if step.can_skip(self.context):
                    LOG.d(f"Skipping mailbox step: {step_name}")
                    continue

                result = step.process(self.context)

                if result is not None:
                    is_success, smtp_status = result
                    results.append((is_success, smtp_status))
                    LOG.d(
                        f"Mailbox step {step_name} returned: is_success={is_success}, status={smtp_status}"
                    )
                    if not is_success:
                        break

        return results

    def _rollback(self) -> None:
        LOG.d(f"Rolling back pipeline for {self.phase} phase")
        for step in reversed(self.steps):
            try:
                step.rollback(self.context)
            except Exception as e:
                LOG.w(f"Error during rollback of step {step.name}: {e}")

        self.context.rollback()