from __future__ import annotations

from dataclasses import dataclass, field
from email.message import Message
from typing import TYPE_CHECKING, List, Optional, Set, Tuple

if TYPE_CHECKING:
    from app.models import Alias, Contact, User, Mailbox, EmailLog
    from aiosmtpd.smtp import Envelope


@dataclass
class EmailProcessingContext:
    """Lightweight unified context that consolidates scattered variables.

    Only reuses existing fields from the original code — no new data fields.
    """

    msg: Message
    envelope: "Envelope"
    rcpt_to: str

    alias: Optional["Alias"] = None
    user: Optional["User"] = None

    contact: Optional["Contact"] = None
    reply_to_contacts: List["Contact"] = field(default_factory=list)

    mailbox: Optional["Mailbox"] = None
    mailboxes: List["Mailbox"] = field(default_factory=list)

    email_log: Optional["EmailLog"] = None

    notified_mailboxes: Set[int] = field(default_factory=set)

    phase: str = ""

    delivery_results: List[Tuple[bool, str]] = field(default_factory=list)

    dmarc_delivery_status: Optional[str] = None

    orig_to: Optional[str] = None
    orig_cc: Optional[str] = None
    alias_domain: Optional[str] = None
    from_header: Optional[str] = None


@dataclass
class StepResult:
    """Result of a processing step."""

    continue_pipeline: bool = True
    early_return: Optional[Tuple[bool, str]] = None
    skip_remaining_steps: bool = False
    rollback_email_log: bool = False