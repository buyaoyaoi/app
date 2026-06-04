from typing import Optional, Tuple, List

import sentry_sdk
from email.message import Message
from email.utils import formatdate
from smtplib import SMTPRecipientsRefused, SMTPServerDisconnected

from app import config
from app.db import Session
from app.email import headers, status
from app.email_utils import (
    add_dkim_signature,
    generate_verp_email,
    get_email_domain_part,
    should_add_dkim_signature,
    should_ignore_bounce,
)
from app.handler.steps import EmailProcessingStep, EmailProcessingContext
from app.log import LOG
from app.mail_sender import sl_sendmail
from app.models import Alias, Contact, EmailLog, Mailbox, VerpType


class DeliveryStep:
    name = "delivery"

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        alias = context.alias
        contact = context.contact
        mailbox = context.mailbox
        msg = context.msg
        email_log = context.email_log

        contact_domain = get_email_domain_part(contact.reply_email)

        add_dkim_signature(msg, config.EMAIL_DOMAIN)

        LOG.d(
            "Forward mail from %s to %s, mail_options:%s, rcpt_options:%s",
            contact.website_email,
            mailbox.email,
            context.envelope.mail_options,
            context.envelope.rcpt_options,
        )

        try:
            sl_sendmail(
                generate_verp_email(VerpType.bounce_forward, email_log.id, contact_domain),
                mailbox.email,
                msg,
                context.envelope.mail_options,
                context.envelope.rcpt_options,
                is_forward=True,
            )
        except (SMTPServerDisconnected, SMTPRecipientsRefused, TimeoutError):
            LOG.w(
                "Postfix error during forward phase %s -> %s -> %s",
                contact,
                alias,
                mailbox,
                exc_info=True,
            )
            if should_ignore_bounce(context.envelope.mail_from):
                return (True, status.E207)
            else:
                EmailLog.delete(email_log.id, commit=True)
                return (False, status.E407)

        Session.commit()
        return (True, status.E200)

    def can_skip(self, context: EmailProcessingContext) -> bool:
        return context.email_log is None or context.mailbox is None


class MailboxPreparationStep:
    name = "mailbox_preparation"

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        alias = context.alias
        contact = context.contact
        mailbox = context.mailbox
        user = context.user
        msg = context.msg

        if mailbox.disabled:
            LOG.d(f"{mailbox} disabled, do not forward")
            if should_ignore_bounce(context.envelope.mail_from):
                return (True, status.E207)
            else:
                return (False, status.E518)

        if mailbox.is_admin_disabled():
            LOG.d(f"{mailbox} admin_disabled, do not forward")
            from app.mailbox_utils import quarantine_disabled_mailbox_email
            quarantine_disabled_mailbox_email(alias, contact, mailbox, context.envelope, msg)
            return (True, status.E207)

        if get_email_domain_part(alias.email) == get_email_domain_part(mailbox.email):
            LOG.w(
                "Mailbox has the same domain as alias. %s -> %s -> %s",
                contact,
                alias,
                mailbox,
            )
            from app.email_utils import send_email_with_rate_control, render
            mailbox_url = f"{config.URL}/dashboard/mailbox/{mailbox.id}/"
            send_email_with_rate_control(
                user,
                config.ALERT_MAILBOX_IS_ALIAS,
                user.email,
                f"Your mailbox {mailbox.email} and alias {alias.email} use the same domain",
                render(
                    "transactional/mailbox-invalid.txt.jinja2",
                    user=mailbox.user,
                    mailbox=mailbox,
                    mailbox_url=mailbox_url,
                    alias=alias,
                ),
                render(
                    "transactional/mailbox-invalid.html",
                    user=mailbox.user,
                    mailbox=mailbox,
                    mailbox_url=mailbox_url,
                    alias=alias,
                ),
                max_nb_alert=1,
            )
            return (False, status.E405)

        email_log = EmailLog.create(
            contact_id=contact.id,
            user_id=contact.user_id,
            mailbox_id=mailbox.id,
            alias_id=contact.alias_id,
            message_id=str(msg[headers.MESSAGE_ID]),
            commit=True,
        )
        LOG.d("Create %s for %s, %s, %s", email_log, contact, user, mailbox)

        context.email_log = email_log
        return None

    def can_skip(self, context: EmailProcessingContext) -> bool:
        return context.mailbox is None


class AliasAsMailboxCheckStep:
    name = "alias_as_mailbox_check"

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        mailbox = context.mailbox
        alias = context.alias
        user = context.user

        mailbox_as_alias = Alias.get_by(email=mailbox.email)
        if mailbox_as_alias is not None:
            LOG.i(
                f"Mailbox {mailbox.id} has email {mailbox.email} that is also alias {alias.id}. Stopping loop"
            )
            mailbox.verified = False
            Session.commit()

            from app.email_utils import send_email_with_rate_control, render, get_email_domain_part
            mailbox_url = f"{config.URL}/dashboard/mailbox/{mailbox.id}/"
            send_email_with_rate_control(
                user,
                config.ALERT_MAILBOX_IS_ALIAS,
                user.email,
                f"Your mailbox {mailbox.email} is an alias",
                render(
                    "transactional/mailbox-invalid.txt.jinja2",
                    user=mailbox.user,
                    mailbox=mailbox,
                    mailbox_url=mailbox_url,
                    alias=alias,
                ),
                render(
                    "transactional/mailbox-invalid.html",
                    user=mailbox.user,
                    mailbox=mailbox,
                    mailbox_url=mailbox_url,
                    alias=alias,
                ),
                max_nb_alert=1,
            )
            return (False, status.E525)

        return None

    def can_skip(self, context: EmailProcessingContext) -> bool:
        return context.mailbox is None or not context.mailbox.verified