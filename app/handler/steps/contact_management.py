from typing import Optional, Tuple, List

import sentry_sdk
from email.message import Message

from app import contact_utils
from app.email import headers, status
from app.email_utils import (
    get_header_unicode,
    parse_full_address,
    should_ignore_bounce,
)
from app.email_validation import is_valid_email
from app.handler.steps import EmailProcessingStep, EmailProcessingContext
from app.log import LOG
from app.models import Alias, Contact


@sentry_sdk.trace
def get_or_create_contact(
    from_header: str, mail_from: str, alias: Alias
) -> Optional[Contact]:
    try:
        contact_name, contact_email = parse_full_address(from_header)
    except ValueError:
        contact_name, contact_email = "", ""

    if len(contact_name) >= Contact.MAX_NAME_LENGTH:
        contact_name = contact_name[0 : Contact.MAX_NAME_LENGTH]

    if not is_valid_email(contact_email):
        if mail_from and mail_from != "<>":
            LOG.w(
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
        LOG.w(f"Error creating contact: {contact_result.error.value}")
    return contact_result.contact


@sentry_sdk.trace
def get_or_create_reply_to_contact(
    reply_to_header: str, alias: Alias, msg: Message
) -> Optional[Contact]:
    try:
        contact_name, contact_address = parse_full_address(reply_to_header)
    except ValueError:
        return

    if len(contact_name) >= Contact.MAX_NAME_LENGTH:
        contact_name = contact_name[0 : Contact.MAX_NAME_LENGTH]

    if not is_valid_email(contact_address):
        LOG.w(
            "invalid reply-to address %s. Parse from %s",
            contact_address,
            reply_to_header,
        )
        return None

    return contact_utils.create_contact(
        contact_address, alias, contact_name, automatic_created=True
    ).contact


class ContactManagementStep:
    name = "contact_management"

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        from_header = get_header_unicode(context.msg[headers.FROM])
        LOG.d("Create or get contact for from_header:%s", from_header)
        context.from_header = from_header

        contact = get_or_create_contact(from_header, context.envelope.mail_from, context.alias)
        if not contact:
            return (False, status.E504)

        context.contact = contact
        context.alias = contact.alias

        reply_to_contacts: List[Contact] = []
        if context.msg[headers.REPLY_TO]:
            reply_to_header_contents = get_header_unicode(context.msg[headers.REPLY_TO])
            if reply_to_header_contents:
                LOG.d("Create or get contact for reply_to_header:%s", reply_to_header_contents)
                for reply_to in [
                    reply_to.strip()
                    for reply_to in reply_to_header_contents.split(",")
                    if reply_to.strip()
                ]:
                    try:
                        reply_to_name, reply_to_email = parse_full_address(reply_to)
                    except ValueError:
                        LOG.d(f"Could not parse reply-to address {reply_to}")
                        continue
                    if reply_to_email == context.alias.email:
                        LOG.i("Reply-to same as alias %s", context.alias)
                    else:
                        reply_contact = get_or_create_reply_to_contact(
                            reply_to_email, context.alias, context.msg
                        )
                        if reply_contact:
                            reply_to_contacts.append(reply_contact)

        context.reply_to_contacts = reply_to_contacts
        return None

    def can_skip(self, context: EmailProcessingContext) -> bool:
        return context.alias is None