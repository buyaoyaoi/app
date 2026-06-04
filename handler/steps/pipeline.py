"""
Email processing pipeline for orchestrating processing steps.
"""

from typing import List, Tuple, Optional
from .protocol import EmailProcessingStep
from .context import EmailProcessingContext


class EmailProcessingPipeline:
    """
    Pipeline for processing emails through a sequence of steps.
    """

    def __init__(self, steps: List[EmailProcessingStep]):
        """
        Initialize pipeline with processing steps.
        
        Args:
            steps: List of processing steps
        """
        self.steps = steps
        self.executed_steps: List[EmailProcessingStep] = []

    def process(self, context: EmailProcessingContext) -> List[Tuple[bool, str]]:
        """
        Process the email through the pipeline.
        
        Args:
            context: Email processing context
            
        Returns:
            List of (success, status) tuples
        """
        self.executed_steps = []
        
        for step in self.steps:
            success, error_msg = step.process(context)
            self.executed_steps.append(step)
            
            if not success:
                self._rollback(context)
                if context.results:
                    return context.results
                return [(False, error_msg or "Unknown error")]
        
        return context.results

    def _rollback(self, context: EmailProcessingContext) -> None:
        """
        Rollback changes from executed steps in reverse order.
        
        Args:
            context: Email processing context
        """
        for step in reversed(self.executed_steps):
            step.rollback(context)
