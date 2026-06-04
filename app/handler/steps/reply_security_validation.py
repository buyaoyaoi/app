from typing import Optional, Tuple, Set

from app import config
from app.email import headers, status
from app.email_utils import (
    get_header_unicode,
    spf_pass,
)
from app.handler.steps import EmailProcessingStep, EmailProcessingContext
from app.log import LOG
from app.mailbox_utils import get_mailbox_for_reply_phase
from app.models import Alias, Contact


class ReplyMailboxValidationStep:
    name = "reply_mailbox_validation"

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        alias = context.alias
        contact = context.contact
        envelope = context.envelope
        msg = context.msg

        mailbox = get_mailbox_for_reply_phase(
            envelope.mail_from, get_header_unicode(msg[headers.FROM]), alias
        )
        if not mailbox:
            if alias.disable_email_spoofing_check:
                LOG.w(
                    "ignore unknown sender to reverse-alias %s: %s -> %s",
                    envelope.mail_from,
                    alias,
                    contact,
                )
                mailbox = alias.mailbox
            else:
                from email_handler import handle_unknown_mailbox
                handle_unknown_mailbox(envelope, msg, context.reply_email, context.user, alias, contact)
                return (False, status.E214)

        if mailbox.is_admin_disabled():
            LOG.i(f"User {context.user} tried to send a mail from admin disabled mailbox {mailbox}")
            return (False, status.E207)

        context.mailbox = mailbox

        if (
            config.ENFORCE_SPF
            and mailbox.force_spf
            and not alias.disable_email_spoofing_check
        ):
            if not spf_pass(envelope, mailbox, context.user, alias, contact.website_email, msg):
                return (True, status.E201)

        return None

    def can_skip(self, context: EmailProcessingContext) -> bool:
        return context.alias is None or context.contact is None


class ReplySecurityValidationStep:
    name = "reply_security_validation"

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        alias = context.alias
        contact = context.contact
        envelope = context.envelope
        msg = context.msg

        from app.handler.dmarc import apply_dmarc_policy_for_reply_phase
        dmarc_delivery_status = apply_dmarc_policy_for_reply_phase(
            alias, contact, envelope, msg
        )
        if dmarc_delivery_status is not None:
            return (False, dmarc_delivery_status)

        return None

    def can_skip(self, context: EmailProcessingContext) -> bool:
        return context.alias is None or context.contact is None