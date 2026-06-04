from dataclasses import dataclass
from typing import Callable

from app import config
from app.email import status
from app.email_utils import get_email_domain_part, is_valid_alias_address_domain, should_ignore_bounce
from app.email_validation import normalize_reply_email
from app.handler.email_processing_context import EmailProcessingContext
from app.models import Alias, Contact, SLDomain
from app.log import LOG


class AliasResolutionStrategy:
    def handle(self, context: EmailProcessingContext):
        raise NotImplementedError


@dataclass
class AliasResolutionStep:
    strategy: AliasResolutionStrategy

    def process(self, context: EmailProcessingContext):
        return self.strategy.handle(context)


@dataclass
class ForwardAliasResolutionStrategy(AliasResolutionStrategy):
    try_auto_create: Callable[[str], Alias | None]
    handle_email_sent_to_ourself: Callable[..., None]

    def handle(self, context: EmailProcessingContext):
        alias_address = context.rcpt_to
        alias = Alias.get_by(email=alias_address)
        if not alias:
            LOG.d(
                "alias %s not exist. Try to see if it can be created on the fly",
                alias_address,
            )
            alias = self.try_auto_create(alias_address)
            if not alias:
                LOG.d("alias %s cannot be created on-the-fly, return 550", alias_address)
                if should_ignore_bounce(context.envelope.mail_from):
                    return [(True, status.E207)]
                return [(False, status.E515)]

        user = alias.user

        if not user.is_active():
            LOG.w(f"User {user} has been soft deleted")
            return [(False, status.E502)]

        if not user.can_send_or_receive():
            LOG.i(f"User {user} cannot receive emails")
            if should_ignore_bounce(context.envelope.mail_from):
                return [(True, status.E207)]
            return [(False, status.E504)]

        if alias.custom_domain_id and not alias.custom_domain.verified:
            LOG.w("Alias %s is on unverified custom domain, refusing email", alias)
            return [(False, status.E520)]

        context.alias = alias
        context.user = user
        context.mail_from = context.envelope.mail_from

        for addr in alias.authorized_addresses():
            if addr == context.mail_from:
                LOG.i("cycle email sent from %s to %s", addr, alias)
                self.handle_email_sent_to_ourself(alias, addr, context.msg, user)
                return [(True, status.E209)]

        return None


class ReplyAliasResolutionStrategy(AliasResolutionStrategy):
    def handle(self, context: EmailProcessingContext):
        reply_email = context.rcpt_to
        reply_domain = get_email_domain_part(reply_email)

        if not reply_email.endswith(config.EMAIL_DOMAIN):
            sl_domain: SLDomain = SLDomain.get_by(domain=reply_domain)
            if sl_domain is None:
                LOG.w(f"Reply email {reply_email} has wrong domain")
                return False, status.E501

        reply_email = normalize_reply_email(reply_email)

        contact = Contact.get_by(reply_email=reply_email)
        if not contact:
            LOG.w(f"No contact with {reply_email} as reverse alias")
            return False, status.E502
        if not contact.user.is_active():
            LOG.w(f"User {contact.user} has been soft deleted")
            return False, status.E502

        alias = contact.alias

        if alias.custom_domain_id and not alias.custom_domain.verified:
            LOG.w("Alias %s is on unverified custom domain, refusing email", alias)
            return False, status.E520

        if alias.is_trashed():
            LOG.d("%s is trashed, do not forward", alias)
            return False, status.E502

        alias_address = contact.alias.email
        alias_domain = get_email_domain_part(alias_address)

        if not is_valid_alias_address_domain(alias.email):
            LOG.e("%s domain isn't known", alias)
            return False, status.E503

        user = alias.user

        if not user.can_send_or_receive():
            LOG.i(f"User {user} cannot send emails")
            return False, status.E504

        context.reply_email = reply_email
        context.contact = contact
        context.alias = alias
        context.user = user
        context.alias_address = alias_address
        context.alias_domain = alias_domain
        return None
