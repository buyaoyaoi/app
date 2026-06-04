"""
Header rewriting step for email processing.
"""

from typing import Tuple, Optional
import logging

from app.email_utils import (
    delete_all_headers_except,
    add_or_replace_header,
    get_header_unicode,
)
from app.email import headers
from app.models import EmailLog, Alias, Contact, Mailbox, User
from app.email.checks import check_recipient_limit
from app.handler.unsubscribe_generator import UnsubscribeGenerator
from email.message import Message
from .protocol import EmailProcessingStep
from .context import EmailProcessingContext

LOG = logging.getLogger(__name__)


class HeaderRewritingStep:
    """
    Step for rewriting email headers.
    """

    def process(self, context: EmailProcessingContext) -> Tuple[bool, Optional[str]]:
        """
        This step doesn't do any processing at this level, as it's handled
        by the original code for now.
        """
        return True, None

    def rollback(self, context: EmailProcessingContext) -> None:
        pass
