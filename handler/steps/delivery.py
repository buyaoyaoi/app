from app.handler.steps.protocol import EmailProcessingStep
from app.handler.steps.context import EmailProcessingContext
from app.email import status, headers
from app.log import LOG
from app import config
from app.email_utils import should_ignore_bounce, get_email_domain_part, add_dkim_signature, generate_verp_email
from app.mail_sender import sl_sendmail
from app.models import EmailLog, VerpType
from app.db import Session
from app.email_handler import should_add_dkim_signature, notify_mailbox
from smtplib import SMTPRecipientsRefused, SMTPServerDisconnected

class DeliveryStep(EmailProcessingStep):
    def process(self, context: EmailProcessingContext) -> None:
        for pm in context.processing_messages:
            if pm.stop_processing:
                continue

            if context.is_reply:
                self._process_reply_msg(context, pm)
            else:
                self._process_forward_msg(context, pm)

    def _process_forward_msg(self, context: EmailProcessingContext, pm) -> None:
        contact = context.contact
        mailbox = pm.mailbox
        email_log = pm.email_log
        envelope = context.envelope

        add_dkim_signature(pm.msg, config.EMAIL_DOMAIN)

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
                generate_verp_email(VerpType.bounce_forward, email_log.id, contact_domain),
                mailbox.email,
                pm.msg,
                envelope.mail_options,
                envelope.rcpt_options,
                is_forward=True,
            )
        except (SMTPServerDisconnected, SMTPRecipientsRefused, TimeoutError):
            LOG.w(
                "Postfix error during forward phase %s -> %s -> %s",
                contact, context.alias, mailbox, exc_info=True,
            )
            if should_ignore_bounce(envelope.mail_from):
                context.add_action_result(True, status.E207)
            else:
                EmailLog.delete(email_log.id, commit=True)
                context.add_action_result(False, status.E407)
            pm.stop_processing = True
            return
        else:
            Session.commit()
            context.add_action_result(True, status.E200)

    def _process_reply_msg(self, context: EmailProcessingContext, pm) -> None:
        alias = context.alias
        contact = context.contact
        mailbox = pm.mailbox
        email_log = pm.email_log
        envelope = context.envelope

        alias_domain = get_email_domain_part(alias.email)

        LOG.d(
            "send email from %s to %s, mail_options:%s,rcpt_options:%s",
            alias.email,
            contact.website_email,
            envelope.mail_options,
            envelope.rcpt_options,
        )

        if should_add_dkim_signature(alias_domain):
            add_dkim_signature(pm.msg, alias_domain)

        try:
            sl_sendmail(
                generate_verp_email(VerpType.bounce_reply, email_log.id, alias_domain),
                contact.website_email,
                pm.msg,
                envelope.mail_options,
                envelope.rcpt_options,
                is_forward=False,
            )

            other_mailboxes = [mb for mb in alias.mailboxes if mb.email != mailbox.email]
            for mb in other_mailboxes:
                if mb.id in context.notified_mailboxes:
                    LOG.d(f"Skipping notification to {mb.email}, already notified")
                    continue
                # notify_mailbox uses orig_to and orig_cc. We need to parse it from the msg.
                # Actually, in original code it uses orig_to and orig_cc from the original message.
                # We can just fetch it from context.msg (the unmodified message)
                orig_to = context.msg[headers.TO]
                orig_cc = context.msg[headers.CC]
                notify_mailbox(alias, mailbox, mb, pm.msg, orig_to, orig_cc, alias_domain)
                context.notified_mailboxes.add(mb.id)

        except Exception:
            LOG.w("Cannot send email from %s to %s", alias, contact)
            EmailLog.delete(email_log.id, commit=True)
            from app.email_utils import send_email, render
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

        context.add_action_result(True, status.E200)
