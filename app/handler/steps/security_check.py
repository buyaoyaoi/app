from __future__ import annotations

import time

from app import config
from app.db import Session
from app.email import headers, status
from app.email.spam import get_spam_score
from app.email_utils import get_spam_info
from app.handler.dmarc import (
    apply_dmarc_policy_for_forward_phase,
    apply_dmarc_policy_for_reply_phase,
)
from app.handler.steps.context import EmailProcessingContext, StepResult
from app.log import LOG
from app.models import BlockBehaviourEnum, EmailLog


class SecurityCheckStep:
    name = "security_check"

    def process(self, ctx: EmailProcessingContext) -> StepResult:
        if ctx.phase == "forward":
            return self._process_forward(ctx)
        return self._process_reply(ctx)

    def _process_forward(self, ctx: EmailProcessingContext) -> StepResult:
        alias = ctx.alias
        contact = ctx.contact
        user = ctx.user

        if alias.user.delete_on is not None:
            LOG.d(f"user {user} is pending to be deleted. Do not forward")
            EmailLog.create(
                contact_id=contact.id,
                user_id=contact.user_id,
                blocked=True,
                alias_id=contact.alias_id,
                commit=True,
            )
            return StepResult(early_return=(True, status.E502))

        if not alias.enabled or alias.is_trashed() or contact.block_forward:
            if not alias.enabled:
                LOG.d("%s is disabled, do not forward", alias)
            if alias.is_trashed():
                LOG.d("%s is trashed, do not forward", alias)
            if contact.block_forward:
                LOG.d("Contact %s of alias %s is blocked, do not forward", contact, alias)

            EmailLog.create(
                contact_id=contact.id,
                user_id=contact.user_id,
                blocked=True,
                alias_id=contact.alias_id,
                commit=True,
            )

            res_status = status.E200
            if user.block_behaviour == BlockBehaviourEnum.return_5xx:
                res_status = status.E502

            return StepResult(early_return=(True, res_status))

        ctx.msg, dmarc_delivery_status = apply_dmarc_policy_for_forward_phase(
            alias, contact, ctx.envelope, ctx.msg
        )
        if dmarc_delivery_status is not None:
            ctx.dmarc_delivery_status = dmarc_delivery_status
            return StepResult(early_return=(False, dmarc_delivery_status))

        return StepResult()

    def _process_reply(self, ctx: EmailProcessingContext) -> StepResult:
        alias = ctx.alias
        contact = ctx.contact
        user = ctx.user
        mailbox = ctx.mailbox
        msg = ctx.msg
        envelope = ctx.envelope

        dmarc_delivery_status = apply_dmarc_policy_for_reply_phase(
            alias, contact, envelope, msg
        )
        if dmarc_delivery_status is not None:
            ctx.dmarc_delivery_status = dmarc_delivery_status
            return StepResult(early_return=(False, dmarc_delivery_status))

        email_log = EmailLog.create(
            contact_id=contact.id,
            alias_id=contact.alias_id,
            is_reply=True,
            user_id=contact.user_id,
            mailbox_id=mailbox.id,
            message_id=msg[headers.MESSAGE_ID],
            commit=True,
        )
        LOG.d("Create %s for %s, %s, %s", email_log, contact, user, mailbox)
        ctx.email_log = email_log

        if config.ENABLE_SPAM_ASSASSIN:
            spam_status = ""
            is_spam = False

            if config.SPAMASSASSIN_HOST:
                start = time.time()
                spam_score, spam_report = get_spam_score(msg, email_log)
                LOG.d(
                    "%s -> %s - spam score %s in %s seconds. Spam report %s",
                    alias,
                    contact,
                    spam_score,
                    time.time() - start,
                    spam_report,
                )
                email_log.spam_score = spam_score
                if spam_score > config.MAX_REPLY_PHASE_SPAM_SCORE:
                    is_spam = True
                    email_log.spam_report = spam_report
            else:
                is_spam, spam_status = get_spam_info(
                    msg, max_score=config.MAX_REPLY_PHASE_SPAM_SCORE
                )

            if is_spam:
                LOG.w(
                    "Email detected as spam. Reply phase. %s -> %s. Spam Score: %s, Spam Report: %s",
                    alias,
                    contact,
                    email_log.spam_score,
                    email_log.spam_report,
                )

                email_log.is_spam = True
                email_log.spam_status = spam_status
                Session.commit()

                from email_handler import handle_spam

                handle_spam(contact, alias, msg, user, mailbox, email_log, is_reply=True)
                return StepResult(early_return=(False, status.E506))

        return StepResult()