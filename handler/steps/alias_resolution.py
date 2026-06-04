from app.handler.steps.protocol import EmailProcessingStep
from app.handler.steps.context import EmailProcessingContext
from app.models import Alias, SLDomain
from app.alias_utils import try_auto_create
from app.email_utils import should_ignore_bounce, get_email_domain_part
from app.email_validation import normalize_reply_email
from app.email import status
from app.log import LOG
from app import config

class AliasResolutionStep(EmailProcessingStep):
    def process(self, context: EmailProcessingContext) -> None:
        if context.is_reply:
            self._process_reply(context)
        else:
            self._process_forward(context)

    def _process_forward(self, context: EmailProcessingContext) -> None:
        alias_address = context.rcpt_to  # alias@SL

        alias = Alias.get_by(email=alias_address)
        if not alias:
            LOG.d(
                "alias %s not exist. Try to see if it can be created on the fly",
                alias_address,
            )
            alias = try_auto_create(alias_address)
            if not alias:
                LOG.d("alias %s cannot be created on-the-fly, return 550", alias_address)
                if should_ignore_bounce(context.envelope.mail_from):
                    context.set_stop_processing(True, status.E207)
                else:
                    context.set_stop_processing(False, status.E515)
                return

        context.alias = alias
        context.user = alias.user

    def _process_reply(self, context: EmailProcessingContext) -> None:
        reply_email = context.rcpt_to
        reply_domain = get_email_domain_part(reply_email)

        # reply_email must end with EMAIL_DOMAIN or a domain that can be used as reverse alias domain
        if not reply_email.endswith(config.EMAIL_DOMAIN):
            sl_domain: SLDomain = SLDomain.get_by(domain=reply_domain)
            if sl_domain is None:
                LOG.w(f"Reply email {reply_email} has wrong domain")
                context.set_stop_processing(False, status.E501)
                return

        # handle case where reply email is generated with non-allowed char
        reply_email = normalize_reply_email(reply_email)
        context.rcpt_to = reply_email # update it after normalization
