from typing import Optional, Tuple, List

import sentry_sdk
from email.message import Message
from email.utils import getaddresses
from email_validator import validate_email, EmailNotValidError
from flanker.addresslib import address
from flanker.addresslib.address import EmailAddress
from sqlalchemy.exc import IntegrityError

from app.db import Session
from app.email import headers, status
from app.email_utils import (
    get_header_unicode,
    add_or_replace_header,
    delete_header,
    generate_reply_email,
    sanitize_email,
)
from app.handler.steps import EmailProcessingStep, EmailProcessingContext
from app.log import LOG
from app.models import Alias, Contact
from app.errors import CannotCreateContactForReverseAlias


@sentry_sdk.trace
def replace_header_when_forward(msg: Message, alias: Alias, header: str):
    new_addrs: List[str] = []
    headers_list = msg.get_all(header, [])
    headers_list = [get_header_unicode(h) for h in headers_list]

    full_addresses: List[EmailAddress] = []
    for h in headers_list:
        full_addresses += address.parse_list(h)

    for full_address in full_addresses:
        contact_email = sanitize_email(full_address.address, not_lower=True)

        if contact_email.lower() == alias.email:
            new_addrs.append(full_address.full_spec())
            continue

        try:
            validate_email(
                contact_email, check_deliverability=False, allow_smtputf8=False
            )
        except EmailNotValidError:
            LOG.w("invalid contact email %s. %s. Skip", contact_email, headers_list)
            continue

        contact = Contact.get_by(alias_id=alias.id, website_email=contact_email)
        contact_name = full_address.display_name
        if len(contact_name) >= Contact.MAX_NAME_LENGTH:
            contact_name = contact_name[0 : Contact.MAX_NAME_LENGTH]

        if contact:
            if contact.name != full_address.display_name:
                LOG.d(
                    "Update contact %s name %s to %s",
                    contact,
                    contact.name,
                    contact_name,
                )
                contact.name = contact_name
                Session.commit()
        else:
            LOG.d(
                "create contact for alias %s and email %s, header %s",
                alias,
                contact_email,
                header,
            )
            try:
                contact = Contact.create(
                    user_id=alias.user_id,
                    alias_id=alias.id,
                    website_email=contact_email,
                    name=contact_name,
                    reply_email=generate_reply_email(contact_email, alias),
                    is_cc=header.lower() == "cc",
                    automatic_created=True,
                )
                Session.commit()
            except IntegrityError:
                LOG.w("Contact %s %s already exist", alias, contact_email)
                Session.rollback()
                contact = Contact.get_by(alias_id=alias.id, website_email=contact_email)

        new_addrs.append(contact.new_addr())

    if new_addrs:
        new_header = ",".join(new_addrs)
        LOG.d("Replace %s header, old: %s, new: %s", header, msg[header], new_header)
        add_or_replace_header(msg, header, new_header)
    else:
        LOG.d("Delete %s header, old value %s", header, msg[header])
        delete_header(msg, header)


@sentry_sdk.trace
def add_alias_to_header_if_needed(msg: Message, alias: Alias):
    to_header = str(msg[headers.TO]) if msg[headers.TO] else None
    cc_header = str(msg[headers.CC]) if msg[headers.CC] else None

    if to_header and alias.email in to_header:
        return

    if cc_header and alias.email in cc_header:
        return

    LOG.d(f"add {alias} to To: header {to_header}")

    if to_header:
        add_or_replace_header(msg, headers.TO, f"{to_header},{alias.email}")
    else:
        add_or_replace_header(msg, headers.TO, alias.email)


