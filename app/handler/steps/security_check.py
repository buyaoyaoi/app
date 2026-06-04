from dataclasses import dataclass
from typing import Callable

from app import config
from app.db import Session
from app.email import headers, status
from app.email.spam import get_spam_score
from app.email_utils import get_header_unicode, get_spam_info, should_ignore_bounce, spf_pass
from app.handler.dmarc import (
    apply_dmarc_policy_for_forward_phase,
    apply_dmarc_policy_for_reply_phase,
)
from app.handler.email_processing_context import EmailProcessingContext
from app.log import LOG
from app.mailbox_utils import get_mailbox_for_reply_phase
from app.models import EmailLog


class SecurityCheckStrategy:
    def handle(self, context: EmailProcessingContext):
        raise NotImplementedError


@dataclass
class SecurityCheckStep:
    strategy: SecurityCheckStrategy

    def process(self, context: EmailProcessingContext):
        return self.strategy.handle(context)


class ForwardSecurityCheckStrategy(SecurityCheckStrategy):
    def handle(self, context: EmailProcessingContext):
        context.msg, dmarc_delivery_status = apply_dmarc_policy_for_forward_phase(
            context.alias, context.contact, context.envelope, context.msg
        )
        if dmarc_delivery_status is not None:
            return [(False, dmarc_delivery_status)]

        context.mailboxes = list(context.alias.mailboxes)
        if not context.mailboxes:
            LOG.w("no valid mailboxes for %s", context.alias)
            if should_ignore_bounce(context.envelope.mail_from):
                return [(True, status.E207)]
            return [(False, status.E516)]

        return None


@dataclass
class ForwardMailboxSecurityCheckStrategy(SecurityCheckStrategy):
    quarantine_disabled_mailbox_email: Callable[..., None]
    handle_spam: Callable[..., None]
    send_invalid_mailbox_notification: Callable[..., None]

    def handle(self, context: EmailProcessingContext):
        mailbox = context.mailbox
        alias = context.alias
        contact = context.contact
        user = context.user

        if mailbox.disabled:
            LOG.d(f"{mailbox} disabled, do not forward")
            from app.email_utils import should_ignore_bounce

            if should_ignore_bounce(context.envelope.mail_from):
                return True, status.E207
            return False, status.E518

        if mailbox.is_admin_disabled():
            LOG.d(f"{mailbox} admin_disabled, do not forward")
            self.quarantine_disabled_mailbox_email(
                alias, contact, mailbox, context.envelope, context.msg
            )
            return True, status.E207

        if context.alias_domain == mailbox.email.split("@")[-1]:
            LOG.w(
                "Mailbox has the same domain as alias. %s -> %s -> %s",
                contact,
                alias,
                mailbox,
            )
            self.send_invalid_mailbox_notification(
                user,
                mailbox,
                alias,
                f"Your mailbox {mailbox.email} and alias {alias.email} use the same domain",
            )
            return False, status.E405

        email_log = EmailLog.create(
            contact_id=contact.id,
            user_id=contact.user_id,
            mailbox_id=mailbox.id,
            alias_id=contact.alias_id,
            message_id=str(context.msg[headers.MESSAGE_ID]),
            commit=True,
        )
        LOG.d("Create %s for %s, %s, %s", email_log, contact, user, mailbox)
        context.email_log = email_log

        if config.ENABLE_SPAM_ASSASSIN:
            spam_status = ""
            is_spam = False

            if config.SPAMASSASSIN_HOST:
                spam_score, spam_report = get_spam_score(context.msg, email_log)
                LOG.d(
                    "%s -> %s - spam score:%s. Spam report %s",
                    contact,
                    alias,
                    spam_score,
                    spam_report,
                )
                email_log.spam_score = spam_score
                Session.commit()

                if (user.max_spam_score and spam_score > user.max_spam_score) or (
                    not user.max_spam_score and spam_score > config.MAX_SPAM_SCORE
                ):
                    is_spam = True
                    email_log.spam_report = spam_report
            else:
                is_spam, spam_status = get_spam_info(
                    context.msg, max_score=user.max_spam_score
                )

            if is_spam:
                LOG.w(
                    "Email detected as spam. %s -> %s. Spam Score: %s, Spam Report: %s",
                    contact,
                    alias,
                    email_log.spam_score,
                    email_log.spam_report,
                )
                email_log.is_spam = True
                email_log.spam_status = spam_status
                Session.commit()
                self.handle_spam(contact, alias, context.msg, user, mailbox, email_log)
                return False, status.E519

        return None


