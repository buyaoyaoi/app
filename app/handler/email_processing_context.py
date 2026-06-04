from dataclasses import dataclass, field
from email.message import Message
from typing import Optional, Set

from aiosmtpd.smtp import Envelope

from app.models import Alias, Contact, EmailLog, Mailbox, User


@dataclass
class EmailProcessingContext:
    envelope: Envelope
    msg: Message
    rcpt_to: str
    notified_mailboxes: Optional[Set[int]] = None
    alias: Optional[Alias] = None
    user: Optional[User] = None
    contact: Optional[Contact] = None
    reply_to_contacts: list[Contact] = field(default_factory=list)
    mailbox: Optional[Mailbox] = None
    email_log: Optional[EmailLog] = None
    reply_email: Optional[str] = None
    alias_address: Optional[str] = None
    alias_domain: Optional[str] = None
    mail_from: Optional[str] = None
    orig_to: Optional[str] = None
    orig_cc: Optional[str] = None
    mailboxes: list[Mailbox] = field(default_factory=list)
