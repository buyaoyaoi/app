import pytest
from unittest.mock import Mock, MagicMock
from app.handler.steps.pipeline import EmailProcessingPipeline
from app.handler.steps.context import EmailProcessingContext

def test_pipeline_execution_order():
    step1 = Mock()
    step2 = Mock()
    
    pipeline = EmailProcessingPipeline([step1, step2])
    context = EmailProcessingContext(
        envelope=Mock(),
        msg=Mock(),
        rcpt_to="test@example.com",
        is_reply=False
    )
    
    pipeline.execute(context)
    
    step1.process.assert_called_once_with(context)
    step2.process.assert_called_once_with(context)

def test_pipeline_stop_processing():
    step1 = Mock()
    def process_step1(context):
        context.stop_processing = True
    step1.process.side_effect = process_step1
    
    step2 = Mock()
    
    pipeline = EmailProcessingPipeline([step1, step2])
    context = EmailProcessingContext(
        envelope=Mock(),
        msg=Mock(),
        rcpt_to="test@example.com",
        is_reply=False
    )
    
    pipeline.execute(context)
    
    step1.process.assert_called_once_with(context)
    step2.process.assert_not_called()
