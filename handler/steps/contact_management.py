"""
Contact management step for email processing.
"""

from typing import Tuple, Optional
import logging

from app.models import Contact, Alias
from app.email_utils import get_header_unicode, parse_full_address
from app import contact_utils
from app.email import status
from .protocol import EmailProcessingStep
from .context import EmailProcessingContext

LOG = logging.getLogger(__name__)


class ContactManagementStep:
    """
    Step for managing contacts (create/update) during email processing.
    """

    def process(self, context: EmailProcessingContext) -> Tuple[bool, Optional[str]]:
        """
        Manage contact for the email.
        """
        if context.phase == "forward":
            return self._process_forward(context)
        else:
            return self._process_reply(context)

    def _process_forward(self, context: EmailProcessingContext) -> Tuple[bool, Optional[str]]:
        """
        Process contact for forward phase.
        """
        from_header = get_header_unicode(context.msg["From"])
        LOG.debug("Create or get contact for from_header:%s", from_header)
        contact = get_or_create_contact(from_header, context.envelope.mail_from, context.alias)
        
        if not contact:
            context.results = [(False, status.E504)]
            return True, None
        
        context.contact = contact
        # Refresh alias in case session was closed
        context.alias = contact.alias
        
        # Process Reply-To contacts
        reply_to_contacts = []
        if context.msg["Reply-To"]:
            reply_to_header_contents = get_header_unicode(context.msg["Reply-To"])
            if reply_to_header_contents:
                LOG.debug(
                    "Create or get contact for reply_to_header:%s", reply_to_header_contents
                )
                for reply_to in [
                    rt.strip()
                    for rt in reply_to_header_contents.split(",")
                    if rt.strip()
                ]:
                    try:
                        reply_to_name, reply_to_email = parse_full_address(reply_to)
                    except ValueError:
                        LOG.debug(f"Could not parse reply-to address {reply_to}")
                        continue
                    if reply_to_email == context.alias.email:
                        LOG.debug("Reply-to same as alias %s", context.alias)
                    else:
                        reply_contact = get_or_create_reply_to_contact(
                            reply_to_email, context.alias, context.msg
                        )
                        if reply_contact:
                            reply_to_contacts.append(reply_contact)
        
        context.reply_to_contacts = reply_to_contacts
        return True, None

    def _process_reply(self, context: EmailProcessingContext) -> Tuple[bool, Optional[str]]:
        """
        Process contact for reply phase.
        """
        from app.email_utils import normalize_reply_email
        from app.models import SLDomain
        
        reply_email = context.rcpt_to
        
        # Validate reply email domain
        if not reply_email.endswith(config.EMAIL_DOMAIN):
            sl_domain: SLDomain = SLDomain.get_by(domain=get_email_domain_part(reply_email))
            if sl_domain is None:
                LOG.warning(f"Reply email {reply_email} has wrong domain")
                context.results = [(False, status.E501)]
                return True, None
        
        # Normalize reply email
        reply_email = normalize_reply_email(reply_email)
        context.rcpt_to = reply_email
        
        # Get contact
        contact = Contact.get_by(reply_email=reply_email)
        if not contact:
            LOG.warning(f"No contact with {reply_email} as reverse alias")
            context.results = [(False, status.E502)]
            return True, None
        
        context.contact = contact
        context.alias = contact.alias
        context.user = contact.alias.user
        
        return True, None

    def rollback(self, context: EmailProcessingContext) -> None:
        """
        No rollback needed for contact management.
        """
        pass


# Reuse existing functions
from app.email_utils import get_email_domain_part
from app import config


def get_or_create_contact(
    from_header: str, mail_from: str, alias: Alias
) -> Optional[Contact]:
    """
    Reuse existing function from email_handler.
    """
    try:
        contact_name, contact_email = parse_full_address(from_header)
    except ValueError:
        contact_name, contact_email = "", ""

    # Ensure contact_name is within limits
    if len(contact_name) >= Contact.MAX_NAME_LENGTH:
        contact_name = contact_name[0 : Contact.MAX_NAME_LENGTH]

    if not is_valid_email(contact_email):
        # From header is wrongly formatted, try with mail_from
        if mail_from and mail_from != "<>":
            LOG.warning(
                "Cannot parse email from from_header %s, use mail_from %s",
                from_header,
                mail_from,
            )
            contact_email = mail_from
    contact_result = contact_utils.create_contact(
        email=contact_email,
        alias=alias,
        name=contact_name,
        mail_from=mail_from,
        allow_empty_email=True,
        automatic_created=True,
        from_partner=False,
    )
    if contact_result.error:
        LOG.warning(f"Error creating contact: {contact_result.error.value}")
    return contact_result.contact


def get_or_create_reply_to_contact(
    reply_to_header: str, alias: Alias, msg
) -> Optional[Contact]:
    """
    Reuse existing function from email_handler.
    """
    try:
        contact_name, contact_address = parse_full_address(reply_to_header)
    except ValueError:
        return None

    if len(contact_name) >= Contact.MAX_NAME_LENGTH:
        contact_name = contact_name[0 : Contact.MAX_NAME_LENGTH]

    if not is_valid_email(contact_address):
        LOG.warning(
            "invalid reply-to address %s. Parse from %s",
            contact_address,
            reply_to_header,
        )
        return None

    return contact_utils.create_contact(
        contact_address, alias, contact_name, automatic_created=True
    ).contact


from app.email_validation import is_valid_email
