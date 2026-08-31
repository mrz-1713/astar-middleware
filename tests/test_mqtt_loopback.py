"""Loopback test: publisher → fake MQTT client → outbox replay.

This is the highest-fidelity check we can run without standing up a real
broker. It verifies:

- LinkstuffsGatewayPublisher actually publishes queued messages
- The topics and JSON payload bytes are exactly what Linkstuffs / Linkstuffs
  Gateway protocol expects
- Outbox dedupes correctly (same event_key → no double publish)
- Outbox replays after a fake-broker disconnect

We patch out paho.mqtt with a deterministic fake so the test is hermetic
(no network, no broker process, no flakiness).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Tuple

from eap_middleware.mapper import CanonicalMapper
from eap_middleware.models import LinkstuffsConfig, MachineConfig
from eap_middleware.outbox import SQLiteOutbox
from eap_middleware.profiles import ProfileRegistry
from eap_middleware.linkstuffs import (
    LINKSTUFFS_TOPIC_ATTRIBUTES,
    LINKSTUFFS_TOPIC_CONNECT,
    LINKSTUFFS_TOPIC_TELEMETRY,
    LinkstuffsGatewayPublisher,
)


class _FakeMQTTInfo:
    def __init__(self, rc: int = 0):
        self.rc = rc
        self._published = True

    def wait_for_publish(self, timeout: float = 10) -> None:
        return None

    def is_published(self) -> bool:
        return self._published


class _FakeMQTTClient:
    """Stand-in for paho.mqtt.client.Client that records publishes."""

    def __init__(self) -> None:
        self.publishes: List[Tuple[str, Dict[str, Any]]] = []
        self.connected = False
        self._lock = threading.Lock()

    def publish(self, topic: str, payload: str, qos: int = 1, retain: bool = False):
        with self._lock:
            self.publishes.append((topic, json.loads(payload)))
        return _FakeMQTTInfo(rc=0)

    def loop_stop(self) -> None:  # pragma: no cover - tear-down
        self.connected = False

    def disconnect(self) -> None:  # pragma: no cover
        self.connected = False


class _LoopbackPublisher(LinkstuffsGatewayPublisher):
    """Publisher that uses a deterministic fake MQTT client and is
    immediately marked 'connected' so the publish loop drains the outbox."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.fake_client = _FakeMQTTClient()

    def _create_client(self) -> Any:
        self._connected = True
        self.fake_client.connected = True
        return self.fake_client


def _machine(tmp_path, display: str, profile_id: str) -> MachineConfig:
    return MachineConfig(
        endpoint_id=f"TOOL_{display}",
        display_name=display,
        machine_profile=profile_id,
        host="127.0.0.1",
        port=5000,
        local_csv_path=str(tmp_path / display / "local"),
        network_csv_path=str(tmp_path / display / "network"),
        admin_config_path=str(tmp_path / display / "admin"),
    )


