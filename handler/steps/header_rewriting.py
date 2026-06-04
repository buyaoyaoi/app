from app.handler.steps.protocol import EmailProcessingStep
from app.handler.steps.context import EmailProcessingContext
from app.email import headers
from app.log import LOG
from app import config
from app.email_utils import (
    add_header, delete_all_headers_except, get_header_unicode, 
    add_or_replace_header, replace, remove_sender_pgp_key_attachment
)
from app.email_handler import (
    replace_sl_message_id_by_original_message_id,
    replace_header_when_forward,
    add_alias_to_header_if_needed,
    replace_header_when_reply,
    replace_original_message_id
)
from app.errors import CannotCreateContactForReverseAlias, NonReverseAliasInReplyPhase
from app.models import EmailLog, Contact
from app.db import Session
from app.alias_utils import get_alias_recipient_name
from email.utils import formatdate
import time
import newrelic.agent

class HeaderRewritingStep(EmailProcessingStep):
    def process(self, context: EmailProcessingContext) -> None:
        for pm in context.processing_messages:
            if pm.stop_processing:
                continue
                
            if context.is_reply:
                self._process_reply_msg(context, pm)
            else:
                self._process_forward_msg(context, pm)

    def _process_forward_msg(self, context: EmailProcessingContext, pm) -> None:
        user = context.user
        alias = context.alias
        contact = context.contact
        mailbox = pm.mailbox
        email_log = pm.email_log
        envelope = context.envelope

        if contact.invalid_email:
            LOG.d("add noreply information %s %s", alias, mailbox)
            pm.msg = add_header(
                pm.msg,
                f"Email sent to {alias.email} from an invalid address and cannot be replied",
                f"Email sent to {alias.email} from an invalid address and cannot be replied",
            )

        headers_to_keep = [
            headers.FROM, headers.TO, headers.CC, headers.SUBJECT, headers.DATE,
            headers.MESSAGE_ID, headers.REFERENCES, headers.IN_REPLY_TO,
            headers.SL_QUEUE_ID, headers.LIST_UNSUBSCRIBE, headers.LIST_ID,
            headers.LIST_UNSUBSCRIBE_POST,
        ] + headers.MIME_HEADERS
        
        if user.include_header_email_header:
            headers_to_keep.append(headers.AUTHENTICATION_RESULTS)
        delete_all_headers_except(pm.msg, headers_to_keep)

        if mailbox.generic_subject:
            LOG.d("Use a generic subject for %s", mailbox)
            orig_subject = pm.msg[headers.SUBJECT]
            orig_subject = get_header_unicode(orig_subject)
            add_or_replace_header(pm.msg, "Subject", mailbox.generic_subject)
            sender = pm.msg[headers.FROM]
            sender = get_header_unicode(sender)
            pm.msg = add_header(
                pm.msg,
                f'Forwarded by SimpleLogin to {alias.email} from "{sender}" with "{orig_subject}" as subject',
                f'Forwarded by SimpleLogin to {alias.email} from "{sender}" with <b>{orig_subject}</b> as subject',
            )

        add_or_replace_header(pm.msg, headers.SL_DIRECTION, "Forward")
        pm.msg[headers.SL_EMAIL_LOG_ID] = str(email_log.id)
        
        if user.include_header_email_header:
            pm.msg[headers.SL_ENVELOPE_FROM] = envelope.mail_from
            if contact.name:
                original_from = f"{contact.name} <{contact.website_email}>"
            else:
                original_from = contact.website_email
            pm.msg[headers.SL_ORIGINAL_FROM] = original_from
            
        pm.msg[headers.SL_ENVELOPE_TO] = alias.email

        if not pm.msg[headers.DATE]:
            LOG.w("missing date header, create one")
            pm.msg[headers.DATE] = formatdate()

        replace_sl_message_id_by_original_message_id(pm.msg)

        old_from_header = pm.msg[headers.FROM]
        new_from_header = contact.new_addr()
        add_or_replace_header(pm.msg, "From", new_from_header)
        LOG.d("From header, new:%s, old:%s", new_from_header, old_from_header)

        if len(context.reply_to_contacts) > 0:
            original_reply_to = get_header_unicode(pm.msg[headers.REPLY_TO])
            new_reply_to_header = ", ".join(
                [reply_to_contact.new_addr() for reply_to_contact in context.reply_to_contacts][:5]
            )
            add_or_replace_header(pm.msg, "Reply-To", new_reply_to_header)
            LOG.d("Reply-To header, new:%s, old:%s", new_reply_to_header, original_reply_to)

        from app.email.checks import check_recipient_limit
        from app.handler.unsubscribe_generator import UnsubscribeGenerator
        
        if not check_recipient_limit(pm.msg, config.MAX_EMAIL_FORWARD_RECIPIENTS):
            context.add_action_result(False, status.E526)
            pm.stop_processing = True
            return

        try:
            replace_header_when_forward(pm.msg, alias, headers.CC)
            replace_header_when_forward(pm.msg, alias, headers.TO)
        except CannotCreateContactForReverseAlias:
            LOG.d("CannotCreateContactForReverseAlias error, delete %s", email_log)
            EmailLog.delete(email_log.id)
            Session.commit()
            raise

        add_alias_to_header_if_needed(pm.msg, alias)
        
        # add List-Unsubscribe header
        pm.msg = UnsubscribeGenerator().add_header_to_message(alias, contact, pm.msg)

    def _process_reply_msg(self, context: EmailProcessingContext, pm) -> None:
        user = context.user
        alias = context.alias
        contact = context.contact
        mailbox = pm.mailbox
        email_log = pm.email_log
        reply_email = context.rcpt_to

        delete_all_headers_except(
            pm.msg,
            [
                headers.FROM, headers.TO, headers.CC, headers.SUBJECT, headers.DATE,
                headers.MESSAGE_ID, headers.REFERENCES, headers.IN_REPLY_TO,
                headers.SL_QUEUE_ID,
            ] + headers.MIME_HEADERS,
        )

        if config.DROP_PGP_KEY_ATTACHMENTS_ON_REPLY:
            pm.msg = remove_sender_pgp_key_attachment(pm.msg)

        if user.replace_reverse_alias:
            LOG.d("Replace reverse-alias %s by contact email %s", reply_email, contact)
            pm.msg = replace(pm.msg, reply_email, contact.website_email)
            LOG.d("Replace mailbox %s by alias email %s", mailbox.email, alias.email)
            pm.msg = replace(pm.msg, mailbox.email, alias.email)

            if config.ENABLE_ALL_REVERSE_ALIAS_REPLACEMENT:
                start = time.time()
                contact_query = (
                    Contact.query()
                    .filter(Contact.alias_id == alias.id)
                    .limit(config.MAX_NB_REVERSE_ALIAS_REPLACEMENT)
                )

                for r_email, website_email in contact_query.values(
                    Contact.reply_email, Contact.website_email
                ):
                    pm.msg = replace(pm.msg, r_email, website_email)

                elapsed = time.time() - start
                LOG.d(
                    "Replace reverse alias by real address for %s contacts takes %s seconds",
                    contact_query.count(), elapsed,
                )
                newrelic.agent.record_custom_metric(
                    "Custom/reverse_alias_replacement_time", elapsed
                )

        Session.commit()

        recipient_name = get_alias_recipient_name(alias)
        if recipient_name.message:
            LOG.d(recipient_name.message)
        LOG.d("From header is %s", recipient_name.name)
        add_or_replace_header(pm.msg, headers.FROM, recipient_name.name)

        try:
            if str(pm.msg[headers.TO]).lower() == "undisclosed-recipients:;":
                LOG.d("email is sent in BCC mode")
            else:
                replace_header_when_reply(pm.msg, alias, headers.TO)

            replace_header_when_reply(pm.msg, alias, headers.CC)
        except NonReverseAliasInReplyPhase as e:
            LOG.w("non reverse-alias in reply %s %s %s", e, contact, alias)
            EmailLog.delete(email_log.id, commit=True)
            
            # Use lazy import for send_email to avoid circular deps
            from app.email_utils import send_email, render
            if mailbox.can_send_or_receive():
                send_email(
                    mailbox.email,
                    f"Email sent to {contact.email} contains non reverse-alias addresses",
                    render(
                        "transactional/non-reverse-alias-reply-phase.txt.jinja2",
                        user=alias.user, destination=contact.email, alias=alias.email,
                        subject=pm.msg[headers.SUBJECT],
                    ),
                )
            context.add_action_result(True, status.E200)
            pm.stop_processing = True
            return

        replace_original_message_id(alias, email_log, pm.msg)

        if not pm.msg[headers.DATE]:
            date_header = formatdate()
            LOG.w("missing date header, add one")
            pm.msg[headers.DATE] = date_header

        pm.msg[headers.SL_DIRECTION] = "Reply"
        pm.msg[headers.SL_EMAIL_LOG_ID] = str(email_log.id)
