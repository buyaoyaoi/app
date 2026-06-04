from email.message import EmailMessage

from aiosmtpd.smtp import Envelope

from app.handler.email_processing_context import EmailProcessingContext
from app.handler.email_processing_pipeline import EmailProcessingPipeline
from app.handler.pgp_strategies import PgpEncryptionStrategyCoordinator
from app.pgp_utils import PGPException


class RecordingStep:
    def __init__(self, calls, name, result=None):
        self.calls = calls
        self.name = name
        self.result = result

    def process(self, context: EmailProcessingContext):
        self.calls.append(self.name)
        context.alias_address = self.name
        return self.result


class FailingPrimaryStrategy:
    def __init__(self, calls):
        self.calls = calls

    def encrypt(self, msg_bytes, pgp_fingerprint, public_key, ctx):
        self.calls.append("primary")
        raise PGPException("fail")


class RecordingValidator:
    def __init__(self, calls):
        self.calls = calls

    def validate(self, public_key, ctx):
        self.calls.append("validator")


class RecordingFallbackStrategy:
    def __init__(self, calls):
        self.calls = calls

    def encrypt(self, msg_bytes, pgp_fingerprint, public_key, ctx):
        self.calls.append("fallback")
        return "encrypted"


class SuccessfulPrimaryStrategy:
    def __init__(self, calls):
        self.calls = calls

    def encrypt(self, msg_bytes, pgp_fingerprint, public_key, ctx):
        self.calls.append("primary")
        return "encrypted"


def test_email_processing_pipeline_stops_after_first_result():
    calls = []
    pipeline = EmailProcessingPipeline(
        [
            RecordingStep(calls, "alias"),
            RecordingStep(calls, "security", (True, "done")),
            RecordingStep(calls, "delivery"),
        ]
    )
    context = EmailProcessingContext(
        envelope=Envelope(),
        msg=EmailMessage(),
        rcpt_to="alias@example.com",
    )

    result = pipeline.run(context)

    assert result == (True, "done")
    assert calls == ["alias", "security"]
    assert context.alias_address == "security"


def test_pgp_strategy_coordinator_uses_fallback_order():
    calls = []
    coordinator = PgpEncryptionStrategyCoordinator(
        primary=FailingPrimaryStrategy(calls),
        validator=RecordingValidator(calls),
        fallback=RecordingFallbackStrategy(calls),
    )

    result = coordinator.encrypt(b"data", "fingerprint", "public-key", object())

    assert result == "encrypted"
    assert calls == ["primary", "validator", "fallback"]


def test_pgp_strategy_coordinator_returns_primary_result_without_fallback():
    calls = []
    coordinator = PgpEncryptionStrategyCoordinator(
        primary=SuccessfulPrimaryStrategy(calls),
        validator=RecordingValidator(calls),
        fallback=RecordingFallbackStrategy(calls),
    )

    result = coordinator.encrypt(b"data", "fingerprint", "public-key", object())

    assert result == "encrypted"
    assert calls == ["primary"]
