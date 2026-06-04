"""
Email processing steps for forward and reply phases.
"""

from .context import EmailProcessingContext
from .pipeline import EmailProcessingPipeline
from .protocol import EmailProcessingStep
from .pgp_encryption import (
    PGPEncryptionStrategy,
    PrimaryEncryptionStrategy,
    SecondaryEncryptionStrategy,
    PGPEncryptor,
)

__all__ = [
    "EmailProcessingContext",
    "EmailProcessingPipeline",
    "EmailProcessingStep",
    "PGPEncryptionStrategy",
    "PrimaryEncryptionStrategy",
    "SecondaryEncryptionStrategy",
    "PGPEncryptor",
]
