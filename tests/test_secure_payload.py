import base64
import time
from datetime import datetime, timezone

import pytest

from eap_middleware.legacy_api import LegacyApiPublisher, build_legacy_api_payload
from eap_middleware.models import CanonicalEvent, LegacyApiConfig
from eap_middleware.outbox import SQLiteOutbox
from eap_middleware.profiles import ProfileRegistry
from eap_middleware.secure_payload import SecurePayloadCodec, SecurePayloadError


FIRST_KEY_B64 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
SECOND_KEY_B64 = (
    "ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj9AQUJDREVGR0hJSktMTU5PUFFSU1RV"
    "VldYWVpbXF1eXw=="
)
KNOWN_PLAINTEXT = (
    '{"Status":"Ok","ToolEvent":"Lot_Start","EAP_ToolName":"SPTS_fxP_OMEGA_01"}'
)
KNOWN_ENCRYPTED = (
    "8PHy8/T19vf4+fr7/P3+/1asS+jBftejnZJwfYY9tOTuqP+utxnxlmhrdWn+FUyeQoVQfypx"
    "YBb0rAL2LHU5vYdAHVeraGLl5nz8rwNQJOzpIp75QuL1uHhTxBsrEE82njAULLRcUa8ahW8D"
    "Bhjy0coYQClgOtm6PcRwT8E7neMHCa5I9MrFcUNPj6uaGwj7+Uk+njgjytPY8A=="
)


def test_decrypts_n8n_aes_ctr_sha3_512_payload_vector():
    codec = SecurePayloadCodec.from_base64_keys(FIRST_KEY_B64, SECOND_KEY_B64)

    assert codec.decrypt_text(KNOWN_ENCRYPTED) == KNOWN_PLAINTEXT


def test_encrypt_json_uses_iv_hmac_ciphertext_layout(monkeypatch):
    codec = SecurePayloadCodec.from_base64_keys(FIRST_KEY_B64, SECOND_KEY_B64)
    monkeypatch.setattr(
        "eap_middleware.secure_payload.os.urandom",
        lambda count: bytes(range(240, 240 + count)),
    )

    encrypted = codec.encrypt_json(
        {
            "Status": "Ok",
            "ToolEvent": "Lot_Start",
            "EAP_ToolName": "SPTS_fxP_OMEGA_01",
        }
    )
    raw = base64.b64decode(encrypted)

    assert encrypted == KNOWN_ENCRYPTED
    assert raw[:16] == bytes(range(240, 256))
    assert len(raw[16:80]) == 64
    assert codec.decrypt_json(encrypted)["ToolEvent"] == "Lot_Start"


def test_tampered_payload_is_rejected():
    codec = SecurePayloadCodec.from_base64_keys(FIRST_KEY_B64, SECOND_KEY_B64)
    raw = bytearray(base64.b64decode(KNOWN_ENCRYPTED))
    raw[-1] ^= 1

    with pytest.raises(SecurePayloadError, match="HMAC"):
        codec.decrypt_text(base64.b64encode(raw).decode("ascii"))


def test_aes_256_gcm_v2_round_trip_has_versioned_envelope(monkeypatch):
    codec = SecurePayloadCodec.from_aes256_gcm_key_base64(FIRST_KEY_B64)
    monkeypatch.setattr(
        "eap_middleware.secure_payload.os.urandom",
        lambda count: bytes(range(count)),
    )

    encrypted = codec.encrypt_json({"ToolEvent": "Lot_Start"})

    assert encrypted.startswith("v2.")
    raw = base64.b64decode(encrypted.removeprefix("v2."))
    assert raw[:12] == bytes(range(12))
    assert codec.decrypt_json(encrypted) == {"ToolEvent": "Lot_Start"}


@pytest.mark.parametrize("offset", [0, 12, -1])
def test_aes_256_gcm_v2_rejects_nonce_ciphertext_and_tag_tampering(offset):
    codec = SecurePayloadCodec.from_aes256_gcm_key_base64(FIRST_KEY_B64)
    encrypted = codec.encrypt_text("authenticated payload")
    raw = bytearray(base64.b64decode(encrypted.removeprefix("v2.")))
    raw[offset] ^= 1

    with pytest.raises(SecurePayloadError, match="authentication failed"):
        codec.decrypt_text("v2." + base64.b64encode(raw).decode("ascii"))


def test_aes_256_gcm_v2_rejects_wrong_key_and_legacy_downgrade():
    codec = SecurePayloadCodec.from_aes256_gcm_key_base64(FIRST_KEY_B64)
    other_key = base64.b64encode(bytes(reversed(range(32)))).decode("ascii")
    other = SecurePayloadCodec.from_aes256_gcm_key_base64(other_key)
    encrypted = codec.encrypt_text("authenticated payload")

    with pytest.raises(SecurePayloadError, match="authentication failed"):
        other.decrypt_text(encrypted)
    with pytest.raises(SecurePayloadError, match="not AES-256-GCM v2"):
        codec.decrypt_text(KNOWN_ENCRYPTED)


@pytest.mark.parametrize("plaintext", ["not json", "[]"])
def test_aes_256_gcm_v2_rejects_non_object_json(plaintext):
    codec = SecurePayloadCodec.from_aes256_gcm_key_base64(FIRST_KEY_B64)

    with pytest.raises(SecurePayloadError, match="valid JSON|JSON object"):
        codec.decrypt_json(codec.encrypt_text(plaintext))


