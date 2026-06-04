import pytest
from unittest.mock import Mock, patch
from app.handler.steps.pgp_encryption import PgpEncryptionExecutor, PrimaryGnuPgStrategy, PublicKeyCheckStrategy, FallbackPgpyStrategy, FallbackRequiredError
from app.pgp_utils import PGPException

def test_pgp_encryption_executor_success_primary():
    executor = PgpEncryptionExecutor()
    executor.strategies = [Mock(spec=PrimaryGnuPgStrategy), Mock(spec=PublicKeyCheckStrategy), Mock(spec=FallbackPgpyStrategy)]
    
    # Primary strategy succeeds
    executor.strategies[0].execute.return_value = "msg"
    
    result = executor.encrypt_message(Mock(), b"bytes", "fingerprint", "public_key", Mock())
    
    assert result == "msg"
    executor.strategies[0].execute.assert_called_once()
    executor.strategies[1].execute.assert_not_called()
    executor.strategies[2].execute.assert_not_called()

def test_pgp_encryption_executor_fallback():
    executor = PgpEncryptionExecutor()
    executor.strategies = [Mock(spec=PrimaryGnuPgStrategy), Mock(spec=PublicKeyCheckStrategy), Mock(spec=FallbackPgpyStrategy)]
    
    # Primary fails with PGPException
    executor.strategies[0].execute.side_effect = PGPException("Primary failed")
    # Second fails with FallbackRequiredError
    executor.strategies[1].execute.side_effect = FallbackRequiredError()
    # Third succeeds
    executor.strategies[2].execute.return_value = "msg"
    
    result = executor.encrypt_message(Mock(), b"bytes", "fingerprint", "public_key", Mock())
    
    assert result == "msg"
    executor.strategies[0].execute.assert_called_once()
    executor.strategies[1].execute.assert_called_once()
    executor.strategies[2].execute.assert_called_once()

def test_pgp_encryption_executor_all_fail():
    executor = PgpEncryptionExecutor()
    executor.strategies = [Mock(spec=PrimaryGnuPgStrategy), Mock(spec=PublicKeyCheckStrategy), Mock(spec=FallbackPgpyStrategy)]
    
    # Primary fails with PGPException
    executor.strategies[0].execute.side_effect = PGPException("Primary failed")
    # Second fails with FallbackRequiredError
    executor.strategies[1].execute.side_effect = FallbackRequiredError()
    # Third fails with Exception
    executor.strategies[2].execute.side_effect = PGPException("Fallback failed")
    
    with pytest.raises(PGPException) as excinfo:
        executor.encrypt_message(Mock(), b"bytes", "fingerprint", "public_key", Mock())
        
    assert "Fallback failed" in str(excinfo.value)
    executor.strategies[0].execute.assert_called_once()
    executor.strategies[1].execute.assert_called_once()
    executor.strategies[2].execute.assert_called_once()
