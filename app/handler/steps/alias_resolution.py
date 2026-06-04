from __future__ import annotations

from app import config
from app.alias_utils import try_auto_create
from app.email import status
from app.email_utils import (
    get_email_domain_part,
    should_ignore_bounce,
    is_valid_alias_address_domain,
    normalize_reply_email,
)
from app.handler.steps.context import EmailProcessingContext, StepResult
from app.log import LOG
from app.models import Alias, Contact, SLDomain


class AliasResolutionStep:
    name = "alias_resolution"

    def process(self, ctx: EmailProcessingContext) -> StepResult:
        if ctx.phase == "forward":
            return self._process_forward(ctx)
        return self._process_reply(ctx)

    def _process_forward(self, ctx: EmailProcessingContext) -> StepResult:
        alias_address = ctx.rcpt_to
        alias = Alias.get_by(email=alias_address)

        if not alias:
            LOG.d(
                "alias %s not exist. Try to see if it can be created on the fly",
                alias_address,
            )
            alias = try_auto_create(alias_address)
            if not alias:
                LOG.d("alias %s cannot be created on-the-fly, return 550", alias_address)
                if should_ignore_bounce(ctx.envelope.mail_from):
                    return StepResult(early_return=(True, status.E207))
                else:
                    return StepResult(early_return=(False, status.E515))

        ctx.alias = alias
        ctx.user = alias.user

        if not ctx.user.is_active():
            LOG.w(f"User {ctx.user} has been soft deleted")
            return StepResult(early_return=(False, status.E502))

        if not ctx.user.can_send_or_receive():
            LOG.i(f"User {ctx.user} cannot receive emails")
            if should_ignore_bounce(ctx.envelope.mail_from):
                return StepResult(early_return=(True, status.E207))
            else:
                return StepResult(early_return=(False, status.E504))

        if alias.custom_domain_id and not alias.custom_domain.verified:
            LOG.w("Alias %s is on unverified custom domain, refusing email", alias)
            return StepResult(early_return=(False, status.E520))

        mail_from = ctx.envelope.mail_from
        for addr in alias.authorized_addresses():
            if addr == mail_from:
                LOG.i("cycle email sent from %s to %s", addr, alias)
                from email_handler import handle_email_sent_to_ourself

                handle_email_sent_to_ourself(alias, addr, ctx.msg, ctx.user)
                return StepResult(early_return=(True, status.E209))

        ctx.mailboxes = alias.mailboxes
        if not ctx.mailboxes:
            LOG.w("no valid mailboxes for %s", alias)
            if should_ignore_bounce(ctx.envelope.mail_from):
                return StepResult(early_return=(True, status.E207))
            else:
                return StepResult(early_return=(False, status.E516))

        return StepResult()

    def _process_reply(self, ctx: EmailProcessingContext) -> StepResult:
        reply_email = ctx.rcpt_to

        reply_domain = get_email_domain_part(reply_email)

        if not reply_email.endswith(config.EMAIL_DOMAIN):
            sl_domain: SLDomain | None = SLDomain.get_by(domain=reply_domain)
            if sl_domain is None:
                LOG.w(f"Reply email {reply_email} has wrong domain")
                return StepResult(early_return=(False, status.E501))

        reply_email = normalize_reply_email(reply_email)

        contact = Contact.get_by(reply_email=reply_email)
        if not contact:
            LOG.w(f"No contact with {reply_email} as reverse alias")
            return StepResult(early_return=(False, status.E502))
        if not contact.user.is_active():
            LOG.w(f"User {contact.user} has been soft deleted")
            return StepResult(early_return=(False, status.E502))

        ctx.contact = contact
        alias = contact.alias
        ctx.alias = alias

        if alias.custom_domain_id and not alias.custom_domain.verified:
            LOG.w("Alias %s is on unverified custom domain, refusing email", alias)
            return StepResult(early_return=(False, status.E520))

        if alias.is_trashed():
            LOG.d("%s is trashed, do not forward", alias)
            return StepResult(early_return=(False, status.E502))

        alias_address: str = contact.alias.email
        ctx.alias_domain = get_email_domain_part(alias_address)

        if not is_valid_alias_address_domain(alias.email):
            LOG.e("%s domain isn't known", alias)
            return StepResult(early_return=(False, status.E503))

        ctx.user = alias.user

        if not ctx.user.can_send_or_receive():
            LOG.i(f"User {ctx.user} cannot send emails")
            return StepResult(early_return=(False, status.E504))

        return StepResult()