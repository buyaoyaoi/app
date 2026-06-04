"""
Email processing context for holding state across steps.
"""

from typing import List, Optional, Tuple, Set

from app.models import (
    Envelope,
    Message,
    Alias,
    Contact,
    Mailbox,
    User,
    EmailLog,
)


class EmailProcessingContext:
    """
    Holds the state and data needed for email processing.
    """

    def __init__(
        self,
        envelope: Envelope,
        msg: Message,
        rcpt_to: str,
        phase: str = "forward",  # "forward" or "reply"
        notified_mailboxes: Optional[Set[int]] = None,
    ):
        # Input parameters
        self.envelope = envelope
        self.msg = msg
        self.rcpt_to = rcpt_to
        self.phase = phase
        self.notified_mailboxes = notified_mailboxes or set()
        
        # Processing state
        self.alias: Optional[Alias] = None
        self.contact: Optional[Contact] = None
        self.user: Optional[User] = None
        self.mailboxes: List[Mailbox] = []
        self.mailbox: Optional[Mailbox] = None
        self.email_log: Optional[EmailLog] = None
        self.reply_to_contacts: List[Contact] = []
        
        # Result
        self.results: List[Tuple[bool, str]] = []
        self.current_result: Optional[Tuple[bool, str]] = None
        
        # Error handling
        self.error_message: Optional[str] = None
        self.should_rollback: bool = False
