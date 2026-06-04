"""
Security validation step for email processing.
"""

from typing import Tuple, Optional
import logging

from app.models import EmailLog, BlockBehaviourEnum
from app.email import status
from .protocol import EmailProcessingStep
from .context import EmailProcessingContext

LOG = logging.getLogger(__name__)


class SecurityValidationStep:
    """
    Step for security validations (user active, alias enabled, etc.).
    """

    def process(self, context: EmailProcessingContext) -> Tuple[bool, Optional[str]]:
        """
        Perform security validations.
        """
        if context.phase == "forward":
            return self._process_forward(context)
        else:
            return self._process_reply(context)

    def _process_forward(self, context: EmailProcessingContext) -> Tuple[bool, Optional[str]]:
        """
        Security validations for forward phase.
        """
        # Check user active
        if not context.user.is_active():
            LOG.warning(f"User {context.user} has been soft deleted")
            context.results = [(False, status.E502)]
            return True, None

        if not context.user.can_send_or_receive():
            LOG.info(f"User {context.user} cannot receive emails")
            if should_ignore_bounce(context.envelope.mail_from):
                context.results = [(True, status.E207)]
            else:
                context.results = [(False, status.E504)]
            return True, None

        # Check custom domain verified
        if context.alias.custom_domain_id and not context.alias.custom_domain.verified:
            LOG.warning("Alias %s is on unverified custom domain, refusing email", context.alias)
            context.results = [(False, status.E520)]
            return True, None

        # Check if email is sent to ourselves
        mail_from = context.envelope.mail_from
        for addr in context.alias.authorized_addresses():
            if addr == mail_from:
                LOG.info("cycle email sent from %s to %s", addr, context.alias)
                from app.email_handler import handle_email_sent_to_ourself
                handle_email_sent_to_ourself(context.alias, addr, context.msg, context.user)
                context.results = [(True, status.E209)]
                return True, None

        # Check user pending deletion
        if context.alias.user.delete_on is not None:
            LOG.debug(f"user {context.user} is pending to be deleted. Do not forward")
            EmailLog.create(
                contact_id=context.contact.id,
                user_id=context.contact.user_id,
                blocked=True,
                alias_id=context.contact.alias_id,
                commit=True,
            )
            context.results = [(True, status.E502)]
            return True, None

        # Check alias enabled
        if not context.alias.enabled or context.alias.is_trashed() or context.contact.block_forward:
            if not context.alias.enabled:
                LOG.debug("%s is disabled, do not forward", context.alias)

            if context.alias.is_trashed():
                LOG.debug("%s is trashed, do not forward", context.alias)

            if context.contact.block_forward:
                LOG.debug("Contact %s of alias %s is blocked, do not forward", context.contact, context.alias)

            EmailLog.create(
                contact_id=context.contact.id,
                user_id=context.contact.user_id,
                blocked=True,
                alias_id=context.contact.alias_id,
                commit=True,
            )

            res_status = status.E200
            if context.user.block_behaviour == BlockBehaviourEnum.return_5xx:
                res_status = status.E502

            context.results = [(True, res_status)]
            return True, None

        # DMARC check
        from app.handler.dmarc import apply_dmarc_policy_for_forward_phase
        msg, dmarc_delivery_status = apply_dmarc_policy_for_forward_phase(
            context.alias, context.contact, context.envelope, context.msg
        )
        if dmarc_delivery_status is not None:
            context.results = [(False, dmarc_delivery_status)]
            return True, None

        context.msg = msg
        context.mailboxes = context.alias.mailboxes

        # Check valid mailboxes
        if not context.mailboxes:
            LOG.warning("no valid mailboxes for %s", context.alias)
            if should_ignore_bounce(context.envelope.mail_from):
                context.results = [(True, status.E207)]
            else:
                context.results = [(False, status.E516)]
            return True, None

        return True, None

    def _process_reply(self, context: EmailProcessingContext) -> Tuple[bool, Optional[str]]:
        """
        Security validations for reply phase.
        """
        # Check user active
        if not context.user.is_active():
            LOG.warning(f"User {context.user} has been soft deleted")
            context.results = [(False, status.E502)]
            return True, None

        # Check custom domain verified
        if context.alias.custom_domain_id and not context.alias.custom_domain.verified:
            LOG.warning("Alias %s is on unverified custom domain, refusing email", context.alias)
            context.results = [(False, status.E520)]
            return True, None

        # Check alias not trashed
        if context.alias.is_trashed():
            LOG.debug("%s is trashed, do not forward", context.alias)
            context.results = [(False, status.E502)]
            return True, None

        # Check valid alias address
        from app.email_utils import is_valid_alias_address_domain
        if not is_valid_alias_address_domain(context.alias.email):
            LOG.error("%s domain isn't known", context.alias)
            context.results = [(False, status.E503)]
            return True, None

        # Check user can send/receive
        if not context.user.can_send_or_receive():
            LOG.info(f"User {context.user} cannot send emails")
            context.results = [(False, status.E504)]
            return True, None

        # DMARC check
        from app.handler.dmarc import apply_dmarc_policy_for_reply_phase
        dmarc_delivery_status = apply_dmarc_policy_for_reply_phase(
            context.alias, context.contact, context.envelope, context.msg
        )
        if dmarc_delivery_status is not None:
            context.results = [(False, dmarc_delivery_status)]
            return True, None

        # Anti-spoofing
        from app.mailbox_utils import get_mailbox_for_reply_phase
        from app.email_utils import get_header_unicode
        mailbox = get_mailbox_for_reply_phase(
            context.envelope.mail_from, get_header_unicode(context.msg["From"]), context.alias
        )
        if not mailbox:
            if context.alias.disable_email_spoofing_check:
                LOG.warning(
                    "ignore unknown sender to reverse-alias %s: %s -> %s",
                    context.envelope.mail_from,
                    context.alias,
                    context.contact,
                )
                mailbox = context.alias.mailbox
            else:
                from app.email_handler import handle_unknown_mailbox
                handle_unknown_mailbox(context.envelope, context.msg, context.rcpt_to, context.user, context.alias, context.contact)
                context.results = [(False, status.E214)]
                return True, None

        context.mailbox = mailbox

        # Check mailbox admin disabled
        if context.mailbox.is_admin_disabled():
            LOG.info(f"User {context.user} tried to send a mail from admin disabled mailbox {context.mailbox}")
            context.results = [(False, status.E207)]
            return True, None

        # SPF check
        from app.email_utils import spf_pass
        if (
            config.ENFORCE_SPF
            and context.mailbox.force_spf
            and not context.alias.disable_email_spoofing_check
        ):
            if not spf_pass(context.envelope, context.mailbox, context.user, context.alias, context.contact.website_email, context.msg):
                context.results = [(True, status.E201)]
                return True, None

        return True, None

    def rollback(self, context: EmailProcessingContext) -> None:
        """
        No rollback needed for security validation.
        """
        pass


def should_ignore_bounce(mail_from: str) -> bool:
    """
    Check if bounce should be ignored.
    """
    return mail_from == "<>"


from app import config
