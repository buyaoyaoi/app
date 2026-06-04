from typing import Any, Protocol

from app.handler.email_processing_context import EmailProcessingContext


class EmailProcessingStep(Protocol):
    def process(self, context: EmailProcessingContext) -> Any | None:
        ...


class EmailProcessingPipeline:
    def __init__(self, steps: list[EmailProcessingStep]):
        self._steps = steps

    def run(self, context: EmailProcessingContext) -> Any | None:
        for step in self._steps:
            result = step.process(context)
            if result is not None:
                return result
        return None
