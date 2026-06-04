from __future__ import annotations

from typing import List, Optional, Protocol, Tuple

from app.handler.steps.context import EmailProcessingContext, StepResult


class EmailProcessingStep(Protocol):
    """Protocol defining the interface for an email processing step."""

    name: str

    def process(self, ctx: EmailProcessingContext) -> StepResult:
        """Execute this processing step on the given context.

        Returns a StepResult indicating whether to continue the pipeline,
        skip remaining steps, or return early.
        """


class EmailProcessingPipeline:
    """Chain of Responsibility pipeline for email processing.

    Replaces the original serial flat code with a structured pipeline.
    Each step returns a StepResult that controls pipeline flow,
    preserving the original exception logic and rollback behavior.
    """

    def __init__(self) -> None:
        self._steps: List[EmailProcessingStep] = []

    def add_step(self, step: EmailProcessingStep) -> "EmailProcessingPipeline":
        self._steps.append(step)
        return self

    def execute(self, ctx: EmailProcessingContext) -> Optional[Tuple[bool, str]]:
        """Execute all steps in order.

        Returns an early return tuple (bool, str) if any step requests it,
        or None if the pipeline completed normally.
        """
        for step in self._steps:
            result = step.process(ctx)

            if result.skip_remaining_steps:
                return result.early_return

            if result.early_return is not None:
                return result.early_return

            if not result.continue_pipeline:
                break

        return None