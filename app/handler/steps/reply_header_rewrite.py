from typing import Optional, Tuple, List, Set

import newrelic.agent
import sentry_sdk
from email.message import Message
from email.utils import getaddresses, formatdate, make_msgid
from sqlalchemy.exc import IntegrityError

from app import config
from app.db import Session
from app.email import headers, status
from app.email_utils import (
    delete_all_headers_except,
    get_header_unicode,
    add_or_replace_header,
    delete_header,
    sl_formataddr,
    replace,
    get_email_domain_part,
    should_add_dkim_signature,
    add_dkim_signature,
    remove_sender_pgp_key_attachment,
)
from app.alias_utils import get_alias_recipient_name
from app.handler.steps import EmailProcessingStep, EmailProcessingContext
from app.log import LOG
from app.models import Alias, Contact, EmailLog, MessageIDMatching
from app.errors import NonReverseAliasInReplyPhase


@sentry_sdk.trace
def replace_header_when_reply(msg: Message, alias: Alias, header: str):
    new_addrs: List[str] = []
    headers_list = msg.get_all(header, [])
    headers_list = [str(h) for h in headers_list]

    headers_list = [h.replace("\r", "") for h in headers_list]
    headers_list = [h.replace("\n", "") for h in headers_list]

    for _, reply_email in getaddresses(headers_list):
        if reply_email == alias.email:
            continue

        contact = Contact.get_by(reply_email=reply_email)
        if not contact:
            LOG.w(
                "email %s contained in %s header in reply phase must be reply emails. headers:%s",
                reply_email,
                header,
                headers_list,
            )
            raise NonReverseAliasInReplyPhase(reply_email)
        else:
            new_addrs.append(sl_formataddr((contact.name, contact.website_email)))

    if new_addrs:
        new_header = ",".join(new_addrs)
        LOG.d("Replace %s header, old: %s, new: %s", header, msg[header], new_header)
        add_or_replace_header(msg, header, new_header)
    else:
        LOG.d("delete the %s header. Old value %s", header, msg[header])
        delete_header(msg, header)


class ReplyHeaderRewriteStep:
    name = "reply_header_rewrite"

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        msg = context.msg
        alias = context.alias
        contact = context.contact
        user = context.user
        mailbox = context.mailbox
        reply_email = context.reply_email

        delete_all_headers_except(
            msg,
            [
                headers.FROM,
                headers.TO,
                headers.CC,
                headers.SUBJECT,
                headers.DATE,
                headers.MESSAGE_ID,
                headers.REFERENCES,
                headers.IN_REPLY_TO,
                headers.SL_QUEUE_ID,
            ]
            + headers.MIME_HEADERS,
        )

        if config.DROP_PGP_KEY_ATTACHMENTS_ON_REPLY:
            msg = remove_sender_pgp_key_attachment(msg)

        context.orig_to = msg[headers.TO]
        context.orig_cc = msg[headers.CC]

        if user.replace_reverse_alias:
            LOG.d("Replace reverse-alias %s by contact email %s", reply_email, contact)
            msg = replace(msg, reply_email, contact.website_email)
            LOG.d("Replace mailbox %s by alias email %s", mailbox.email, alias.email)
            msg = replace(msg, mailbox.email, alias.email)

            if config.ENABLE_ALL_REVERSE_ALIAS_REPLACEMENT:
                import time
                start = time.time()

                contact_query = (
                    Contact.query()
                    .filter(Contact.alias_id == alias.id)
                    .limit(config.MAX_NB_REVERSE_ALIAS_REPLACEMENT)
                )

                for reply_email_val, website_email in contact_query.values(
                    Contact.reply_email, Contact.website_email
                ):
                    msg = replace(msg, reply_email_val, website_email)

                elapsed = time.time() - start
                LOG.d(
                    "Replace reverse alias by real address for %s contacts takes %s seconds",
                    contact_query.count(),
                    elapsed,
                )
                newrelic.agent.record_custom_metric(
                    "Custom/reverse_alias_replacement_time", elapsed
                )

        context.msg = msg
        return None

    def can_skip(self, context: EmailProcessingContext) -> bool:
        return context.alias is None or context.contact is None


