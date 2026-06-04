from __future__ import annotations

from smtplib import SMTPRecipientsRefused, SMTPServerDisconnected

from app import config
from app.db import Session
from app.email import status
from app.email_utils import (
    should_ignore_bounce,
    get_email_domain_part,
    generate_verp_email,
    render,
    send_email,
    add_header,
    add_or_replace_header,
    add_dkim_signature,
    should_add_dkim_signature,
)
from app.handler.steps.context import EmailProcessingContext, StepResult
from app.log import LOG
from app.mail_sender import sl_sendmail
from app.models import (
    EmailLog,
    TransactionalEmail,
    VerpType,
)


class DeliveryStep:
    name = "delivery"

    def process(self, ctx: EmailProcessingContext) -> StepResult:
        if ctx.phase == "forward_per_mailbox":
            return self._process_forward_per_mailbox(ctx)
        return self._process_reply(ctx)

    def _process_forward_per_mailbox(
        self, ctx: EmailProcessingContext
    ) -> StepResult:
        msg = ctx.msg
        alias = ctx.alias
        contact = ctx.contact
        mailbox = ctx.mailbox
        envelope = ctx.envelope
        email_log = ctx.email_log

        LOG.d(
            "Forward mail from %s to %s, mail_options:%s, rcpt_options:%s ",
            contact.website_email,
            mailbox.email,
            envelope.mail_options,
            envelope.rcpt_options,
        )

        contact_domain = get_email_domain_part(contact.reply_email)
        try:
            sl_sendmail(
                generate_verp_email(
                    VerpType.bounce_forward, email_log.id, contact_domain
                ),
                mailbox.email,
                msg,
                envelope.mail_options,
                envelope.rcpt_options,
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
            if should_ignore_bounce(envelope.mail_from):
                return StepResult(early_return=(True, status.E207))
            else:
                EmailLog.delete(email_log.id, commit=True)
                return StepResult(early_return=(False, status.E407))
        else:
            Session.commit()
            return StepResult(early_return=(True, status.E200))

    def _process_reply(self, ctx: EmailProcessingContext) -> StepResult:
        alias = ctx.alias
        contact = ctx.contact
        mailbox = ctx.mailbox
        user = ctx.user
        envelope = ctx.envelope
        msg = ctx.msg
        email_log = ctx.email_log
        notified_mailboxes = ctx.notified_mailboxes
        orig_to = ctx.orig_to
        orig_cc = ctx.orig_cc
        alias_domain = ctx.alias_domain

        LOG.d(
            "send email from %s to %s, mail_options:%s,rcpt_options:%s",
            alias.email,
            contact.website_email,
            envelope.mail_options,
            envelope.rcpt_options,
        )

        try:
            sl_sendmail(
                generate_verp_email(
                    VerpType.bounce_reply, email_log.id, alias_domain
                ),
                contact.website_email,
                msg,
                envelope.mail_options,
                envelope.rcpt_options,
                is_forward=False,
            )

            other_mailboxes = [
                mb
                for mb in alias.mailboxes
                if mb.email != mailbox.email
            ]
            for mb in other_mailboxes:
                if mb.id in notified_mailboxes:
                    LOG.d(
                        f"Skipping notification to {mb.email}, already notified"
                    )
                    continue
                self._notify_mailbox(
                    alias,
                    mailbox,
                    mb,
                    msg,
                    orig_to,
                    orig_cc,
                    alias_domain,
                )
                notified_mailboxes.add(mb.id)

        except Exception:
            LOG.w("Cannot send email from %s to %s", alias, contact)
            EmailLog.delete(email_log.id, commit=True)
            if mailbox.can_send_or_receive():
                send_email(
                    mailbox.email,
                    f"Email cannot be sent to {contact.email} from {alias.email}",
                    render(
                        "transactional/reply-error.txt.jinja2",
                        user=user,
                        alias=alias,
                        contact=contact,
                        contact_domain=get_email_domain_part(contact.email),
                    ),
                    render(
                        "transactional/reply-error.html",
                        user=user,
                        alias=alias,
                        contact=contact,
                        contact_domain=get_email_domain_part(contact.email),
                    ),
                )

        return StepResult(early_return=(True, status.E200))

    def _notify_mailbox(
        self,
        alias,
        mailbox,
        other_mb,
        msg,
        orig_to,
        orig_cc,
        alias_domain,
    ):
        LOG.d(
            f"notify {other_mb.email} about email sent "
            f"from {mailbox.email} on behalf of {alias.email} to {msg['To']}"
        )
        notif = add_header(
            msg,
            f"""**** Don't forget to remove this section if you reply to this email ****
Email sent on behalf of alias {alias.email} using mailbox {mailbox.email}""",
        )
        add_or_replace_header(notif, "From", alias.email)
        add_or_replace_header(notif, "To", orig_to)
        add_or_replace_header(notif, "Cc", orig_cc)

        if should_add_dkim_signature(alias_domain):
            add_dkim_signature(msg, alias_domain)

        transaction = TransactionalEmail.create(
            email=other_mb.email, commit=True
        )
        sl_sendmail(
            generate_verp_email(
                VerpType.transactional, transaction.id, alias_domain
            ),
            other_mb.email,
            notif,
        )