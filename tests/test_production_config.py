from pathlib import Path

import pytest

from eap_middleware.config import ConfigError, service_config_from_dict


def _base_config():
    return {
        "linkstuffs": {"enabled": True, "access_token": "token"},
        "machines": [
            {
                "endpoint_id": "TOOL_01",
                "display_name": "SPTS_fxP_OMEGA_01",
                "machine_profile": "spts_fxp_omega",
                "host": "10.0.0.1",
                "port": 5000,
                "secs_device_id": 0,
                "enabled": True,
                # MQTT-focused compatibility tests do not provision the
                # per-machine HTTPS route required for an online deployment.
                "offline_test_mode": True,
            }
        ],
    }


def test_valid_config_uses_profile_defaults():
    config = service_config_from_dict(_base_config())
    machine = config.machines[0]
    assert machine.display_name == "SPTS_fxP_OMEGA_01"
    assert machine.csv_local_dir == Path("D:/MachineData/EAP_SPTS_fxP_OMEGA_01/csv_in")
    assert config.outbox_retention_days == 30
    assert config.linkstuffs.port == 8883
    assert config.linkstuffs.tls is True
    assert config.linkstuffs.allow_insecure is False


def test_unknown_profile_rejected():
    data = _base_config()
    data["machines"][0]["machine_profile"] = "unknown"
    with pytest.raises(ConfigError, match="Unknown"):
        service_config_from_dict(data)


def test_duplicate_display_name_rejected():
    data = _base_config()
    second = dict(data["machines"][0])
    second["endpoint_id"] = "TOOL_02"
    data["machines"].append(second)
    with pytest.raises(ConfigError, match="Duplicate display_name"):
        service_config_from_dict(data)


def test_linkstuffs_token_required_when_enabled():
    data = _base_config()
    data["linkstuffs"]["access_token"] = ""
    with pytest.raises(ConfigError, match="access_token"):
        service_config_from_dict(data)


def test_linkstuffs_token_can_come_from_environment(monkeypatch):
    data = _base_config()
    data["linkstuffs"]["access_token"] = "${LINKSTUFFS_GATEWAY_ACCESS_TOKEN}"
    monkeypatch.setenv("LINKSTUFFS_GATEWAY_ACCESS_TOKEN", "secret-token")
    config = service_config_from_dict(data)
    assert config.linkstuffs.access_token == "secret-token"


def test_plaintext_linkstuffs_requires_explicit_insecure_override():
    data = _base_config()
    data["linkstuffs"]["tls"] = False
    with pytest.raises(ConfigError, match="allow_insecure"):
        service_config_from_dict(data)


def test_plaintext_linkstuffs_can_be_explicitly_allowed_for_test_network():
    data = _base_config()
    data["linkstuffs"].update({"tls": False, "allow_insecure": True, "port": 1883})
    config = service_config_from_dict(data)
    assert config.linkstuffs.tls is False
    assert config.linkstuffs.allow_insecure is True
    assert config.linkstuffs.port == 1883


@pytest.mark.parametrize(
    "http_config",
    [
        {"base_url": "http://example.invalid", "verify_tls": True},
        {"base_url": "https://example.invalid", "verify_tls": False},
    ],
)
def test_insecure_http_requires_explicit_test_override(http_config):
    data = _base_config()
    data["linkstuffs_http"] = {
        "enabled": True,
        "device_tokens": {"SPTS_fxP_OMEGA_01": "token"},
        **http_config,
    }

    with pytest.raises(ConfigError, match="allow_insecure"):
        service_config_from_dict(data)


def test_insecure_http_can_be_explicitly_allowed_for_test_network(caplog):
    data = _base_config()
    data["linkstuffs_http"] = {
        "enabled": True,
        "base_url": "http://127.0.0.1:8080",
        "verify_tls": False,
        "allow_insecure": True,
        "device_tokens": {"SPTS_fxP_OMEGA_01": "token"},
    }

    config = service_config_from_dict(data)

    assert config.linkstuffs_http.allow_insecure is True
    assert "TEST/LAB INSECURE HTTP" in caplog.text


def test_machine_http_override_cannot_bypass_insecure_gate():
    data = _base_config()
    data["linkstuffs_http"] = {
        "enabled": False,
        "base_url": "https://example.invalid",
        "verify_tls": True,
    }
    data["machines"][0]["linkstuffs_http"] = {
        "enabled": True,
        "base_url": "http://example.invalid",
        "device_token": "machine-token",
        "verify_tls": True,
    }

    with pytest.raises(ConfigError, match="Machine TOOL_01.*allow_insecure"):
        service_config_from_dict(data)