class HeaderRewriteStep:
    name = "header_rewrite"

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        msg = context.msg
        alias = context.alias
        contact = context.contact
        user = context.user
        mailbox = context.mailbox

        headers_to_keep = [
            headers.FROM,
            headers.TO,
            headers.CC,
            headers.SUBJECT,
            headers.DATE,
            headers.MESSAGE_ID,
            headers.REFERENCES,
            headers.IN_REPLY_TO,
            headers.SL_QUEUE_ID,
            headers.LIST_UNSUBSCRIBE,
            headers.LIST_ID,
            headers.LIST_UNSUBSCRIBE_POST,
        ] + headers.MIME_HEADERS

        if user.include_header_email_header:
            headers_to_keep.append(headers.AUTHENTICATION_RESULTS)

        from app.email_utils import delete_all_headers_except
        delete_all_headers_except(msg, headers_to_keep)

        if mailbox.generic_subject:
            LOG.d("Use a generic subject for %s", mailbox)
            from app.email_utils import get_header_unicode, add_header
            orig_subject = msg[headers.SUBJECT]
            orig_subject = get_header_unicode(orig_subject)
            add_or_replace_header(msg, "Subject", mailbox.generic_subject)
            sender = msg[headers.FROM]
            sender = get_header_unicode(sender)
            msg = add_header(
                msg,
                f"""Forwarded by SimpleLogin to {alias.email} from "{sender}" with "{orig_subject}" as subject""",
                f"""Forwarded by SimpleLogin to {alias.email} from "{sender}" with <b>{orig_subject}</b> as subject""",
            )

        if contact.invalid_email:
            LOG.d("add noreply information %s %s", alias, mailbox)
            from app.email_utils import add_header
            msg = add_header(
                msg,
                f"""Email sent to {alias.email} from an invalid address and cannot be replied""",
                f"""Email sent to {alias.email} from an invalid address and cannot be replied""",
            )

        add_or_replace_header(msg, headers.SL_DIRECTION, "Forward")

        msg[headers.SL_EMAIL_LOG_ID] = str(context.email_log.id)
        if user.include_header_email_header:
            msg[headers.SL_ENVELOPE_FROM] = context.envelope.mail_from
            if contact.name:
                original_from = f"{contact.name} <{contact.website_email}>"
            else:
                original_from = contact.website_email
            msg[headers.SL_ORIGINAL_FROM] = original_from

        msg[headers.SL_ENVELOPE_TO] = alias.email

        if not msg[headers.DATE]:
            LOG.w("missing date header, create one")
            from email.utils import formatdate
            msg[headers.DATE] = formatdate()

        from email_handler import replace_sl_message_id_by_original_message_id
        replace_sl_message_id_by_original_message_id(msg)

        old_from_header = msg[headers.FROM]
        new_from_header = contact.new_addr()
        add_or_replace_header(msg, "From", new_from_header)
        LOG.d("From header, new:%s, old:%s", new_from_header, old_from_header)

        if len(context.reply_to_contacts) > 0:
            original_reply_to = get_header_unicode(msg[headers.REPLY_TO])
            new_reply_to_header = ", ".join(
                [reply_to_contact.new_addr() for reply_to_contact in context.reply_to_contacts][:5]
            )
            add_or_replace_header(msg, "Reply-To", new_reply_to_header)
            LOG.d("Reply-To header, new:%s, old:%s", new_reply_to_header, original_reply_to)

        from app.email.checks import check_recipient_limit
        if not check_recipient_limit(msg, config.MAX_EMAIL_FORWARD_RECIPIENTS):
            return (False, status.E526)

        try:
            replace_header_when_forward(msg, alias, headers.CC)
            replace_header_when_forward(msg, alias, headers.TO)
        except CannotCreateContactForReverseAlias:
            LOG.d("CannotCreateContactForReverseAlias error, delete %s", context.email_log)
            EmailLog.delete(context.email_log.id)
            Session.commit()
            raise

        add_alias_to_header_if_needed(msg, alias)

        from app.handler.unsubscribe_generator import UnsubscribeGenerator
        msg = UnsubscribeGenerator().add_header_to_message(alias, contact, msg)

        context.msg = msg
        return None

    def can_skip(self, context: EmailProcessingContext) -> bool:
        return context.email_log is None


from app import config
from app.models import EmailLog