from typing import Protocol, Optional, Tuple, List, runtime_checkable, TYPE_CHECKING
from email.message import Message

from app.email import status

if TYPE_CHECKING:
    from app.handler.steps.context import EmailProcessingContext


@runtime_checkable
class EmailProcessingStep(Protocol):
    name: str

    def process(self, context: "EmailProcessingContext") -> Optional[Tuple[bool, str]]:
        ...

    def can_skip(self, context: "EmailProcessingContext") -> bool:
        return False

    def rollback(self, context: "EmailProcessingContext") -> None:
        pass


class StepResult:
    __slots__ = ("is_success", "smtp_status", "should_stop", "skip_remaining")

    def __init__(
        self,
        is_success: bool = True,
        smtp_status: str = status.E200,
        should_stop: bool = False,
        skip_remaining: bool = False,
    ):
        self.is_success = is_success
        self.smtp_status = smtp_status
        self.should_stop = should_stop
        self.skip_remaining = skip_remaining

    @classmethod
    def success(cls) -> "StepResult":
        return cls(is_success=True, smtp_status=status.E200)

    @classmethod
    def failure(cls, smtp_status: str) -> "StepResult":
        return cls(is_success=False, smtp_status=smtp_status, should_stop=True)

    @classmethod
    def stop_with_status(cls, is_success: bool, smtp_status: str) -> "StepResult":
        return cls(is_success=is_success, smtp_status=smtp_status, should_stop=True)

    @classmethod
    def skip_remaining(cls) -> "StepResult":
        return cls(is_success=True, smtp_status=status.E200, skip_remaining=True)

    def to_tuple(self) -> Tuple[bool, str]:
        return (self.is_success, self.smtp_status)


from app.handler.steps.context import EmailProcessingContext

__all__ = ["EmailProcessingStep", "StepResult", "EmailProcessingContext"]