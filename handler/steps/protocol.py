"""
Protocol defining email processing step interface.
"""

from typing import Protocol, Optional, Tuple
from email.message import Message

from .context import EmailProcessingContext


class EmailProcessingStep(Protocol):
    """
    Protocol for email processing steps.
    """

    def process(self, context: EmailProcessingContext) -> Tuple[bool, Optional[str]]:
        """
        Process the email context.
        
        Args:
            context: Email processing context
            
        Returns:
            Tuple of (success, error message)
        """
        ...

    def rollback(self, context: EmailProcessingContext) -> None:
        """
        Rollback any changes made during processing.
        
        Args:
            context: Email processing context
        """
        ...
