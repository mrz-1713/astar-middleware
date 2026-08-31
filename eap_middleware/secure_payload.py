"""Versioned authenticated payload encryption for the optional legacy API.

New integrations use ``aes_256_gcm_v2``.  The historic n8n/PHP
``aes-256-ctr`` + HMAC construction remains available only as the explicitly
selected ``legacy_ctr_v1`` compatibility mode; its wire bytes cannot be
changed without coordinating the external peer.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any, Dict

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


AES_256_GCM_V2 = "aes_256_gcm_v2"
LEGACY_CTR_V1 = "legacy_ctr_v1"
SUPPORTED_ENCRYPTION_MODES = frozenset({AES_256_GCM_V2, LEGACY_CTR_V1})

_V2_PREFIX = "v2."
_V2_AAD = b"astar-legacy-api:aes-256-gcm:v2"
_V2_NONCE_BYTES = 12
_V2_TAG_BYTES = 16



class SecurePayloadError(ValueError):
    """Raised when a payload cannot be encrypted or decrypted."""

    pass


@dataclass(frozen=True)
class SecurePayloadCodec:
    """Encrypt and decrypt exactly one configured payload format.

    There is deliberately no automatic downgrade: a v2 codec rejects legacy
    input and vice versa.  That makes a peer migration explicit and prevents
    corrupted or attacker-controlled v2 data from being reinterpreted as the
    weaker compatibility format.
    """

    first_key: bytes
    second_key: bytes = b""
    encryption_mode: str = LEGACY_CTR_V1

    @classmethod
    def from_base64_keys(
        cls,
        first_key_b64: str,
        second_key_b64: str,
    ) -> "SecurePayloadCodec":
        first = _decode_base64_key(first_key_b64, "first key")
        second = _decode_base64_key(second_key_b64, "second key")
        if len(first) != 32:
            raise SecurePayloadError("first key must decode to 32 bytes for AES-256")
        if len(second) == 0:
            raise SecurePayloadError("second key must not be empty")
        return cls(
            first_key=first,
            second_key=second,
            encryption_mode=LEGACY_CTR_V1,
        )

    @classmethod
    def from_aes256_gcm_key_base64(cls, key_b64: str) -> "SecurePayloadCodec":
        key = _decode_base64_key(key_b64, "AES-256-GCM key")
        if len(key) != 32:
            raise SecurePayloadError(
                "AES-256-GCM key must decode to exactly 32 bytes"
            )
        return cls(first_key=key, encryption_mode=AES_256_GCM_V2)

    @classmethod
    def from_raw_keys(
        cls,
        first_key: str,
        second_key: str,
    ) -> "SecurePayloadCodec":
        first = _normalize_aes256_key(first_key.encode("utf-8"))
        second = second_key.encode("utf-8")
        if len(second) == 0:
            raise SecurePayloadError("second key must not be empty")
        return cls(
            first_key=first,
            second_key=second,
            encryption_mode=LEGACY_CTR_V1,
        )

    def encrypt_text(self, plaintext: str) -> str:
        if self.encryption_mode == AES_256_GCM_V2:
            nonce = os.urandom(_V2_NONCE_BYTES)
            sealed = AESGCM(self.first_key).encrypt(
                nonce,
                plaintext.encode("utf-8"),
                _V2_AAD,
            )
            return _V2_PREFIX + base64.b64encode(nonce + sealed).decode("ascii")
        if self.encryption_mode != LEGACY_CTR_V1:
            raise SecurePayloadError(
                f"unsupported encryption mode {self.encryption_mode!r}"
            )
        iv = os.urandom(16)
        encryptor = Cipher(algorithms.AES(self.first_key), modes.CTR(iv)).encryptor()
        ciphertext = encryptor.update(plaintext.encode("utf-8")) + encryptor.finalize()
        digest = hmac.new(self.second_key, ciphertext, hashlib.sha3_512).digest()
        return base64.b64encode(iv + digest + ciphertext).decode("ascii")

    def decrypt_text(self, encrypted: str) -> str:
        if self.encryption_mode == AES_256_GCM_V2:
            if not encrypted.startswith(_V2_PREFIX):
                raise SecurePayloadError("payload is not AES-256-GCM v2")
            mixed = _decode_base64_payload(encrypted[len(_V2_PREFIX):])
            if len(mixed) < _V2_NONCE_BYTES + _V2_TAG_BYTES:
                raise SecurePayloadError("AES-256-GCM v2 payload is truncated")
            nonce = mixed[:_V2_NONCE_BYTES]
            sealed = mixed[_V2_NONCE_BYTES:]
            try:
                plaintext = AESGCM(self.first_key).decrypt(nonce, sealed, _V2_AAD)
            except InvalidTag as exc:
                # Do not distinguish a bad key from nonce/ciphertext/tag
                # tampering; those details are useful to an attacker and not
                # actionable for an operator.
                raise SecurePayloadError(
                    "AES-256-GCM v2 authentication failed"
                ) from exc
            try:
                return plaintext.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SecurePayloadError(
                    "decrypted payload is not valid UTF-8"
                ) from exc
        if self.encryption_mode != LEGACY_CTR_V1:
            raise SecurePayloadError(
                f"unsupported encryption mode {self.encryption_mode!r}"
            )
        if encrypted.startswith(_V2_PREFIX):
            raise SecurePayloadError("AES-256-GCM v2 payload requires a v2 codec")
        try:
            mixed = base64.b64decode(encrypted, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise SecurePayloadError("payload is not valid base64") from exc
        if len(mixed) < 80:
            raise SecurePayloadError("payload is shorter than IV + HMAC")
        iv = mixed[:16]
        received_digest = mixed[16:80]
        ciphertext = mixed[80:]
        expected_digest = hmac.new(
            self.second_key,
            ciphertext,
            hashlib.sha3_512,
        ).digest()
        if not hmac.compare_digest(received_digest, expected_digest):
            raise SecurePayloadError("payload HMAC verification failed")
        decryptor = Cipher(algorithms.AES(self.first_key), modes.CTR(iv)).decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecurePayloadError("decrypted payload is not valid UTF-8") from exc

    def encrypt_json(self, payload: Dict[str, Any]) -> str:
        return self.encrypt_text(json.dumps(payload, separators=(",", ":"), default=str))

    def decrypt_json(self, encrypted: str) -> Dict[str, Any]:
        try:
            decoded = json.loads(self.decrypt_text(encrypted))
        except json.JSONDecodeError as exc:
            raise SecurePayloadError(
                "decrypted payload is not valid JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise SecurePayloadError("decrypted payload is not a JSON object")
        return decoded


def _normalize_aes256_key(key: bytes) -> bytes:
    """Match PHP/OpenSSL passphrase handling for AES-256.

    PHP's openssl_encrypt/decrypt accepts an arbitrary passphrase string and
    OpenSSL uses the first required bytes, padding shorter passphrases with NUL.
    Node's createCipheriv and cryptography require an exact 32-byte key.
    """
    if len(key) >= 32:
        return key[:32]
    return key.ljust(32, b"\0")


def _decode_base64_key(encoded: str, label: str) -> bytes:
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SecurePayloadError(f"{label} is not valid base64") from exc


def _decode_base64_payload(encoded: str) -> bytes:
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SecurePayloadError("payload is not valid base64") from exc
