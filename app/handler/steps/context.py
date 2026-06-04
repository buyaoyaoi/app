from typing import Optional, Set, List
from email.message import Message
from aiosmtpd.smtp import Envelope

from app.models import Alias, Contact, User, Mailbox, EmailLog


class EmailProcessingContext:
    __slots__ = (
        "envelope",
        "msg",
        "rcpt_to",
        "alias",
        "contact",
        "user",
        "mailbox",
        "mailboxes",
        "email_log",
        "reply_to_contacts",
        "notified_mailboxes",
        "phase",
        "reply_email",
        "alias_address",
        "alias_domain",
        "contact_domain",
        "from_header",
        "mail_from",
        "is_spam",
        "spam_score",
        "spam_report",
        "spam_status",
        "orig_to",
        "orig_cc",
        "_results",
        "_rollback_actions",
    )

    def __init__(
        self,
        envelope: Envelope,
        msg: Message,
        rcpt_to: str,
        phase: str,
    ):
        self.envelope = envelope
        self.msg = msg
        self.rcpt_to = rcpt_to
        self.phase = phase

        self.alias: Optional[Alias] = None
        self.contact: Optional[Contact] = None
        self.user: Optional[User] = None
        self.mailbox: Optional[Mailbox] = None
        self.mailboxes: Optional[List[Mailbox]] = None
        self.email_log: Optional[EmailLog] = None
        self.reply_to_contacts: List[Contact] = []
        self.notified_mailboxes: Set[int] = set()

        self.reply_email: Optional[str] = None
        self.alias_address: Optional[str] = None
        self.alias_domain: Optional[str] = None
        self.contact_domain: Optional[str] = None
        self.from_header: Optional[str] = None
        self.mail_from: Optional[str] = None

        self.is_spam: bool = False
        self.spam_score: Optional[float] = None
        self.spam_report: Optional[str] = None
        self.spam_status: str = ""

        self.orig_to: Optional[str] = None
        self.orig_cc: Optional[str] = None

        self._results: List[tuple] = []
        self._rollback_actions: List[callable] = []

    def add_result(self, is_success: bool, smtp_status: str) -> None:
        self._results.append((is_success, smtp_status))

    def get_results(self) -> List[Tuple[bool, str]]:
        return self._results.copy()

    def add_rollback_action(self, action: callable) -> None:
        self._rollback_actions.append(action)

    def rollback(self) -> None:
        for action in reversed(self._rollback_actions):
            try:
                action()
            except Exception:
                pass

    def is_forward_phase(self) -> bool:
        return self.phase == "forward"

    def is_reply_phase(self) -> bool:
        return self.phase == "reply"

    def get_mail_from(self) -> str:
        return self.envelope.mail_from

    def get_rcpt_tos(self) -> List[str]:
        return self.envelope.rcpt_tos