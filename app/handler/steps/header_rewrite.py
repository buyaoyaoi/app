from dataclasses import dataclass
from email.utils import formatdate
from typing import Callable

from app import config
from app.db import Session
from app.email import headers, status
from app.email.checks import check_recipient_limit
from app.email_utils import (
    add_dkim_signature,
    add_header,
    add_or_replace_header,
    delete_all_headers_except,
    get_header_unicode,
    remove_sender_pgp_key_attachment,
    replace,
)
from app.errors import CannotCreateContactForReverseAlias, NonReverseAliasInReplyPhase
from app.handler.email_processing_context import EmailProcessingContext
from app.handler.unsubscribe_generator import UnsubscribeGenerator
from app.log import LOG
from app.models import EmailLog


class HeaderRewriteStrategy:
    def handle(self, context: EmailProcessingContext):
        raise NotImplementedError


@dataclass
class PreHeaderRewriteStep:
    strategy: HeaderRewriteStrategy

    def process(self, context: EmailProcessingContext):
        return self.strategy.handle(context)


@dataclass
class PostHeaderRewriteStep:
    strategy: HeaderRewriteStrategy

    def process(self, context: EmailProcessingContext):
        return self.strategy.handle(context)


class ForwardPreHeaderRewriteStrategy(HeaderRewriteStrategy):
    def handle(self, context: EmailProcessingContext):
        if context.contact.invalid_email:
            LOG.d("add noreply information %s %s", context.alias, context.mailbox)
            context.msg = add_header(
                context.msg,
                f"""Email sent to {context.alias.email} from an invalid address and cannot be replied""",
                f"""Email sent to {context.alias.email} from an invalid address and cannot be replied""",
            )

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
        if context.user.include_header_email_header:
            headers_to_keep.append(headers.AUTHENTICATION_RESULTS)
        delete_all_headers_except(context.msg, headers_to_keep)

        if context.mailbox.generic_subject:
            LOG.d("Use a generic subject for %s", context.mailbox)
            orig_subject = get_header_unicode(context.msg[headers.SUBJECT])
            add_or_replace_header(context.msg, "Subject", context.mailbox.generic_subject)
            sender = get_header_unicode(context.msg[headers.FROM])
            context.msg = add_header(
                context.msg,
                f"""Forwarded by SimpleLogin to {context.alias.email} from \"{sender}\" with \"{orig_subject}\" as subject""",
                f"""Forwarded by SimpleLogin to {context.alias.email} from \"{sender}\" with <b>{orig_subject}</b> as subject""",
            )

        return None


@dataclass
class ForwardPostHeaderRewriteStrategy(HeaderRewriteStrategy):
    replace_sl_message_id_by_original_message_id: Callable[[object], None]
    replace_header_when_forward: Callable[[object, object, str], None]
    add_alias_to_header_if_needed: Callable[[object, object], None]

    def handle(self, context: EmailProcessingContext):
        context.msg[headers.SL_DIRECTION] = "Forward"
        context.msg[headers.SL_EMAIL_LOG_ID] = str(context.email_log.id)
        if context.user.include_header_email_header:
            context.msg[headers.SL_ENVELOPE_FROM] = context.envelope.mail_from
            if context.contact.name:
                original_from = f"{context.contact.name} <{context.contact.website_email}>"
            else:
                original_from = context.contact.website_email
            context.msg[headers.SL_ORIGINAL_FROM] = original_from
        context.msg[headers.SL_ENVELOPE_TO] = context.alias.email

        if not context.msg[headers.DATE]:
            LOG.w("missing date header, create one")
            context.msg[headers.DATE] = formatdate()

        self.replace_sl_message_id_by_original_message_id(context.msg)

        old_from_header = context.msg[headers.FROM]
        new_from_header = context.contact.new_addr()
        add_or_replace_header(context.msg, "From", new_from_header)
        LOG.d("From header, new:%s, old:%s", new_from_header, old_from_header)

        if len(context.reply_to_contacts) > 0:
            original_reply_to = get_header_unicode(context.msg[headers.REPLY_TO])
            new_reply_to_header = ", ".join(
                [reply_to_contact.new_addr() for reply_to_contact in context.reply_to_contacts][:5]
            )
            add_or_replace_header(context.msg, "Reply-To", new_reply_to_header)
            LOG.d(
                "Reply-To header, new:%s, old:%s",
                new_reply_to_header,
                original_reply_to,
            )

        if not check_recipient_limit(context.msg, config.MAX_EMAIL_FORWARD_RECIPIENTS):
            return False, status.E526

        try:
            self.replace_header_when_forward(context.msg, context.alias, headers.CC)
            self.replace_header_when_forward(context.msg, context.alias, headers.TO)
        except CannotCreateContactForReverseAlias:
            LOG.d("CannotCreateContactForReverseAlias error, delete %s", context.email_log)
            EmailLog.delete(context.email_log.id)
            Session.commit()
            raise

        self.add_alias_to_header_if_needed(context.msg, context.alias)
        context.msg = UnsubscribeGenerator().add_header_to_message(
            context.alias, context.contact, context.msg
        )
        add_dkim_signature(context.msg, config.EMAIL_DOMAIN)
        return None


