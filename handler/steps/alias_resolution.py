"""
Alias resolution step for email processing.
"""

from typing import Tuple, Optional
import logging

from app.models import Alias
from app.alias_utils import try_auto_create
from app import config
from app.email import status
from .protocol import EmailProcessingStep
from .context import EmailProcessingContext

LOG = logging.getLogger(__name__)


class AliasResolutionStep:
    """
    Step for resolving aliases from recipient address.
    """

    def __init__(self, phase: str):
        self.phase = phase

    def process(self, context: EmailProcessingContext) -> Tuple[bool, Optional[str]]:
        """
        Resolve alias from recipient address.
        """
        if self.phase == "forward":
            return self._process_forward(context)
        return True, None

    def _process_forward(self, context: EmailProcessingContext) -> Tuple[bool, Optional[str]]:
        """
        Process alias for forward phase.
        """
        alias_address = context.rcpt_to
        alias = Alias.get_by(email=alias_address)
        
        if not alias:
            LOG.debug(
                "alias %s not exist. Try to see if it can be created on the fly",
                alias_address,
            )
            alias = try_auto_create(alias_address)
            if not alias:
                LOG.debug("alias %s cannot be created on-the-fly", alias_address)
                if should_ignore_bounce(context.envelope.mail_from):
                    context.results = [(True, status.E207)]
                else:
                    context.results = [(False, status.E515)]
                return True, None
        
        context.alias = alias
        context.user = alias.user
        
        return True, None

    def rollback(self, context: EmailProcessingContext) -> None:
        pass


def should_ignore_bounce(mail_from: str) -> bool:
    return mail_from == "<>"
