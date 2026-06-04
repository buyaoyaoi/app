from app.handler.steps.protocol import EmailProcessingStep
from app.handler.steps.context import EmailProcessingContext, ProcessingMessage
from app.models import EmailLog, BlockBehaviourEnum, Alias
from app.email import status, headers
from app.log import LOG
from app import config
from app.email_utils import should_ignore_bounce, get_email_domain_part, copy
from app.handler.dmarc import apply_dmarc_policy_for_forward_phase, apply_dmarc_policy_for_reply_phase
from app.email.spam import get_spam_score
from app.email_utils import get_spam_info, send_email_with_rate_control, render, get_header_unicode
from app.email_handler import (
    handle_email_sent_to_ourself, 
    handle_spam, 
    quarantine_disabled_mailbox_email,
    get_mailbox_for_reply_phase,
    handle_unknown_mailbox,
    spf_pass,
    is_valid_alias_address_domain
)
from app.db import Session
import time

class SecurityValidationStep(EmailProcessingStep):
    def process(self, context: EmailProcessingContext) -> None:
        if context.is_reply:
            self._process_reply(context)
        else:
            self._process_forward(context)

    def _process_forward(self, context: EmailProcessingContext) -> None:
        user = context.user
        alias = context.alias
        contact = context.contact
        envelope = context.envelope
        msg = context.msg

        if not user.is_active():
            LOG.w(f"User {user} has been soft deleted")
            context.set_stop_processing(False, status.E502)
            return

        if not user.can_send_or_receive():
            LOG.i(f"User {user} cannot receive emails")
            if should_ignore_bounce(envelope.mail_from):
                context.set_stop_processing(True, status.E207)
            else:
                context.set_stop_processing(False, status.E504)
            return

        if alias.custom_domain_id and not alias.custom_domain.verified:
            LOG.w("Alias %s is on unverified custom domain, refusing email", alias)
            context.set_stop_processing(False, status.E520)
            return

        # check if email is sent from alias's owning mailbox(es)
        mail_from = envelope.mail_from
        for addr in alias.authorized_addresses():
            if addr == mail_from:
                LOG.i("cycle email sent from %s to %s", addr, alias)
                handle_email_sent_to_ourself(alias, addr, msg, user)
                context.set_stop_processing(True, status.E209)
                return

        if alias.user.delete_on is not None:
            LOG.d(f"user {user} is pending to be deleted. Do not forward")
            EmailLog.create(
                contact_id=contact.id,
                user_id=contact.user_id,
                blocked=True,
                alias_id=contact.alias_id,
                commit=True,
            )
            context.set_stop_processing(True, status.E502)
            return

        if not alias.enabled or alias.is_trashed() or contact.block_forward:
            if not alias.enabled:
                LOG.d("%s is disabled, do not forward", alias)
            if alias.is_trashed():
                LOG.d("%s is trashed, do not forward", alias)
            if contact.block_forward:
                LOG.d("Contact %s of alias %s is blocked, do not forward", contact, alias)

            EmailLog.create(
                contact_id=contact.id,
                user_id=contact.user_id,
                blocked=True,
                alias_id=contact.alias_id,
                commit=True,
            )

            res_status = status.E200
            if user.block_behaviour == BlockBehaviourEnum.return_5xx:
                res_status = status.E502

            context.set_stop_processing(True, res_status)
            return

        # Check if we need to reject or quarantine based on dmarc
        new_msg, dmarc_delivery_status = apply_dmarc_policy_for_forward_phase(
            alias, contact, envelope, msg
        )
        if dmarc_delivery_status is not None:
            context.set_stop_processing(False, dmarc_delivery_status)
            return
        context.msg = new_msg  # update msg after dmarc

        mailboxes = alias.mailboxes
        if not mailboxes:
            LOG.w("no valid mailboxes for %s", alias)
            if should_ignore_bounce(envelope.mail_from):
                context.set_stop_processing(True, status.E207)
            else:
                context.set_stop_processing(False, status.E516)
            return

        # Now iterate mailboxes and initialize ProcessingMessage
        for mailbox in mailboxes:
            if not mailbox.verified:
                LOG.d("%s unverified, do not forward", mailbox)
                context.add_action_result(False, status.E517)
                continue

            mailbox_as_alias = Alias.get_by(email=mailbox.email)
            if mailbox_as_alias is not None:
                LOG.info(
                    f"Mailbox {mailbox.id} has email {mailbox.email} that is also alias {alias.id}. Stopping loop"
                )
                mailbox.verified = False
                Session.commit()
                mailbox_url = f"{config.URL}/dashboard/mailbox/{mailbox.id}/"
                send_email_with_rate_control(
                    user,
                    config.ALERT_MAILBOX_IS_ALIAS,
                    user.email,
                    f"Your mailbox {mailbox.email} is an alias",
                    render(
                        "transactional/mailbox-invalid.txt.jinja2",
                        user=mailbox.user,
                        mailbox=mailbox,
                        mailbox_url=mailbox_url,
                        alias=alias,
                    ),
                    render(
                        "transactional/mailbox-invalid.html",
                        user=mailbox.user,
                        mailbox=mailbox,
                        mailbox_url=mailbox_url,
                        alias=alias,
                    ),
                    max_nb_alert=1,
                )
                context.add_action_result(False, status.E525)
                continue

            # Check individual mailbox conditions
            if mailbox.disabled:
                LOG.d(f"{mailbox} disabled, do not forward")
                if should_ignore_bounce(envelope.mail_from):
                    context.add_action_result(True, status.E207)
                else:
                    context.add_action_result(False, status.E518)
                continue

            if mailbox.is_admin_disabled():
                LOG.d(f"{mailbox} admin_disabled, do not forward")
                quarantine_disabled_mailbox_email(alias, contact, mailbox, envelope, context.msg)
                context.add_action_result(True, status.E207)
                continue

            if get_email_domain_part(alias.email) == get_email_domain_part(mailbox.email):
                LOG.w(
                    "Mailbox has the same domain as alias. %s -> %s -> %s",
                    contact, alias, mailbox
                )
                mailbox_url = f"{config.URL}/dashboard/mailbox/{mailbox.id}/"
                send_email_with_rate_control(
                    user,
                    config.ALERT_MAILBOX_IS_ALIAS,
                    user.email,
                    f"Your mailbox {mailbox.email} and alias {alias.email} use the same domain",
                    render(
                        "transactional/mailbox-invalid.txt.jinja2",
                        user=mailbox.user, mailbox=mailbox, mailbox_url=mailbox_url, alias=alias,
                    ),
                    render(
                        "transactional/mailbox-invalid.html",
                        user=mailbox.user, mailbox=mailbox, mailbox_url=mailbox_url, alias=alias,
                    ),
                    max_nb_alert=1,
                )
                context.add_action_result(False, status.E405)
                continue

            # Create EmailLog
            email_log = EmailLog.create(
                contact_id=contact.id,
                user_id=contact.user_id,
                mailbox_id=mailbox.id,
                alias_id=contact.alias_id,
                message_id=str(context.msg[headers.MESSAGE_ID]),
                commit=True,
            )
            LOG.d("Create %s for %s, %s, %s", email_log, contact, user, mailbox)

            pm = ProcessingMessage(
                msg=copy(context.msg),
                mailbox=mailbox,
                email_log=email_log
            )

            # Spam check
            if config.ENABLE_SPAM_ASSASSIN:
                is_spam = False
                spam_status = ""
                
                if config.SPAMASSASSIN_HOST:
                    start = time.time()
                    spam_score, spam_report = get_spam_score(pm.msg, email_log)
                    LOG.d(
                        "%s -> %s - spam score:%s in %s seconds. Spam report %s",
                        contact, alias, spam_score, time.time() - start, spam_report,
                    )
                    email_log.spam_score = spam_score
                    Session.commit()

                    if (user.max_spam_score and spam_score > user.max_spam_score) or (
                        not user.max_spam_score and spam_score > config.MAX_SPAM_SCORE
                    ):
                        is_spam = True
                        email_log.spam_report = spam_report
                else:
                    is_spam, spam_status = get_spam_info(pm.msg, max_score=user.max_spam_score)

                if is_spam:
                    LOG.w(
                        "Email detected as spam. %s -> %s. Spam Score: %s, Spam Report: %s",
                        contact, alias, email_log.spam_score, email_log.spam_report,
                    )
                    email_log.is_spam = True
                    email_log.spam_status = spam_status
                    Session.commit()

                    handle_spam(contact, alias, pm.msg, user, mailbox, email_log)
                    context.add_action_result(False, status.E519)
                    pm.stop_processing = True

            if not pm.stop_processing:
                context.processing_messages.append(pm)

    def _process_reply(self, context: EmailProcessingContext) -> None:
        user = context.user
        alias = context.alias
        contact = context.contact
        envelope = context.envelope
        msg = context.msg

        if alias.custom_domain_id and not alias.custom_domain.verified:
            LOG.w("Alias %s is on unverified custom domain, refusing email", alias)
            context.set_stop_processing(False, status.E520)
            return

        if alias.is_trashed():
            LOG.d("%s is trashed, do not forward", alias)
            context.set_stop_processing(False, status.E502)
            return

        if not is_valid_alias_address_domain(alias.email):
            LOG.e("%s domain isn't known", alias)
            context.set_stop_processing(False, status.E503)
            return

        if not user.can_send_or_receive():
            LOG.i(f"User {user} cannot send emails")
            context.set_stop_processing(False, status.E504)
            return

        dmarc_delivery_status = apply_dmarc_policy_for_reply_phase(
            alias, contact, envelope, msg
        )
        if dmarc_delivery_status is not None:
            context.set_stop_processing(False, dmarc_delivery_status)
            return

        mailbox = get_mailbox_for_reply_phase(
            envelope.mail_from, get_header_unicode(msg[headers.FROM]), alias
        )
        if not mailbox:
            if alias.disable_email_spoofing_check:
                LOG.w(
                    "ignore unknown sender to reverse-alias %s: %s -> %s",
                    envelope.mail_from, alias, contact,
                )
                mailbox = alias.mailbox
            else:
                handle_unknown_mailbox(envelope, msg, context.rcpt_to, user, alias, contact)
                context.set_stop_processing(False, status.E214)
                return

        if mailbox.is_admin_disabled():
            LOG.i(f"User {user} tried to send a mail from admin disabled mailbox {mailbox}")
            context.set_stop_processing(False, status.E207)
            return

        if config.ENFORCE_SPF and mailbox.force_spf and not alias.disable_email_spoofing_check:
            if not spf_pass(envelope, mailbox, user, alias, contact.website_email, msg):
                context.set_stop_processing(True, status.E201)
                return

        email_log = EmailLog.create(
            contact_id=contact.id,
            alias_id=contact.alias_id,
            is_reply=True,
            user_id=contact.user_id,
            mailbox_id=mailbox.id,
            message_id=msg[headers.MESSAGE_ID],
            commit=True,
        )
        LOG.d("Create %s for %s, %s, %s", email_log, contact, user, mailbox)

        pm = ProcessingMessage(
            msg=copy(context.msg),
            mailbox=mailbox,
            email_log=email_log
        )

        # Spam check
        if config.ENABLE_SPAM_ASSASSIN:
            is_spam = False
            spam_status = ""

            if config.SPAMASSASSIN_HOST:
                start = time.time()
                spam_score, spam_report = get_spam_score(pm.msg, email_log)
                LOG.d(
                    "%s -> %s - spam score %s in %s seconds. Spam report %s",
                    alias, contact, spam_score, time.time() - start, spam_report,
                )
                email_log.spam_score = spam_score
                if spam_score > config.MAX_REPLY_PHASE_SPAM_SCORE:
                    is_spam = True
                    email_log.spam_report = spam_report
            else:
                is_spam, spam_status = get_spam_info(
                    pm.msg, max_score=config.MAX_REPLY_PHASE_SPAM_SCORE
                )

            if is_spam:
                LOG.w(
                    "Email detected as spam. Reply phase. %s -> %s. Spam Score: %s, Spam Report: %s",
                    alias, contact, email_log.spam_score, email_log.spam_report,
                )
                email_log.is_spam = True
                email_log.spam_status = spam_status
                Session.commit()

                handle_spam(contact, alias, pm.msg, user, mailbox, email_log, is_reply=True)
                context.set_stop_processing(False, status.E506)
                return

        context.processing_messages.append(pm)