class ReplyPreHeaderRewriteStrategy(HeaderRewriteStrategy):
    def handle(self, context: EmailProcessingContext):
        delete_all_headers_except(
            context.msg,
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
            context.msg = remove_sender_pgp_key_attachment(context.msg)

        context.orig_to = context.msg[headers.TO]
        context.orig_cc = context.msg[headers.CC]

        if context.user.replace_reverse_alias:
            LOG.d(
                "Replace reverse-alias %s by contact email %s",
                context.reply_email,
                context.contact,
            )
            context.msg = replace(
                context.msg, context.reply_email, context.contact.website_email
            )
            LOG.d("Replace mailbox %s by alias email %s", context.mailbox.email, context.alias.email)
            context.msg = replace(context.msg, context.mailbox.email, context.alias.email)

            if config.ENABLE_ALL_REVERSE_ALIAS_REPLACEMENT:
                from app.models import Contact
                import time
                import newrelic.agent

                start = time.time()
                contact_query = (
                    Contact.query()
                    .filter(Contact.alias_id == context.alias.id)
                    .limit(config.MAX_NB_REVERSE_ALIAS_REPLACEMENT)
                )

                for reply_email, website_email in contact_query.values(
                    Contact.reply_email, Contact.website_email
                ):
                    context.msg = replace(context.msg, reply_email, website_email)

                elapsed = time.time() - start
                LOG.d(
                    "Replace reverse alias by real address for %s contacts takes %s seconds",
                    contact_query.count(),
                    elapsed,
                )
                newrelic.agent.record_custom_metric(
                    "Custom/reverse_alias_replacement_time", elapsed
                )

        return None


@dataclass
class ReplyPostHeaderRewriteStrategy(HeaderRewriteStrategy):
    get_alias_recipient_name: Callable[[object], object]
    replace_header_when_reply: Callable[[object, object, str], None]
    replace_original_message_id: Callable[[object, object, object], None]
    send_email: Callable[..., None]
    render: Callable[..., str]

    def handle(self, context: EmailProcessingContext):
        recipient_name = self.get_alias_recipient_name(context.alias)
        if recipient_name.message:
            LOG.d(recipient_name.message)
        LOG.d("From header is %s", recipient_name.name)
        add_or_replace_header(context.msg, headers.FROM, recipient_name.name)

        try:
            if str(context.msg[headers.TO]).lower() == "undisclosed-recipients:;":
                LOG.d("email is sent in BCC mode")
            else:
                self.replace_header_when_reply(context.msg, context.alias, headers.TO)

            self.replace_header_when_reply(context.msg, context.alias, headers.CC)
        except NonReverseAliasInReplyPhase as exc:
            LOG.w("non reverse-alias in reply %s %s %s", exc, context.contact, context.alias)
            EmailLog.delete(context.email_log.id, commit=True)
            if context.mailbox.can_send_or_receive():
                self.send_email(
                    context.mailbox.email,
                    f"Email sent to {context.contact.email} contains non reverse-alias addresses",
                    self.render(
                        "transactional/non-reverse-alias-reply-phase.txt.jinja2",
                        user=context.alias.user,
                        destination=context.contact.email,
                        alias=context.alias.email,
                        subject=context.msg[headers.SUBJECT],
                    ),
                )
            return True, status.E200

        self.replace_original_message_id(context.alias, context.email_log, context.msg)

        if not context.msg[headers.DATE]:
            LOG.w("missing date header, add one")
            context.msg[headers.DATE] = formatdate()

        context.msg[headers.SL_DIRECTION] = "Reply"
        context.msg[headers.SL_EMAIL_LOG_ID] = str(context.email_log.id)

        if context.alias_domain and context.alias_domain:
            from app.email_utils import should_add_dkim_signature

            if should_add_dkim_signature(context.alias_domain):
                add_dkim_signature(context.msg, context.alias_domain)

        return None
