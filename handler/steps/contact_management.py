from app.handler.steps.protocol import EmailProcessingStep
from app.handler.steps.context import EmailProcessingContext
from app.models import Contact
from app.email import status
from app.email_utils import get_header_unicode
from app.email import headers
from app.log import LOG

class ContactManagementStep(EmailProcessingStep):
    def process(self, context: EmailProcessingContext) -> None:
        if context.is_reply:
            self._process_reply(context)
        else:
            self._process_forward(context)

    def _process_forward(self, context: EmailProcessingContext) -> None:
        # Import here to avoid circular imports if needed
        from app.email_handler import get_or_create_contact, get_or_create_reply_to_contact, parse_full_address

        from_header = get_header_unicode(context.msg[headers.FROM])
        LOG.d("Create or get contact for from_header:%s", from_header)
        contact = get_or_create_contact(from_header, context.envelope.mail_from, context.alias)
        if not contact:
            context.set_stop_processing(False, status.E504)
            return
            
        context.contact = contact
        # In case the Session was closed in the get_or_create we re-fetch the alias
        context.alias = contact.alias

        reply_to_contact = []
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
                        reply_contact = get_or_create_reply_to_contact(reply_to_email, context.alias, context.msg)
                        if reply_contact:
                            reply_to_contact.append(reply_contact)
                            
        context.reply_to_contacts = reply_to_contact

    def _process_reply(self, context: EmailProcessingContext) -> None:
        reply_email = context.rcpt_to
        
        contact = Contact.get_by(reply_email=reply_email)
        if not contact:
            LOG.w(f"No contact with {reply_email} as reverse alias")
            context.set_stop_processing(False, status.E502)
            return
            
        if not contact.user.is_active():
            LOG.w(f"User {contact.user} has been soft deleted")
            context.set_stop_processing(False, status.E502)
            return

        context.contact = contact
        context.alias = contact.alias
        context.user = contact.user
