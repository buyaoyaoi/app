from __future__ import annotations

from app import config
from app.email import headers
from app.email_utils import (
    get_header_unicode,
    parse_full_address,
    generate_reply_email,
)
from app.handler.steps.context import EmailProcessingContext, StepResult
from app.log import LOG
from app.models import Contact


class ContactManagementStep:
    name = "contact_management"

    def process(self, ctx: EmailProcessingContext) -> StepResult:
        if ctx.phase == "forward":
            return self._process_forward(ctx)
        return self._process_reply(ctx)

    def _process_forward(self, ctx: EmailProcessingContext) -> StepResult:
        from email_handler import get_or_create_contact, get_or_create_reply_to_contact

        from_header = get_header_unicode(ctx.msg[headers.FROM])
        LOG.d("Create or get contact for from_header:%s", from_header)
        contact = get_or_create_contact(from_header, ctx.envelope.mail_from, ctx.alias)
        if not contact:
            return StepResult(early_return=(False, "504"))
        ctx.contact = contact
        ctx.alias = contact.alias

        ctx.from_header = from_header

        reply_to_contacts: list[Contact] = []
        if ctx.msg[headers.REPLY_TO]:
            reply_to_header_contents = get_header_unicode(ctx.msg[headers.REPLY_TO])
            if reply_to_header_contents:
                LOG.d(
                    "Create or get contact for reply_to_header:%s",
                    reply_to_header_contents,
                )
                for reply_to in [
                    r.strip()
                    for r in reply_to_header_contents.split(",")
                    if r.strip()
                ]:
                    try:
                        reply_to_name, reply_to_email = parse_full_address(reply_to)
                    except ValueError:
                        LOG.d(f"Could not parse reply-to address {reply_to}")
                        continue
                    if reply_to_email == ctx.alias.email:
                        LOG.i("Reply-to same as alias %s", ctx.alias)
                    else:
                        reply_contact = get_or_create_reply_to_contact(
                            reply_to_email, ctx.alias, ctx.msg
                        )
                        if reply_contact:
                            reply_to_contacts.append(reply_contact)

        ctx.reply_to_contacts = reply_to_contacts
        return StepResult()

    def _process_reply(self, ctx: EmailProcessingContext) -> StepResult:
        from app.mailbox_utils import get_mailbox_for_reply_phase

        from email_handler import handle_unknown_mailbox

        mailbox = get_mailbox_for_reply_phase(
            ctx.envelope.mail_from,
            get_header_unicode(ctx.msg[headers.FROM]),
            ctx.alias,
        )
        if not mailbox:
            if ctx.alias.disable_email_spoofing_check:
                LOG.w(
                    "ignore unknown sender to reverse-alias %s: %s -> %s",
                    ctx.envelope.mail_from,
                    ctx.alias,
                    ctx.contact,
                )
                mailbox = ctx.alias.mailbox
            else:
                handle_unknown_mailbox(
                    ctx.envelope,
                    ctx.msg,
                    ctx.rcpt_to,
                    ctx.user,
                    ctx.alias,
                    ctx.contact,
                )
                return StepResult(early_return=(False, "214"))

        if mailbox.is_admin_disabled():
            LOG.i(
                f"User {ctx.user} tried to send a mail from admin disabled mailbox {mailbox}"
            )
            return StepResult(early_return=(False, "207"))

        if (
            config.ENFORCE_SPF
            and mailbox.force_spf
            and not ctx.alias.disable_email_spoofing_check
        ):
            from app.email_utils import spf_pass

            if not spf_pass(
                ctx.envelope,
                mailbox,
                ctx.user,
                ctx.alias,
                ctx.contact.website_email,
                ctx.msg,
            ):
                return StepResult(early_return=(True, "201"))

        ctx.mailbox = mailbox
        return StepResult()