from dataclasses import dataclass
from smtplib import SMTPRecipientsRefused, SMTPServerDisconnected
from typing import Callable

from app.db import Session
from app.email import status
from app.email_utils import generate_verp_email, get_email_domain_part, should_ignore_bounce
from app.handler.email_processing_context import EmailProcessingContext
from app.log import LOG
from app.mail_sender import sl_sendmail
from app.models import Alias, EmailLog, VerpType


class DeliveryStrategy:
    def handle(self, context: EmailProcessingContext):
        raise NotImplementedError


@dataclass
class DeliveryStep:
    strategy: DeliveryStrategy

    def process(self, context: EmailProcessingContext):
        return self.strategy.handle(context)


@dataclass
class ForwardDeliveryStrategy(DeliveryStrategy):
    mailbox_pipeline_runner: Callable[[EmailProcessingContext], tuple[bool, str]]
    send_invalid_mailbox_notification: Callable[..., None]
    copy_message: Callable[[object], object]

    def handle(self, context: EmailProcessingContext):
        ret: list[tuple[bool, str]] = []
        for mailbox in context.mailboxes:
            if not mailbox.verified:
                LOG.d("%s unverified, do not forward", mailbox)
                ret.append((False, status.E517))
                continue

            mailbox_as_alias = Alias.get_by(email=mailbox.email)
            if mailbox_as_alias is not None:
                LOG.info(
                    f"Mailbox {mailbox.id} has email {mailbox.email} that is also alias {context.alias.id}. Stopping loop"
                )
                mailbox.verified = False
                Session.commit()
                self.send_invalid_mailbox_notification(
                    context.user,
                    mailbox,
                    context.alias,
                    f"Your mailbox {mailbox.email} is an alias",
                )
                ret.append((False, status.E525))
                continue

            mailbox_context = EmailProcessingContext(
                envelope=context.envelope,
                msg=self.copy_message(context.msg),
                rcpt_to=context.rcpt_to,
                alias=context.alias,
                user=context.user,
                contact=context.contact,
                reply_to_contacts=list(context.reply_to_contacts),
                mailbox=mailbox,
                alias_domain=get_email_domain_part(context.alias.email),
            )
            ret.append(self.mailbox_pipeline_runner(mailbox_context))

        return ret


class ForwardMailboxDeliveryStrategy(DeliveryStrategy):
    def handle(self, context: EmailProcessingContext):
        LOG.d(
            "Forward mail from %s to %s, mail_options:%s, rcpt_options:%s ",
            context.contact.website_email,
            context.mailbox.email,
            context.envelope.mail_options,
            context.envelope.rcpt_options,
        )

        contact_domain = get_email_domain_part(context.contact.reply_email)
        try:
            sl_sendmail(
                generate_verp_email(
                    VerpType.bounce_forward, context.email_log.id, contact_domain
                ),
                context.mailbox.email,
                context.msg,
                context.envelope.mail_options,
                context.envelope.rcpt_options,
                is_forward=True,
            )
        except (SMTPServerDisconnected, SMTPRecipientsRefused, TimeoutError):
            LOG.w(
                "Postfix error during forward phase %s -> %s -> %s",
                context.contact,
                context.alias,
                context.mailbox,
                exc_info=True,
            )
            if should_ignore_bounce(context.envelope.mail_from):
                return True, status.E207
            EmailLog.delete(context.email_log.id, commit=True)
            return False, status.E407
        Session.commit()
        return True, status.E200


@dataclass
class ReplyDeliveryStrategy(DeliveryStrategy):
    notify_mailbox: Callable[..., None]
    send_email: Callable[..., None]
    render: Callable[..., str]

    def handle(self, context: EmailProcessingContext):
        LOG.d(
            "send email from %s to %s, mail_options:%s,rcpt_options:%s",
            context.alias.email,
            context.contact.website_email,
            context.envelope.mail_options,
            context.envelope.rcpt_options,
        )

        try:
            sl_sendmail(
                generate_verp_email(
                    VerpType.bounce_reply, context.email_log.id, context.alias_domain
                ),
                context.contact.website_email,
                context.msg,
                context.envelope.mail_options,
                context.envelope.rcpt_options,
                is_forward=False,
            )

            other_mailboxes = [
                mb for mb in context.alias.mailboxes if mb.email != context.mailbox.email
            ]
            for mailbox in other_mailboxes:
                if mailbox.id in context.notified_mailboxes:
                    LOG.d(f"Skipping notification to {mailbox.email}, already notified")
                    continue
                self.notify_mailbox(
                    context.alias,
                    context.mailbox,
                    mailbox,
                    context.msg,
                    context.orig_to,
                    context.orig_cc,
                    context.alias_domain,
                )
                context.notified_mailboxes.add(mailbox.id)
        except Exception:
            LOG.w("Cannot send email from %s to %s", context.alias, context.contact)
            EmailLog.delete(context.email_log.id, commit=True)
            if context.mailbox.can_send_or_receive():
                self.send_email(
                    context.mailbox.email,
                    f"Email cannot be sent to {context.contact.email} from {context.alias.email}",
                    self.render(
                        "transactional/reply-error.txt.jinja2",
                        user=context.user,
                        alias=context.alias,
                        contact=context.contact,
                        contact_domain=get_email_domain_part(context.contact.email),
                    ),
                    self.render(
                        "transactional/reply-error.html",
                        user=context.user,
                        alias=context.alias,
                        contact=context.contact,
                        contact_domain=get_email_domain_part(context.contact.email),
                    ),
                )
        return True, status.E200
