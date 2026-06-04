"""
Email delivery step.
"""

from typing import Tuple, Optional
import logging
from smtplib import SMTPRecipientsRefused, SMTPServerDisconnected

from app.email_utils import (
    add_dkim_signature,
    generate_verp_email,
    get_email_domain_part,
    should_add_dkim_signature,
)
from app.email import status
from app.email import headers
from app import config
from app.models import EmailLog
from .protocol import EmailProcessingStep
from .context import EmailProcessingContext

LOG = logging.getLogger(__name__)


class DeliveryStep:
    """
    Step for delivering emails.
    """

    def process(self, context: EmailProcessingContext) -> Tuple[bool, Optional[str]]:
        """
        Deliver email(s).
        """
        if context.phase == "forward":
            return self._process_forward(context)
        else:
            return self._process_reply(context)

    def _process_forward(self, context: EmailProcessingContext) -> Tuple[bool, Optional[str]]:
        """
        Delivery for forward phase.
        """
        mailbox = context.mailbox
        if not mailbox:
            return True, None

        # Add DKIM signature
        add_dkim_signature(context.msg, config.EMAIL_DOMAIN)

        LOG.debug(
            "Forward mail from %s to %s, mail_options:%s, rcpt_options:%s ",
            context.contact.website_email,
            mailbox.email,
            context.envelope.mail_options,
            context.envelope.rcpt_options,
        )

        contact_domain = get_email_domain_part(context.contact.reply_email)
        try:
            from app.mail_sender import sl_sendmail
            from app.models import VerpType
            sl_sendmail(
                generate_verp_email(VerpType.bounce_forward, context.email_log.id, contact_domain),
                mailbox.email,
                context.msg,
                context.envelope.mail_options,
                context.envelope.rcpt_options,
                is_forward=True,
            )
        except (SMTPServerDisconnected, SMTPRecipientsRefused, TimeoutError):
            LOG.warning(
                "Postfix error during forward phase %s -> %s -> %s",
                context.contact,
                context.alias,
                mailbox,
                exc_info=True,
            )
            if should_ignore_bounce(context.envelope.mail_from):
                context.results.append((True, status.E207))
            else:
                EmailLog.delete(context.email_log.id, commit=True)
                context.results.append((False, status.E407))
        else:
            from app.db import Session
            Session.commit()
            context.results.append((True, status.E200))

        return True, None

    def _process_reply(self, context: EmailProcessingContext) -> Tuple[bool, Optional[str]]:
        """
        Delivery for reply phase.
        """
        from app.email_utils import get_email_domain_part
        alias_domain = get_email_domain_part(context.alias.email)

        LOG.debug(
            "send email from %s to %s, mail_options:%s,rcpt_options:%s",
            context.alias.email,
            context.contact.website_email,
            context.envelope.mail_options,
            context.envelope.rcpt_options,
        )

        if should_add_dkim_signature(alias_domain):
            add_dkim_signature(context.msg, alias_domain)

        try:
            from app.mail_sender import sl_sendmail
            from app.models import VerpType
            sl_sendmail(
                generate_verp_email(VerpType.bounce_reply, context.email_log.id, alias_domain),
                context.contact.website_email,
                context.msg,
                context.envelope.mail_options,
                context.envelope.rcpt_options,
                is_forward=False,
            )

            # Notify other mailboxes
            other_mailboxes = [mb for mb in context.alias.mailboxes if mb.email != context.mailbox.email]
            for mb in other_mailboxes:
                if mb.id in context.notified_mailboxes:
                    LOG.debug(f"Skipping notification to {mb.email}, already notified")
                    continue
                notify_mailbox(
                    context.alias,
                    context.mailbox,
                    mb,
                    context.msg,
                    context.orig_to,
                    context.orig_cc,
                    alias_domain
                )
                context.notified_mailboxes.add(mb.id)

        except Exception:
            LOG.warning("Cannot send email from %s to %s", context.alias, context.contact)
            EmailLog.delete(context.email_log.id, commit=True)
            if context.mailbox.can_send_or_receive():
                from app.email_utils import send_email
                from app.email_utils import render
                send_email(
                    context.mailbox.email,
                    f"Email cannot be sent to {context.contact.email} from {context.alias.email}",
                    render(
                        "transactional/reply-error.txt.jinja2",
                        user=context.user,
                        alias=context.alias,
                        contact=context.contact,
                        contact_domain=get_email_domain_part(context.contact.email),
                    ),
                    render(
                        "transactional/reply-error.html",
                        user=context.user,
                        alias=context.alias,
                        contact=context.contact,
                        contact_domain=get_email_domain_part(context.contact.email),
                    ),
                )

        context.results = [(True, status.E200)]
        return True, None

    def rollback(self, context: EmailProcessingContext) -> None:
        """
        No rollback needed for delivery (already sent or failed).
        """
        pass


def notify_mailbox(
    alias, mailbox, other_mb, msg, orig_to, orig_cc, alias_domain
):
    """
    Reuse from email_handler.
    """
    LOG.debug(
        f"notify {other_mb.email} about email sent "
        f"from {mailbox.email} on behalf of {alias.email} to {msg[headers.TO]}"
    )
    from app.email_utils import add_header, add_or_replace_header, should_add_dkim_signature, add_dkim_signature
    from app.models import TransactionalEmail, VerpType
    from app.email_utils import generate_verp_email
    from app.mail_sender import sl_sendmail
    
    notif = add_header(
        msg,
        f"""**** Don't forget to remove this section if you reply to this email ****
Email sent on behalf of alias {alias.email} using mailbox {mailbox.email}""",
    )
    add_or_replace_header(notif, headers.FROM, alias.email)
    add_or_replace_header(notif, headers.TO, orig_to)
    add_or_replace_header(notif, headers.CC, orig_cc)

    if should_add_dkim_signature(alias_domain):
        add_dkim_signature(msg, alias_domain)

    transaction = TransactionalEmail.create(email=other_mb.email, commit=True)
    sl_sendmail(
        generate_verp_email(VerpType.transactional, transaction.id, alias_domain),
        other_mb.email,
        notif,
    )


def should_ignore_bounce(mail_from: str) -> bool:
    return mail_from == "<>"