@pytest.mark.parametrize(
    "key_b64",
    ["not base64!", base64.b64encode(b"short").decode("ascii")],
)
def test_aes_256_gcm_v2_requires_exactly_32_base64_key_bytes(key_b64):
    with pytest.raises(SecurePayloadError, match="base64|exactly 32 bytes"):
        SecurePayloadCodec.from_aes256_gcm_key_base64(key_b64)


def test_explicit_raw_keys_round_trip_spec_plaintext_sample():
    spec_plaintext = {
        "TokenID": "skd29f-kd204j-6b34fc-wpfd20-d01kru",
        "EAP_ToolName": "SPTS_fxP_OMEGA_01",
        "DatetimeStart": "2025-11-28 09:46:59.345559",
        "DatetimeEnd": "",
        "ToolEvent": "Lot_Start",
        "LoadPort": "1",
        "LotID": "25110302-08 rap si",
        "Recipe": "",
        "SECSGEM_Raw_Event": "LotStarted",
        "LotCsvFileName": "SPTS_fxP_OMEGA_01_Lot_20251128_094120_459318.csv",
        "Status": "Ok",
        "ErrorMsg": "",
    }
    codec = SecurePayloadCodec.from_raw_keys(
        "explicit-test-first-key-not-for-production",
        "explicit-test-second-key-not-for-production",
    )
    encrypted = codec.encrypt_json(spec_plaintext)
    assert codec.decrypt_json(encrypted) == spec_plaintext


def test_raw_key_codec_supports_php_style_arbitrary_passphrases():
    codec = SecurePayloadCodec.from_raw_keys(
        "short-engineering-first-key",
        "second-key-used-for-hmac",
    )
    payload = {
        "TokenID": "test-token",
        "EAP_ToolName": "SPTS_fxP_OMEGA_01",
        "ToolEvent": "Lot_Start",
    }

    encrypted = codec.encrypt_json(payload)

    assert codec.decrypt_json(encrypted) == payload


def test_legacy_api_payload_and_encrypted_request_body(tmp_path):
    profile = ProfileRegistry().get("spts_fxp_omega")
    event = CanonicalEvent(
        timestamp=datetime(2025, 11, 28, 9, 46, 59, 345559, tzinfo=timezone.utc),
        endpoint_id="TOOL_01",
        display_name="SPTS_fxP_OMEGA_01",
        machine_profile="spts_fxp_omega",
        vendor=profile.vendor,
        model=profile.model,
        event_type="lot_start",
        raw_event_name="LotStarted",
        load_port="1",
        lot_id="25110302-08 rap si",
        recipe="",
        secs_raw_event="LotStarted",
    )
    config = LegacyApiConfig(
        enabled=True,
        url="https://example.invalid/webhook",
        encrypted=True,
        encryption_mode="legacy_ctr_v1",
        first_key_b64=FIRST_KEY_B64,
        second_key_b64=SECOND_KEY_B64,
        token_id="test-token",
    )
    publisher = LegacyApiPublisher(config, SQLiteOutbox(tmp_path / "legacy.sqlite3"))

    payload = build_legacy_api_payload(event, profile, token_id=config.token_id)
    body = publisher._request_body(payload)

    assert payload["Status"] == "Ok"
    assert payload["DatetimeStart"] == "2025-11-28 09:46:59.345559"
    assert payload["DatetimeEnd"] == ""
    assert payload["ToolEvent"] == "Lot_Start"
    assert payload["SECSGEM_Raw_Event"] == "LotStarted"
    assert set(body) == {"data"}
    assert publisher._codec is not None
    assert publisher._codec.decrypt_json(body["data"]) == payload


def test_legacy_api_publisher_uses_aes_256_gcm_v2(tmp_path):
    config = LegacyApiConfig(
        enabled=True,
        url="https://example.invalid/webhook",
        encrypted=True,
        encryption_mode="aes_256_gcm_v2",
        encryption_key_b64=FIRST_KEY_B64,
    )
    publisher = LegacyApiPublisher(
        config, SQLiteOutbox(tmp_path / "legacy-v2.sqlite3")
    )

    body = publisher._request_body({"Status": "Ok"})

    assert body["data"].startswith("v2.")
    assert publisher._codec is not None
    assert publisher._codec.decrypt_json(body["data"]) == {"Status": "Ok"}


def test_legacy_api_transient_failure_advances_attempts_without_stalling(tmp_path):
    outbox = SQLiteOutbox(tmp_path / "legacy-failure.sqlite3")
    config = LegacyApiConfig(
        enabled=True,
        url="https://example.invalid/webhook",
        encrypted=False,
    )
    publisher = LegacyApiPublisher(config, outbox)
    outbox.enqueue(
        config.url,
        {"Status": "Ok"},
        key="legacy-failure",
        partition_key="TOOL_01",
    )
    publisher._post_json = lambda *_args: (False, "temporary outage")

    publisher.start()
    try:
        deadline = time.time() + 2.0
        while time.time() < deadline and outbox.attempts(1) == 0:
            time.sleep(0.01)
    finally:
        publisher.stop()

    assert outbox.attempts(1) == 1
    assert outbox.stats()["pending"] == 1
