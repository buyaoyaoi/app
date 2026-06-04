from typing import Optional, Tuple, List

import arrow
import sentry_sdk

from app.config import config
from app.email import headers, status
from app.email_utils import should_ignore_bounce
from app.email.spam import get_spam_score
from app.email_utils import get_spam_info
from app.handler.dmarc import apply_dmarc_policy_for_forward_phase
from app.handler.steps import EmailProcessingStep, EmailProcessingContext
from app.log import LOG
from app.models import Alias, Contact, EmailLog, BlockBehaviourEnum


class SecurityValidationStep:
    name = "security_validation"

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        user = context.user
        alias = context.alias
        contact = context.contact

        if not user.is_active():
            LOG.w(f"User {user} has been soft deleted")
            return (False, status.E502)

        if not user.can_send_or_receive():
            LOG.i(f"User {user} cannot receive emails")
            if should_ignore_bounce(context.envelope.mail_from):
                return (True, status.E207)
            else:
                return (False, status.E504)

        if alias.custom_domain_id and not alias.custom_domain.verified:
            LOG.w("Alias %s is on unverified custom domain, refusing email", alias)
            return (False, status.E520)

        if alias.user.delete_on is not None:
            LOG.d(f"user {user} is pending to be deleted. Do not forward")
            EmailLog.create(
                contact_id=contact.id,
                user_id=contact.user_id,
                blocked=True,
                alias_id=contact.alias_id,
                commit=True,
            )
            return (True, status.E502)

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
            return (True, res_status)

        msg, dmarc_delivery_status = apply_dmarc_policy_for_forward_phase(
            alias, contact, context.envelope, context.msg
        )
        if dmarc_delivery_status is not None:
            return (False, dmarc_delivery_status)

        context.msg = msg
        return None

    def can_skip(self, context: EmailProcessingContext) -> bool:
        return context.alias is None or context.contact is None


class SpamCheckStep:
    name = "spam_check"

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        if not config.ENABLE_SPAM_ASSASSIN:
            return None

        user = context.user
        alias = context.alias
        contact = context.contact
        mailbox = context.mailbox
        msg = context.msg

        spam_status = ""
        is_spam = False

        if config.SPAMASSASSIN_HOST:
            import time
            start = time.time()
            spam_score, spam_report = get_spam_score(msg, context.email_log)
            LOG.d(
                "%s -> %s - spam score:%s in %s seconds. Spam report %s",
                contact,
                alias,
                spam_score,
                time.time() - start,
                spam_report,
            )
            context.email_log.spam_score = spam_score

            if (user.max_spam_score and spam_score > user.max_spam_score) or (
                not user.max_spam_score and spam_score > config.MAX_SPAM_SCORE
            ):
                is_spam = True
                context.email_log.spam_report = spam_report
        else:
            is_spam, spam_status = get_spam_info(msg, max_score=user.max_spam_score)

        if is_spam:
            LOG.w(
                "Email detected as spam. %s -> %s. Spam Score: %s, Spam Report: %s",
                contact,
                alias,
                context.email_log.spam_score,
                context.email_log.spam_report,
            )
            context.email_log.is_spam = True
            context.email_log.spam_status = spam_status

            from email_handler import handle_spam
            handle_spam(contact, alias, msg, user, mailbox, context.email_log)
            return (False, status.E519)

        context.is_spam = is_spam
        context.spam_score = spam_score if config.SPAMASSASSIN_HOST else None
        context.spam_report = spam_report if config.SPAMASSASSIN_HOST else None
        context.spam_status = spam_status
        return None

    def can_skip(self, context: EmailProcessingContext) -> bool:
        return context.email_log is None


class MailboxValidationStep:
    name = "mailbox_validation"

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        alias = context.alias
        mailboxes = alias.mailboxes

        if not mailboxes:
            LOG.w("no valid mailboxes for %s", alias)
            if should_ignore_bounce(context.envelope.mail_from):
                return (True, status.E207)
            else:
                return (False, status.E516)

        context.mailboxes = mailboxes
        return None

    def can_skip(self, context: EmailProcessingContext) -> bool:
        return context.alias is None


class CycleEmailCheckStep:
    name = "cycle_email_check"

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        alias = context.alias
        user = context.user
        mail_from = context.envelope.mail_from

        for addr in alias.authorized_addresses():
            if addr == mail_from:
                LOG.i("cycle email sent from %s to %s", addr, alias)
                from email_handler import handle_email_sent_to_ourself
                handle_email_sent_to_ourself(alias, addr, context.msg, user)
                return (True, status.E209)

        return None

    def can_skip(self, context: EmailProcessingContext) -> bool:
        return context.alias is None