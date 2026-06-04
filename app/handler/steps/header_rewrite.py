from __future__ import annotations

from app import config
from app.db import Session
from app.email import headers
from app.email_utils import (
    add_or_replace_header,
    get_header_unicode,
    delete_all_headers_except,
    add_header,
    add_dkim_signature,
    formatdate,
    replace_header_when_reply,
    replace,
    remove_sender_pgp_key_attachment,
    should_add_dkim_signature,
    render,
    send_email,
)
from app.handler.steps.context import EmailProcessingContext, StepResult
from app.log import LOG
from app.models import Contact, EmailLog
from app.errors import NonReverseAliasInReplyPhase
import newrelic.agent
import time


class HeaderRewriteStep:
    name = "header_rewrite"

    def process(self, ctx: EmailProcessingContext) -> StepResult:
        if ctx.phase in ("forward", "forward_per_mailbox"):
            return self._process_forward(ctx)
        return self._process_reply(ctx)

    def _process_forward(self, ctx: EmailProcessingContext) -> StepResult:
        msg = ctx.msg
        alias = ctx.alias
        contact = ctx.contact
        mailbox = ctx.mailbox
        user = ctx.user
        email_log = ctx.email_log
        reply_to_contacts = ctx.reply_to_contacts

        from email_handler import (
            replace_header_when_forward,
            add_alias_to_header_if_needed,
            replace_sl_message_id_by_original_message_id,
        )

        if contact.invalid_email:
            LOG.d("add noreply information %s %s", alias, mailbox)
            msg = add_header(
                msg,
                f"""Email sent to {alias.email} from an invalid address and cannot be replied""",
                f"""Email sent to {alias.email} from an invalid address and cannot be replied""",
            )
            ctx.msg = msg

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
        delete_all_headers_except(msg, headers_to_keep)

        if mailbox.generic_subject:
            LOG.d("Use a generic subject for %s", mailbox)
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

        add_or_replace_header(msg, headers.SL_DIRECTION, "Forward")
        msg[headers.SL_EMAIL_LOG_ID] = str(email_log.id)
        if user.include_header_email_header:
            msg[headers.SL_ENVELOPE_FROM] = ctx.envelope.mail_from
            if contact.name:
                original_from = f"{contact.name} <{contact.website_email}>"
            else:
                original_from = contact.website_email
            msg[headers.SL_ORIGINAL_FROM] = original_from
        msg[headers.SL_ENVELOPE_TO] = alias.email

        if not msg[headers.DATE]:
            LOG.w("missing date header, create one")
            msg[headers.DATE] = formatdate()

        replace_sl_message_id_by_original_message_id(msg)

        old_from_header = msg[headers.FROM]
        new_from_header = contact.new_addr()
        add_or_replace_header(msg, "From", new_from_header)
        LOG.d("From header, new:%s, old:%s", new_from_header, old_from_header)

        if len(reply_to_contacts) > 0:
            original_reply_to = get_header_unicode(msg[headers.REPLY_TO])
            new_reply_to_header = ", ".join(
                [
                    reply_to_contact.new_addr()
                    for reply_to_contact in reply_to_contacts
                ][:5]
            )
            add_or_replace_header(msg, "Reply-To", new_reply_to_header)
            LOG.d(
                "Reply-To header, new:%s, old:%s",
                new_reply_to_header,
                original_reply_to,
            )

        from app.email.checks import check_recipient_limit

        if not check_recipient_limit(msg, config.MAX_EMAIL_FORWARD_RECIPIENTS):
            return StepResult(early_return=(False, "526"))

        try:
            replace_header_when_forward(msg, alias, headers.CC)
            replace_header_when_forward(msg, alias, headers.TO)
        except Exception:
            from app.errors import CannotCreateContactForReverseAlias
            import sys

            if isinstance(sys.exc_info()[1], CannotCreateContactForReverseAlias):
                LOG.d("CannotCreateContactForReverseAlias error, delete %s", email_log)
                EmailLog.delete(email_log.id)
                Session.commit()
                raise

        add_alias_to_header_if_needed(msg, alias)

        from app.handler.unsubscribe_generator import UnsubscribeGenerator

        msg = UnsubscribeGenerator().add_header_to_message(alias, contact, msg)

        add_dkim_signature(msg, config.EMAIL_DOMAIN)

        ctx.msg = msg
        return StepResult()

    def _process_reply(self, ctx: EmailProcessingContext) -> StepResult:
        msg = ctx.msg
        alias = ctx.alias
        contact = ctx.contact
        user = ctx.user
        mailbox = ctx.mailbox
        email_log = ctx.email_log

        from email_handler import replace_original_message_id

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

        ctx.orig_to = msg[headers.TO]
        ctx.orig_cc = msg[headers.CC]

        if user.replace_reverse_alias:
            LOG.d(
                "Replace reverse-alias %s by contact email %s",
                ctx.rcpt_to,
                contact,
            )
            msg = replace(msg, ctx.rcpt_to, contact.website_email)
            LOG.d(
                "Replace mailbox %s by alias email %s",
                mailbox.email,
                alias.email,
            )
            msg = replace(msg, mailbox.email, alias.email)

            if config.ENABLE_ALL_REVERSE_ALIAS_REPLACEMENT:
                start = time.time()
                contact_query = (
                    Contact.query()
                    .filter(Contact.alias_id == alias.id)
                    .limit(config.MAX_NB_REVERSE_ALIAS_REPLACEMENT)
                )

                for reply_email, website_email in contact_query.values(
                    Contact.reply_email, Contact.website_email
                ):
                    msg = replace(msg, reply_email, website_email)

                elapsed = time.time() - start
                LOG.d(
                    "Replace reverse alias by real address for %s contacts takes %s seconds",
                    contact_query.count(),
                    elapsed,
                )
                newrelic.agent.record_custom_metric(
                    "Custom/reverse_alias_replacement_time", elapsed
                )

        from app.alias_utils import get_alias_recipient_name

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
            LOG.w(
                "non reverse-alias in reply %s %s %s",
                e,
                contact,
                alias,
            )
            EmailLog.delete(email_log.id, commit=True)

            if mailbox.can_send_or_receive():
                send_email(
                    mailbox.email,
                    f"Email sent to {contact.email} contains non reverse-alias addresses",
                    render(
                        "transactional/non-reverse-alias-reply-phase.txt.jinja2",
                        user=alias.user,
                        destination=contact.email,
                        alias=alias.email,
                        subject=msg[headers.SUBJECT],
                    ),
                )
            return StepResult(early_return=(True, "200"))

        replace_original_message_id(alias, email_log, msg)

        if not msg[headers.DATE]:
            date_header = formatdate()
            LOG.w("missing date header, add one")
            msg[headers.DATE] = date_header

        msg[headers.SL_DIRECTION] = "Reply"
        msg[headers.SL_EMAIL_LOG_ID] = str(email_log.id)

        if should_add_dkim_signature(ctx.alias_domain):
            add_dkim_signature(msg, ctx.alias_domain)

        ctx.msg = msg
        return StepResult()