from __future__ import annotations

from types import SimpleNamespace

import pytest

from eap_middleware.config import ConfigError, machine_from_dict, service_config_from_dict
from eap_middleware.csv_store import LotBuffer, PerLotCsvWriter
from eap_middleware.linkstuffs import LinkstuffsGatewayPublisher
from eap_middleware.linkstuffs_http import LinkstuffsHttpPublisher
from eap_middleware.models import (
    CsvRow,
    LinkstuffsConfig,
    LinkstuffsHttpConfig,
    MachineConfig,
)
from eap_middleware.outbox import SQLiteOutbox
from eap_middleware.profiles import ProfileRegistry
from gateway.event_subscription import (
    EventSubscriptionManager,
    ReportDefinition,
    SubscriptionConfig,
)
from gateway.host import GatewayHost


def _machine(tmp_path=None) -> MachineConfig:
    return MachineConfig(
        endpoint_id="TOOL",
        display_name="DAV",
        machine_profile="davinci_200_mc4_hc1",
        host="127.0.0.1",
        port=5000,
        local_csv_path=str(tmp_path / "local") if tmp_path else None,
    )


def test_disabled_publishers_create_no_outbox_rows(tmp_path):
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _machine(tmp_path)
    mqtt_outbox = SQLiteOutbox(tmp_path / "mqtt.sqlite3")
    mqtt = LinkstuffsGatewayPublisher(
        LinkstuffsConfig(enabled=False, access_token="hardcoded"), mqtt_outbox
    )
    mqtt.queue_machine_connect(machine)
    mqtt.queue_machine_attributes(machine, profile)
    assert mqtt_outbox.pending() == []

    http_outbox = SQLiteOutbox(tmp_path / "http.sqlite3")
    http = LinkstuffsHttpPublisher(
        LinkstuffsHttpConfig(
            enabled=False,
            base_url="https://example.invalid",
            device_tokens={machine.display_name: "hardcoded"},
        ),
        http_outbox,
    )
    http.queue_machine_attributes(machine, profile)
    assert http_outbox.pending() == []


def _http_only_config(token: str = "tok"):
    return {
        "linkstuffs": {"enabled": False},
        "linkstuffs_http": {
            "enabled": True,
            "base_url": "https://thingsboard.invalid",
            "device_tokens": {"DAV": token} if token else {},
        },
        "machines": [{
            "endpoint_id": "TOOL",
            "display_name": "DAV",
            "machine_profile": "davinci_200_mc4_hc1",
            "host": "127.0.0.1",
            "port": 5000,
            "secs_device_id": 0,
        }],
    }


def test_every_enabled_machine_requires_an_https_or_mqtt_route():
    with pytest.raises(ConfigError, match="device_tokens|upstream route"):
        service_config_from_dict(_http_only_config(token=""))


@pytest.mark.parametrize(
    ("field", "value"),
    [("port", 0), ("port", 65536), ("secs_device_id", -1), ("secs_device_id", 32768)],
)
def test_machine_protocol_ranges_are_validated(field, value):
    data = _http_only_config()["machines"][0]
    data[field] = value
    with pytest.raises(ConfigError, match=field):
        machine_from_dict(data, ProfileRegistry())


def test_retry_and_retention_ranges_are_validated():
    data = _http_only_config()
    data["linkstuffs_http"]["retry_count"] = -1
    with pytest.raises(ConfigError, match="retry_count"):
        service_config_from_dict(data)
    data = _http_only_config()
    data["outbox_retention_days"] = -1
    with pytest.raises(ConfigError, match="retention"):
        service_config_from_dict(data)


def test_csv_buffer_survives_local_write_failure(tmp_path, monkeypatch):
    writer = PerLotCsvWriter()
    key = ("TOOL", "1")
    writer._buffers[key] = LotBuffer(
        machine=_machine(tmp_path),
        lot_id="LOT",
        load_port="1",
        rows=[CsvRow("now", "Lot_End", "DAV", "1", "PM1", "LOT", "W1", "R", "raw")],
    )
    monkeypatch.setattr(
        writer, "_write_buffer", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError, match="disk full"):
        writer._write_and_remove(key, "test")
    assert key in writer._buffers


def _bare_host() -> GatewayHost:
    host = object.__new__(GatewayHost)
    host.tool_id = "DAV"
    host._last_event_time = None
    host.stream_function = lambda _s, _f: (lambda value=None: value)
    return host


def test_s6f11_callback_failure_returns_nonzero_ack():
    host = _bare_host()
    host._decode_packet_data = lambda *_args: [0, 3140002, [[1003140002, ["W", "L", "R"]]]]
    host._on_event = lambda *_args: (_ for _ in ()).throw(RuntimeError("storage failed"))
    assert host._handle_s6f11(None, SimpleNamespace(data=b"message")) == 1


def test_s5f1_callback_failure_returns_nonzero_ack():
    host = _bare_host()
    host._decode_packet_data = lambda *_args: [0x83, 42, "alarm"]
    host._on_alarm = lambda *_args: (_ for _ in ()).throw(RuntimeError("storage failed"))
    assert host._handle_s5f1(None, SimpleNamespace(data=b"message")) == 1


def test_s6f11_preserves_all_reports_and_selects_owned_report():
    host = _bare_host()
    parsed = host._parse_event_data([
        0,
        3140002,
        [
            [77, ["unrelated"]],
            [1003140002, ["W1", "LOT", "RCP"]],
        ],
    ])
    assert len(parsed["_reports_raw"]) == 2
    assert parsed["_rptid"] == 1003140002
    assert parsed["_v_raw"] == ["W1", "LOT", "RCP"]


def test_request_status_none_sends_empty_all_svid_request():
    host = _bare_host()
    captured = []
    host.stream_function = lambda _s, _f: (
        lambda payload: captured.append(payload) or payload
    )
    host.send_and_waitfor_response = lambda _message: None
    assert host.request_status(None) == {}
    assert captured == [[]]


class _SubscriptionHost:
    def __init__(self):
        self.responses = iter([3, 0, 0])
        self.sent = []

    def stream_function(self, _stream, _function):
        return lambda payload: payload

    def send_and_waitfor_response(self, message):
        self.sent.append(message)
        return next(self.responses)


def test_drack_collision_deletes_then_redefines_owned_reports():
    host = _SubscriptionHost()
    manager = EventSubscriptionManager(
        host,
        config=SubscriptionConfig(
            reports=[ReportDefinition(1003140002, "PM", [1, 2, 3])]
        ),
    )
    assert manager.define_reports() is True
    assert host.sent[0]["DATA"][0]["VID"] == [1, 2, 3]
    assert host.sent[1]["DATA"][0]["VID"] == []
    assert host.sent[2]["DATA"][0]["VID"] == [1, 2, 3]


def test_generated_davinci_machine_snippet_uses_current_schema():
    import yaml
    from pathlib import Path

    path = Path("output/davinci200_mc4_hc1/gateway_config_snippet.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    machine = machine_from_dict(raw["machines"][0], ProfileRegistry())
    assert machine.endpoint_id == "DAVINCI200_MC4_HC1"
    assert machine.machine_profile == "davinci_200_mc4_hc1"
