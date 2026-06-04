from typing import Optional, Tuple

from app.email import status
from app.email_utils import (
    get_email_domain_part,
    is_valid_alias_address_domain,
    normalize_reply_email,
)
from app.handler.steps import EmailProcessingStep, EmailProcessingContext
from app.log import LOG
from app.models import Alias, Contact, SLDomain


class ReplyAliasResolutionStep:
    name = "reply_alias_resolution"

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        reply_email = context.rcpt_to
        reply_domain = get_email_domain_part(reply_email)

        from app import config
        if not reply_email.endswith(config.EMAIL_DOMAIN):
            sl_domain: SLDomain = SLDomain.get_by(domain=reply_domain)
            if sl_domain is None:
                LOG.w(f"Reply email {reply_email} has wrong domain")
                return (False, status.E501)

        reply_email = normalize_reply_email(reply_email)
        context.reply_email = reply_email

        contact = Contact.get_by(reply_email=reply_email)
        if not contact:
            LOG.w(f"No contact with {reply_email} as reverse alias")
            return (False, status.E502)

        if not contact.user.is_active():
            LOG.w(f"User {contact.user} has been soft deleted")
            return (False, status.E502)

        context.contact = contact
        context.alias = contact.alias
        context.user = contact.user

        alias = context.alias
        if alias.custom_domain_id and not alias.custom_domain.verified:
            LOG.w("Alias %s is on unverified custom domain, refusing email", alias)
            return (False, status.E520)

        if alias.is_trashed():
            LOG.d("%s is trashed, do not forward", alias)
            return (False, status.E502)

        alias_address: str = alias.email
        alias_domain = get_email_domain_part(alias_address)
        context.alias_address = alias_address
        context.alias_domain = alias_domain

        if not is_valid_alias_address_domain(alias.email):
            LOG.e("%s domain isn't known", alias)
            return (False, status.E503)

        if not context.user.can_send_or_receive():
            LOG.i(f"User {context.user} cannot send emails")
            return (False, status.E504)

        return None

    def can_skip(self, context: EmailProcessingContext) -> bool:
        return False