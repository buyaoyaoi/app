from typing import List, Tuple
from app.handler.steps.context import EmailProcessingContext
from app.handler.steps.protocol import EmailProcessingStep

class EmailProcessingPipeline:
    def __init__(self, steps: List[EmailProcessingStep]):
        self.steps = steps

    def execute(self, context: EmailProcessingContext) -> List[Tuple[bool, str]]:
        for step in self.steps:
            if context.stop_processing:
                break
            
            step.process(context)
            
        return context.action_results