def test_missing_environment_variable_rejected():
    data = _base_config()
    data["linkstuffs"]["access_token"] = "${MISSING_LINKSTUFFS_TOKEN}"
    with pytest.raises(ConfigError, match="MISSING_LINKSTUFFS_TOKEN"):
        service_config_from_dict(data)


def test_legacy_api_can_be_disabled_without_url_or_keys():
    data = _base_config()
    data["legacy_api"] = {
        "enabled": False,
        "encrypted": True,
    }

    config = service_config_from_dict(data)

    assert config.legacy_api.enabled is False
    assert config.legacy_api.encrypted is True


def test_legacy_api_rejects_plain_http_without_test_override():
    data = _base_config()
    data["legacy_api"] = {
        "enabled": True,
        "url": "http://127.0.0.1:8080/webhook",
        "allow_insecure": False,
        "encrypted": False,
    }

    with pytest.raises(ConfigError, match="legacy_api.*allow_insecure"):
        service_config_from_dict(data)


def test_legacy_api_allows_plain_http_with_explicit_lab_override():
    data = _base_config()
    data["legacy_api"] = {
        "enabled": True,
        "url": "http://127.0.0.1:8080/webhook",
        "allow_insecure": True,
        "encrypted": False,
    }

    config = service_config_from_dict(data)

    assert config.legacy_api.allow_insecure is True


def test_encrypted_legacy_api_accepts_raw_keys_from_environment(monkeypatch):
    data = _base_config()
    legacy_api_url = (
        "https://flow.linkbot.sg/webhook/EncryptedMachineData/api/"
        "API_MachineStatus/00_Machine_Status_Event"
    )
    data["legacy_api"] = {
        "enabled": True,
        "url": legacy_api_url,
        "encrypted": True,
        "encryption_mode": "legacy_ctr_v1",
        "first_key": "${LEGACY_API_FIRST_KEY}",
        "second_key": "${LEGACY_API_SECOND_KEY}",
        "send_tool_events": ["Lot_Start", "Lot_End"],
    }
    monkeypatch.setenv("LEGACY_API_FIRST_KEY", "first-passphrase")
    monkeypatch.setenv("LEGACY_API_SECOND_KEY", "second-passphrase")

    config = service_config_from_dict(data)

    assert config.legacy_api.enabled is True
    assert config.legacy_api.first_key == "first-passphrase"
    assert config.legacy_api.second_key == "second-passphrase"
    assert config.legacy_api.send_tool_events == ["Lot_Start", "Lot_End"]


def test_encrypted_legacy_api_rejects_missing_keys():
    data = _base_config()
    data["legacy_api"] = {
        "enabled": True,
        "url": "https://flow.linkbot.sg/webhook/EncryptedMachineData",
        "encrypted": True,
        "encryption_mode": "legacy_ctr_v1",
    }

    with pytest.raises(ConfigError, match="first_key/second_key"):
        service_config_from_dict(data)


def test_encrypted_legacy_api_requires_explicit_encryption_mode():
    data = _base_config()
    data["legacy_api"] = {
        "enabled": True,
        "url": "https://example.invalid/webhook",
        "encrypted": True,
        "first_key": "first",
        "second_key": "second",
    }

    with pytest.raises(ConfigError, match="encryption_mode must be explicit"):
        service_config_from_dict(data)


def test_aes_256_gcm_v2_config_accepts_exactly_32_byte_key():
    data = _base_config()
    data["legacy_api"] = {
        "enabled": True,
        "url": "https://example.invalid/webhook",
        "encrypted": True,
        "encryption_mode": "aes_256_gcm_v2",
        "encryption_key_b64": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
    }

    config = service_config_from_dict(data)

    assert config.legacy_api.encryption_mode == "aes_256_gcm_v2"


@pytest.mark.parametrize("key_b64", ["not-base64", "c2hvcnQ="])
def test_aes_256_gcm_v2_config_rejects_invalid_keys(key_b64):
    data = _base_config()
    data["legacy_api"] = {
        "enabled": True,
        "url": "https://example.invalid/webhook",
        "encrypted": True,
        "encryption_mode": "aes_256_gcm_v2",
        "encryption_key_b64": key_b64,
    }

    with pytest.raises(ConfigError, match="exactly 32 bytes"):
        service_config_from_dict(data)
