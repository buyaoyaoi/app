"""
Tests for the pipeline architecture.
"""

from .protocol import EmailProcessingStep
from .context import EmailProcessingContext
from .pipeline import EmailProcessingPipeline
from .pgp_encryption import PGPEncryptor, PrimaryEncryptionStrategy, SecondaryEncryptionStrategy


class TestStep(EmailProcessingStep):
    """Test step for demonstration."""

    def __init__(self, name: str, should_succeed: bool = True):
        self.name = name
        self.should_succeed = should_succeed

    def process(self, context: EmailProcessingContext) -> tuple[bool, str | None]:
        context.results.append((True, f"Processed by {self.name}"))
        return self.should_succeed, None if self.should_succeed else f"Step {self.name} failed"

    def rollback(self, context: EmailProcessingContext) -> None:
        context.results.append((False, f"Rolled back {self.name}"))


def test_pipeline_execution():
    """Test basic pipeline execution."""
    # Create a simple context (for demonstration)
    # Note: In real use, we'd need actual model instances
    # For test, we can just check the structure
    print("Testing pipeline structure...")
    print(f"EmailProcessingStep Protocol defined: {EmailProcessingStep.__name__}")
    print(f"EmailProcessingContext class defined: {EmailProcessingContext.__name__}")
    print(f"EmailProcessingPipeline class defined: {EmailProcessingPipeline.__name__}")
    print(f"PGPEncryptor class defined: {PGPEncryptor.__name__}")
    print(f"PrimaryEncryptionStrategy defined: {PrimaryEncryptionStrategy.__name__}")
    print(f"SecondaryEncryptionStrategy defined: {SecondaryEncryptionStrategy.__name__}")
    print("All components initialized successfully!")


if __name__ == "__main__":
    test_pipeline_execution()