class ReplyFromHeaderStep:
    name = "reply_from_header"

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        msg = context.msg
        alias = context.alias
        email_log = context.email_log

        recipient_name = get_alias_recipient_name(alias)
        if recipient_name.message:
            LOG.d(recipient_name.message)
        LOG.d("From header is %s", recipient_name.name)
        add_or_replace_header(msg, headers.FROM, recipient_name.name)

        try:
            if str(msg[headers.TO]).lower() == "undisclosed-recipients:;":
                LOG.d("email is sent in BCC mode")
            else:
                replace_header_when_reply(msg, alias, headers.TO)

            replace_header_when_reply(msg, alias, headers.CC)
        except NonReverseAliasInReplyPhase as e:
            LOG.w("non reverse-alias in reply %s %s %s", e, context.contact, alias)

            EmailLog.delete(email_log.id, commit=True)

            if context.mailbox.can_send_or_receive():
                from app.email_utils import send_email, render
                send_email(
                    context.mailbox.email,
                    f"Email sent to {context.contact.email} contains non reverse-alias addresses",
                    render(
                        "transactional/non-reverse-alias-reply-phase.txt.jinja2",
                        user=alias.user,
                        destination=context.contact.email,
                        alias=alias.email,
                        subject=msg[headers.SUBJECT],
                    ),
                )
            return (True, status.E200)

        context.msg = msg
        return None

    def can_skip(self, context: EmailProcessingContext) -> bool:
        return context.email_log is None


class ReplyMessageIdStep:
    name = "reply_message_id"

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        msg = context.msg
        alias = context.alias
        email_log = context.email_log

        replace_original_message_id(alias, email_log, msg)

        if not msg[headers.DATE]:
            date_header = formatdate()
            LOG.w("missing date header, add one")
            msg[headers.DATE] = date_header

        msg[headers.SL_DIRECTION] = "Reply"
        msg[headers.SL_EMAIL_LOG_ID] = str(email_log.id)

        context.msg = msg
        return None

    def can_skip(self, context: EmailProcessingContext) -> bool:
        return context.email_log is None


@sentry_sdk.trace
def replace_original_message_id(alias: Alias, email_log: EmailLog, msg: Message):
    original_message_id = msg[headers.MESSAGE_ID]
    if original_message_id:
        matching = MessageIDMatching.get_by(original_message_id=original_message_id)
        if matching:
            sl_message_id = matching.sl_message_id
            LOG.d("reuse the sl_message_id %s", sl_message_id)
        else:
            sl_message_id = make_msgid(
                str(email_log.id), get_email_domain_part(alias.email)
            )
            LOG.d("create a new sl_message_id %s", sl_message_id)
            try:
                MessageIDMatching.create(
                    sl_message_id=sl_message_id,
                    original_message_id=original_message_id,
                    email_log_id=email_log.id,
                    commit=True,
                )
            except IntegrityError:
                LOG.w(
                    "another matching with original_message_id %s was created in the mean time",
                    original_message_id,
                )
                Session.rollback()
                matching = MessageIDMatching.get_by(
                    original_message_id=original_message_id
                )
                sl_message_id = matching.sl_message_id
    else:
        sl_message_id = make_msgid(
            str(email_log.id), get_email_domain_part(alias.email)
        )
        LOG.d("no original_message_id, create a new sl_message_id %s", sl_message_id)

    del msg[headers.MESSAGE_ID]
    msg[headers.MESSAGE_ID] = sl_message_id

    email_log.sl_message_id = sl_message_id
    Session.commit()

    if msg[headers.REFERENCES]:
        message_ids = str(msg[headers.REFERENCES]).split()
        new_message_ids = []
        for message_id in message_ids:
            matching = MessageIDMatching.get_by(original_message_id=message_id)
            if matching:
                LOG.d(
                    "replace original message id by SL one, %s -> %s",
                    message_id,
                    matching.sl_message_id,
                )
                new_message_ids.append(matching.sl_message_id)
            else:
                new_message_ids.append(message_id)

        del msg[headers.REFERENCES]
        msg[headers.REFERENCES] = " ".join(new_message_ids)