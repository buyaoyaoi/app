from dataclasses import dataclass
from typing import Callable

from app.email import headers, status
from app.email_utils import get_header_unicode, parse_full_address
from app.handler.email_processing_context import EmailProcessingContext
from app.log import LOG
from app.models import BlockBehaviourEnum, Contact, EmailLog


class ContactManagementStrategy:
    def handle(self, context: EmailProcessingContext):
        raise NotImplementedError


@dataclass
class ContactManagementStep:
    strategy: ContactManagementStrategy

    def process(self, context: EmailProcessingContext):
        return self.strategy.handle(context)


@dataclass
class ForwardContactManagementStrategy(ContactManagementStrategy):
    get_or_create_contact: Callable[[str, str, object], Contact | None]
    get_or_create_reply_to_contact: Callable[[str, object, object], Contact | None]

    def handle(self, context: EmailProcessingContext):
        alias = context.alias
        if alias is None:
            return None

        from_header = get_header_unicode(context.msg[headers.FROM])
        LOG.d("Create or get contact for from_header:%s", from_header)
        contact = self.get_or_create_contact(
            from_header, context.envelope.mail_from, alias
        )
        if not contact:
            return [(False, status.E504)]

        context.contact = contact
        context.alias = contact.alias
        context.user = contact.user
        context.reply_to_contacts = []

        if context.msg[headers.REPLY_TO]:
            reply_to_header_contents = get_header_unicode(context.msg[headers.REPLY_TO])
            if reply_to_header_contents:
                LOG.d(
                    "Create or get contact for reply_to_header:%s",
                    reply_to_header_contents,
                )
                for reply_to in [
                    reply_to.strip()
                    for reply_to in reply_to_header_contents.split(",")
                    if reply_to.strip()
                ]:
                    try:
                        _, reply_to_email = parse_full_address(reply_to)
                    except ValueError:
                        LOG.d(f"Could not parse reply-to address {reply_to}")
                        continue
                    if reply_to_email == context.alias.email:
                        LOG.i("Reply-to same as alias %s", context.alias)
                    else:
                        reply_contact = self.get_or_create_reply_to_contact(
                            reply_to_email, context.alias, context.msg
                        )
                        if reply_contact:
                            context.reply_to_contacts.append(reply_contact)

        if context.alias.user.delete_on is not None:
            LOG.d(f"user {context.user} is pending to be deleted. Do not forward")
            EmailLog.create(
                contact_id=contact.id,
                user_id=contact.user_id,
                blocked=True,
                alias_id=contact.alias_id,
                commit=True,
            )
            return [(True, status.E502)]

        if not context.alias.enabled or context.alias.is_trashed() or contact.block_forward:
            if not context.alias.enabled:
                LOG.d("%s is disabled, do not forward", context.alias)

            if context.alias.is_trashed():
                LOG.d("%s is trashed, do not forward", context.alias)

            if contact.block_forward:
                LOG.d(
                    "Contact %s of alias %s is blocked, do not forward",
                    contact,
                    context.alias,
                )

            EmailLog.create(
                contact_id=contact.id,
                user_id=contact.user_id,
                blocked=True,
                alias_id=contact.alias_id,
                commit=True,
            )

            res_status = status.E200
            if context.user.block_behaviour == BlockBehaviourEnum.return_5xx:
                res_status = status.E502

            return [(True, res_status)]

        return None


class ReplyContactManagementStrategy(ContactManagementStrategy):
    def handle(self, context: EmailProcessingContext):
        return None