@dataclass
class ReplySecurityCheckStrategy(SecurityCheckStrategy):
    handle_unknown_mailbox: Callable[..., None]
    handle_spam: Callable[..., None]

    def handle(self, context: EmailProcessingContext):
        dmarc_delivery_status = apply_dmarc_policy_for_reply_phase(
            context.alias, context.contact, context.envelope, context.msg
        )
        if dmarc_delivery_status is not None:
            return False, dmarc_delivery_status

        mailbox = get_mailbox_for_reply_phase(
            context.envelope.mail_from,
            get_header_unicode(context.msg[headers.FROM]),
            context.alias,
        )
        if not mailbox:
            if context.alias.disable_email_spoofing_check:
                LOG.w(
                    "ignore unknown sender to reverse-alias %s: %s -> %s",
                    context.envelope.mail_from,
                    context.alias,
                    context.contact,
                )
                mailbox = context.alias.mailbox
            else:
                self.handle_unknown_mailbox(
                    context.envelope,
                    context.msg,
                    context.reply_email,
                    context.user,
                    context.alias,
                    context.contact,
                )
                return False, status.E214

        if mailbox.is_admin_disabled():
            LOG.i(
                f"User {context.user} tried to send a mail from admin disabled mailbox {mailbox}"
            )
            return False, status.E207

        if (
            config.ENFORCE_SPF
            and mailbox.force_spf
            and not context.alias.disable_email_spoofing_check
        ):
            if not spf_pass(
                context.envelope,
                mailbox,
                context.user,
                context.alias,
                context.contact.website_email,
                context.msg,
            ):
                return True, status.E201

        email_log = EmailLog.create(
            contact_id=context.contact.id,
            alias_id=context.contact.alias_id,
            is_reply=True,
            user_id=context.contact.user_id,
            mailbox_id=mailbox.id,
            message_id=context.msg[headers.MESSAGE_ID],
            commit=True,
        )
        LOG.d(
            "Create %s for %s, %s, %s",
            email_log,
            context.contact,
            context.user,
            mailbox,
        )
        context.mailbox = mailbox
        context.email_log = email_log

        if config.ENABLE_SPAM_ASSASSIN:
            spam_status = ""
            is_spam = False

            if config.SPAMASSASSIN_HOST:
                spam_score, spam_report = get_spam_score(context.msg, email_log)
                LOG.d(
                    "%s -> %s - spam score %s. Spam report %s",
                    context.alias,
                    context.contact,
                    spam_score,
                    spam_report,
                )
                email_log.spam_score = spam_score
                if spam_score > config.MAX_REPLY_PHASE_SPAM_SCORE:
                    is_spam = True
                    email_log.spam_report = spam_report
            else:
                is_spam, spam_status = get_spam_info(
                    context.msg, max_score=config.MAX_REPLY_PHASE_SPAM_SCORE
                )

            if is_spam:
                LOG.w(
                    "Email detected as spam. Reply phase. %s -> %s. Spam Score: %s, Spam Report: %s",
                    context.alias,
                    context.contact,
                    email_log.spam_score,
                    email_log.spam_report,
                )
                email_log.is_spam = True
                email_log.spam_status = spam_status
                Session.commit()
                self.handle_spam(
                    context.contact,
                    context.alias,
                    context.msg,
                    context.user,
                    mailbox,
                    email_log,
                    is_reply=True,
                )
                return False, status.E506

        return None
