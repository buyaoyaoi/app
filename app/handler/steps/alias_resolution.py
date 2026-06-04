from typing import Optional, Tuple

from app.alias_utils import try_auto_create
from app.email import status
from app.email_utils import should_ignore_bounce
from app.handler.steps import EmailProcessingStep, EmailProcessingContext
from app.log import LOG
from app.models import Alias


class AliasResolutionStep:
    name = "alias_resolution"

    def process(self, context: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        alias_address = context.rcpt_to
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
                    return (True, status.E207)
                else:
                    return (False, status.E515)

        context.alias = alias
        context.user = alias.user
        return None

    def can_skip(self, context: EmailProcessingContext) -> bool:
        return False