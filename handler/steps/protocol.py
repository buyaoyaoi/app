from typing import Protocol
from app.handler.steps.context import EmailProcessingContext

class EmailProcessingStep(Protocol):
    def process(self, context: EmailProcessingContext) -> None:
        """Execute the processing step"""
        ...