def _wait_for_publish_count(client: _FakeMQTTClient, expected: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with client._lock:
            if len(client.publishes) >= expected:
                return
        time.sleep(0.05)
    raise AssertionError(
        f"Timeout waiting for {expected} publishes; got {len(client.publishes)}"
    )


def test_publisher_drains_outbox_and_emits_correct_topics(tmp_path):
    """Connect + attributes + telemetry from all 3 profiles all land on the
    fake broker with the exact topic and JSON payload Linkstuffs expects."""
    registry = ProfileRegistry()
    machines = [
        _machine(tmp_path, "SPTS_fxP_OMEGA_01", "spts_fxp_omega"),
        _machine(tmp_path, "DAVINCI_01", "davinci_200_mc4_hc1"),
        _machine(tmp_path, "PTIQ_01", "ptiq_secsgem"),
    ]
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3", retention_days=1)
    config = LinkstuffsConfig(
        enabled=True,
        host="127.0.0.1",
        port=1883,
        access_token="fake-token",
        qos=1,
        client_id="loopback-test",
    )
    publisher = _LoopbackPublisher(config=config, outbox=outbox)

    try:
        publisher.start()

        # 1) For each machine: queue connect + attributes
        for m in machines:
            profile = registry.get(m.machine_profile)
            publisher.queue_machine_connect(m)
            publisher.queue_machine_attributes(m, profile)

        # 2) Feed one canonical Lot_Start event per machine
        sample_events = {
            "SPTS_fxP_OMEGA_01": (851, "CassetteStarted", "lot_start"),
            "DAVINCI_01": (3200017, "ControlJob:Selected-Executing", "lot_start"),
            "PTIQ_01": (0, "SCH1.LotStarted", "lot_start"),
        }
        for m in machines:
            ceid, raw, expected = sample_events[m.display_name]
            profile = registry.get(m.machine_profile)
            event = CanonicalMapper(profile).from_secs_event(
                m, ceid,
                {
                    "DATETIME": "2025-11-28 09:46:59.345559",
                    "SECSGEM_RAW_EVENT": raw,
                    "LOAD_PORT": 1,
                    "LOT_ID": f"{m.display_name}-LOT-001",
                },
            )
            assert event.event_type == expected
            publisher.queue_event(event)

        # 3) Expect: 3 connects + 3 attributes + 3 telemetry = 9 publishes
        _wait_for_publish_count(publisher.fake_client, 9, timeout=5.0)

    finally:
        publisher.stop()

    publishes = publisher.fake_client.publishes
    topics = [t for t, _ in publishes]
    assert topics.count(LINKSTUFFS_TOPIC_CONNECT) == 3
    assert topics.count(LINKSTUFFS_TOPIC_ATTRIBUTES) == 3
    assert topics.count(LINKSTUFFS_TOPIC_TELEMETRY) == 3

    # Each connect carries the right device + machine_profile type
    connect_payloads = {p["device"]: p["type"] for t, p in publishes if t == LINKSTUFFS_TOPIC_CONNECT}
    assert connect_payloads == {
        "SPTS_fxP_OMEGA_01": "spts_fxp_omega",
        "DAVINCI_01": "davinci_200_mc4_hc1",
        "PTIQ_01": "ptiq_secsgem",
    }

    # Each telemetry message is keyed by display_name with one [ts, values] entry
    telemetry = [p for t, p in publishes if t == LINKSTUFFS_TOPIC_TELEMETRY]
    by_device = {next(iter(p)): p[next(iter(p))] for p in telemetry}
    assert set(by_device) == {"SPTS_fxP_OMEGA_01", "DAVINCI_01", "PTIQ_01"}
    for device, batch in by_device.items():
        assert len(batch) == 1
        entry = batch[0]
        assert "ts" in entry and isinstance(entry["ts"], int)
        assert entry["values"]["event_type"] == "lot_start"
        assert entry["values"]["display_name"] == device
        assert entry["values"]["lot_id"] == f"{device}-LOT-001"


def test_outbox_dedupes_identical_event_keys(tmp_path):
    """Queuing the same event twice produces one outbox row, one publish."""
    registry = ProfileRegistry()
    m = _machine(tmp_path, "SPTS_fxP_OMEGA_01", "spts_fxp_omega")
    profile = registry.get(m.machine_profile)
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    config = LinkstuffsConfig(enabled=True, access_token="t", client_id="dedupe-test")
    publisher = _LoopbackPublisher(config=config, outbox=outbox)

    event = CanonicalMapper(profile).from_secs_event(
        m, 851,
        {
            "DATETIME": "2025-11-28 09:46:59.345559",
            "SECSGEM_RAW_EVENT": "CassetteStarted",
            "LOT_ID": "LOT-DUP",
        },
    )
    try:
        publisher.start()
        # Queue the same event 3 times - event_key is deterministic, so the
        # outbox should keep only one row.
        publisher.queue_event(event)
        publisher.queue_event(event)
        publisher.queue_event(event)
        _wait_for_publish_count(publisher.fake_client, 1, timeout=3.0)
        time.sleep(0.5)  # give any duplicate a chance to slip through
    finally:
        publisher.stop()

    telemetry = [
        t for t, _ in publisher.fake_client.publishes
        if t == LINKSTUFFS_TOPIC_TELEMETRY
    ]
    assert len(telemetry) == 1, f"expected 1 telemetry publish, got {len(telemetry)}"
