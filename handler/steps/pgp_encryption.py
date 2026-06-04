"""
PGP encryption step with fallback strategies.
"""

from typing import Tuple, Optional
import logging
from email.message import Message

from app.pgp_utils import PGPException, load_public_key_and_check, create_pgp_context
from app import config
from app.email_utils import add_header
from app.models import Alias, Contact, Mailbox, User
from app.email_handler import prepare_pgp_message

LOG = logging.getLogger(__name__)


class PGPEncryptionStrategy:
    """
    Base strategy for PGP encryption.
    """

    def encrypt(self, msg: Message, pgp_finger_print: str, public_key: str, ctx=None) -> Message:
        """
        Encrypt message with this strategy.
        
        Raises:
            PGPException: If encryption fails
        """
        raise NotImplementedError


class PrimaryEncryptionStrategy(PGPEncryptionStrategy):
    """
    Primary encryption strategy using python-gnupg.
    """

    def encrypt(self, msg: Message, pgp_finger_print: str, public_key: str, ctx=None) -> Message:
        return prepare_pgp_message(msg, pgp_finger_print, public_key, can_sign=True, ctx=ctx)


class SecondaryEncryptionStrategy(PGPEncryptionStrategy):
    """
    Secondary encryption strategy using pgpy as fallback.
    """

    def encrypt(self, msg: Message, pgp_finger_print: str, public_key: str, ctx=None) -> Message:
        load_public_key_and_check(public_key, ctx)
        return prepare_pgp_message(msg, pgp_finger_print, public_key, can_sign=True, ctx=ctx)


class PGPEncryptor:
    """
    Encryptor that uses strategies with fallback.
    """

    def __init__(self):
        self.strategies = [
            PrimaryEncryptionStrategy(),
            SecondaryEncryptionStrategy(),
        ]

    def encrypt(self, msg: Message, pgp_finger_print: str, public_key: str) -> Optional[Message]:
        """
        Try to encrypt with strategies in order until success.
        """
        ctx = create_pgp_context()
        for strategy in self.strategies:
            try:
                return strategy.encrypt(msg, pgp_finger_print, public_key, ctx)
            except PGPException as e:
                LOG.warning("Encryption strategy failed: %s", e)
                continue
        return None
