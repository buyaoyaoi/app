from dataclasses import dataclass
from io import BytesIO

from sl_pgp import PgpContext

from app import pgp_utils
from app.log import LOG
from app.pgp_utils import PGPException, load_public_key_and_check


class MessageEncryptionStrategy:
    def encrypt(
        self,
        msg_bytes: bytes,
        pgp_fingerprint: str,
        public_key: str,
        ctx: PgpContext,
    ) -> str:
        raise NotImplementedError


@dataclass
class GnuPGEncryptionStrategy(MessageEncryptionStrategy):
    def encrypt(
        self,
        msg_bytes: bytes,
        pgp_fingerprint: str,
        public_key: str,
        ctx: PgpContext,
    ) -> str:
        return pgp_utils.encrypt_file(BytesIO(msg_bytes), pgp_fingerprint, ctx)


@dataclass
class PublicKeyValidationStrategy:
    def validate(self, public_key: str, ctx: PgpContext) -> None:
        load_public_key_and_check(public_key, ctx)


@dataclass
class PgpyEncryptionStrategy(MessageEncryptionStrategy):
    def encrypt(
        self,
        msg_bytes: bytes,
        pgp_fingerprint: str,
        public_key: str,
        ctx: PgpContext,
    ) -> str:
        encrypted = pgp_utils.encrypt_file_with_pgpy(msg_bytes, public_key, ctx)
        return str(encrypted)


@dataclass
class PgpEncryptionStrategyCoordinator:
    primary: MessageEncryptionStrategy
    validator: PublicKeyValidationStrategy
    fallback: MessageEncryptionStrategy

    def encrypt(
        self,
        msg_bytes: bytes,
        pgp_fingerprint: str,
        public_key: str,
        ctx: PgpContext,
    ) -> str:
        try:
            return self.primary.encrypt(msg_bytes, pgp_fingerprint, public_key, ctx)
        except PGPException:
            LOG.w(
                "Cannot encrypt using python-gnupg, check if public key is valid and try with pgpy"
            )
            self.validator.validate(public_key, ctx)
            encrypted = self.fallback.encrypt(msg_bytes, pgp_fingerprint, public_key, ctx)
            LOG.i(
                f"encryption works with pgpy and not with python-gnupg, public key {public_key}"
            )
            return encrypted
