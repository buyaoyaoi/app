from dataclasses import dataclass, field
from typing import Any, List, Optional, Set, Tuple
from email.message import Message

@dataclass
class ProcessingMessage:
    msg: Message
    mailbox: Optional[Any] = None # For forward phase, the target mailbox. For reply phase, the sender mailbox.
    email_log: Optional[Any] = None
    is_spam: bool = False
    spam_status: str = ""
    stop_processing: bool = False # If true, skip further steps for this message
    action_result: Optional[Tuple[bool, str]] = None # The result for this specific message

@dataclass
class EmailProcessingContext:
    # Input variables
    envelope: Any
    msg: Message
    rcpt_to: str
    is_reply: bool
    notified_mailboxes: Set[int] = field(default_factory=set)
    
    # State populated during pipeline
    alias: Optional[Any] = None
    user: Optional[Any] = None
    contact: Optional[Any] = None
    reply_to_contacts: List[Any] = field(default_factory=list)
    mailboxes: List[Any] = field(default_factory=list)
    
    # Messages to be processed (one per mailbox in forward phase, one for contact in reply phase)
    processing_messages: List[ProcessingMessage] = field(default_factory=list)
    
    # Pipeline control
    stop_processing: bool = False
    
    # Output variables (for global failures/short-circuits)
    action_results: List[Tuple[bool, str]] = field(default_factory=list)
    
    def set_stop_processing(self, is_success: bool, smtp_status: str):
        self.action_results.append((is_success, smtp_status))
        self.stop_processing = True
        
    def add_action_result(self, is_success: bool, smtp_status: str):
        self.action_results.append((is_success, smtp_status))
