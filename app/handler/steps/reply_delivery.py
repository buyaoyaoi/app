from typing import Optional, Tuple, Set

from email.message import Message
from email.utils import formatdate

import sentry_sdk

from app import config
from app.db import Session
from app.email import headers, status
from app.email_utils import (
    add_dkim_signature,
    add_or_replace_header,
    add_header,
    generate_verp_email,
    get_email_domain_part,
    render,
    send_email,
    should_add_dkim_signature,
)
from app.handler.steps import EmailProcessingStep, EmailProcessingContext
from app.log import LOG
from app.mail_sender import sl_sendmail
from app.models import Alias, Contact, EmailLog, Mailbox, TransactionalEmail, VerpType


class ReplyDeliveryStep:
    name = "reply_delivery"

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        alias = context.alias
        contact = context.contact
        mailbox = context.mailbox
        msg = context.msg
        email_log = context.email_log
        alias_domain = context.alias_domain
        envelope = context.envelope

        LOG.d(
            "send email from %s to %s, mail_options:%s, rcpt_options:%s",
            alias.email,
            contact.website_email,
            envelope.mail_options,
            envelope.rcpt_options,
        )

        if should_add_dkim_signature(alias_domain):
            add_dkim_signature(msg, alias_domain)

        try:
            sl_sendmail(
                generate_verp_email(VerpType.bounce_reply, email_log.id, alias_domain),
                contact.website_email,
                msg,
                envelope.mail_options,
                envelope.rcpt_options,
                is_forward=False,
            )

            other_mailboxes = [mb for mb in alias.mailboxes if mb.email != mailbox.email]
            for mb in other_mailboxes:
                if mb.id in context.notified_mailboxes:
                    LOG.d(f"Skipping notification to {mb.email}, already notified")
                    continue
                notify_mailbox(alias, mailbox, mb, msg, context.orig_to, context.orig_cc, alias_domain)
                context.notified_mailboxes.add(mb.id)

        except Exception:
            LOG.w("Cannot send email from %s to %s", alias, contact)
            EmailLog.delete(email_log.id, commit=True)
            if mailbox.can_send_or_receive():
                send_email(
                    mailbox.email,
                    f"Email cannot be sent to {contact.email} from {alias.email}",
                    render(
                        "transactional/reply-error.txt.jinja2",
                        user=context.user,
                        alias=alias,
                        contact=contact,
                        contact_domain=get_email_domain_part(contact.email),
                    ),
                    render(
                        "transactional/reply-error.html",
                        user=context.user,
                        alias=alias,
                        contact=contact,
                        contact_domain=get_email_domain_part(contact.email),
                    ),
                )

        return (True, status.E200)

    def can_skip(self, context: EmailProcessingContext) -> bool:
        return context.email_log is None


@sentry_sdk.trace
def notify_mailbox(
    alias: Alias,
    mailbox: Mailbox,
    other_mb: Mailbox,
    msg: Message,
    orig_to: str,
    orig_cc: str,
    alias_domain: str,
):
    LOG.d(
        f"notify {other_mb.email} about email sent "
        f"from {mailbox.email} on behalf of {alias.email} to {msg[headers.TO]}"
    )
    notif = add_header(
        msg,
        f"""**** Don't forget to remove this section if you reply to this email ****
Email sent on behalf of alias {alias.email} using mailbox {mailbox.email}""",
    )
    add_or_replace_header(notif, headers.FROM, alias.email)
    add_or_replace_header(notif, headers.TO, orig_to)
    add_or_replace_header(notif, headers.CC, orig_cc)

    if should_add_dkim_signature(alias_domain):
        add_dkim_signature(notif, alias_domain)

    transaction = TransactionalEmail.create(email=other_mb.email, commit=True)
    sl_sendmail(
        generate_verp_email(VerpType.transactional, transaction.id, alias_domain),
        other_mb.email,
        notif,
    )


class ReplyEmailLogStep:
    name = "reply_email_log"

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        contact = context.contact
        alias = context.alias
        mailbox = context.mailbox
        msg = context.msg

        email_log = EmailLog.create(
            contact_id=contact.id,
            alias_id=contact.alias_id,
            is_reply=True,
            user_id=contact.user_id,
            mailbox_id=mailbox.id,
            message_id=msg[headers.MESSAGE_ID],
            commit=True,
        )
        LOG.d("Create %s for %s, %s, %s", email_log, contact, context.user, mailbox)

        context.email_log = email_log
        return None

    def can_skip(self, context: EmailProcessingContext) -> bool:
        return context.contact is None or context.mailbox is None


class ReplySpamCheckStep:
    name = "reply_spam_check"

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        if not config.ENABLE_SPAM_ASSASSIN:
            return None

        alias = context.alias
        contact = context.contact
        mailbox = context.mailbox
        msg = context.msg
        email_log = context.email_log
        user = context.user

        spam_status = ""
        is_spam = False

        if config.SPAMASSASSIN_HOST:
            import time
            start = time.time()
            from app.email.spam import get_spam_score
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
            from app.email_utils import get_spam_info
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
            return (False, status.E506)

        return None

    def can_skip(self, context: EmailProcessingContext) -> bool:
        return context.email_log is None