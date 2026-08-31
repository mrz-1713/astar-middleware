"""End-to-end: realistic DaVinci simulator <-HSMS-> middleware -> CSV + MQTT.

SecsGemEquipment runs PASSIVE and replays a full lot using the real DaVinci
CEIDs from the SECS-Items workbook. The middleware connects ACTIVE, parses
the events, runs them through the canonical mapper + JobTracker, writes the
per-lot CSV, and publishes telemetry to a fake MQTT broker.

This is the closest we can get to "DaVinci really works" without real tools.

Skipped automatically if secsgem isn't installed.
"""

from __future__ import annotations

import json
import socket
import struct
import time

import pytest

pytest.importorskip("secsgem")
pytest.importorskip("paho.mqtt.client")

import secsgem.hsms
from eap_middleware.csv_store import PerLotCsvWriter
from eap_middleware.job_tracker import JobTracker
from eap_middleware.linkstuffs import LINKSTUFFS_TOPIC_TELEMETRY
from eap_middleware.mapper import CanonicalMapper
from eap_middleware.models import LinkstuffsConfig, MachineConfig
from eap_middleware.outbox import SQLiteOutbox
from eap_middleware.profiles import ProfileRegistry
from eap_middleware.secs_runtime import SecsMachineSession

from simulator.secsgem_equipment import SecsGemEquipment
from tests.test_mqtt_loopback import _LoopbackPublisher


def _free_port() -> int:
    for _ in range(20):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
            probe.close()
            return port
        except OSError:
            probe.close()
            continue
    raise RuntimeError("Could not find a free, immediately-rebindable port")


def test_davinci_simulator_full_lot_flows_to_csv_and_mqtt(tmp_path):
    port = _free_port()
    display = "DAVINCI200_MC4_HC1_SIM"
    wafer_count = 2  # keep the test fast - 2 wafers prove the loop works

    # --- DaVinci simulator (PASSIVE) ---
    sim_settings = secsgem.hsms.HsmsSettings(
        address="127.0.0.1",
        port=port,
        connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
        session_id=0,
    )
    simulator = SecsGemEquipment(
        settings=sim_settings,
        tool_id="DAV_SIM_TEST",
        wafer_count=wafer_count,
        step_interval_sec=0.05,   # tight loop for tests
        fire_alarm=True,
        loop_lots=False,
    )

    # --- middleware (ACTIVE, dials simulator) ---
    machine = MachineConfig(
        endpoint_id="TOOL_02",
        display_name=display,
        machine_profile="davinci_200_mc4_hc1",
        host="127.0.0.1",
        port=port,
        secs_device_id=0,
        local_csv_path=str(tmp_path / "local"),
        network_csv_path=str(tmp_path / "network"),
        admin_config_path=str(tmp_path / "admin"),
        hsms_mode="active",
    )
    profile = ProfileRegistry().get(machine.machine_profile)
    tracker = JobTracker()
    mapper = CanonicalMapper(profile, tracker=tracker)
    csv_writer = PerLotCsvWriter()
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    publisher = _LoopbackPublisher(
        config=LinkstuffsConfig(
            enabled=True, host="127.0.0.1", port=1883,
            access_token="fake", client_id="dav-sim-e2e",
        ),
        outbox=outbox,
    )

    events_received = []
    alarms_received = []

    def on_event(_machine, ceid, data):
        events_received.append((ceid, data))
        ev = mapper.from_secs_event(_machine, ceid, data)
        csv_writer.append(_machine, profile, ev)
        publisher.queue_event(ev)

    def on_alarm(_machine, alarm):
        alarms_received.append(alarm)
        ev = mapper.alarm_event(_machine, alarm)
        publisher.queue_event(ev)

    session = SecsMachineSession(
        machine=machine,
        event_callback=on_event,
        alarm_callback=on_alarm,
        connect_callback=lambda *a, **k: None,
        disconnect_callback=lambda *a, **k: None,
    )

    try:
        publisher.start()
        simulator.enable()
        simulator.start_events()
        time.sleep(0.3)  # let listener bind
        session.start()

        # Wait for the simulator to drive the lot end. With 2 wafers and
        # 0.05s steps the run takes ~1.5s; allow generous slack for HSMS.
        deadline = time.time() + 30.0
        target_ceids = {3050001, 3140002, 3140003, 3220016, 3160002, 3050002}
        while time.time() < deadline:
            seen = {c for c, _ in events_received if c > 0}
            if target_ceids.issubset(seen):
                break
            time.sleep(0.1)

        # Wait for the publisher to drain
        time.sleep(0.5)

    finally:
        try:
            session.stop()
        except Exception:
            pass
        try:
            simulator.disable()
        except Exception:
            pass
        publisher.stop()
        csv_writer.flush_all(reason="test_teardown")

    # --- assertions ---

    seen_ceids = {c for c, _ in events_received if c > 0}
    assert 3050001 in seen_ceids, "MaterialReceived missing"
    assert 3140002 in seen_ceids, "PM1/ProcessingStarted missing"
    assert 3140003 in seen_ceids, "PM1/ProcessingFinished missing"
    assert 3220016 in seen_ceids, "InProcess2Processed missing"
    assert 3160002 in seen_ceids, "LP1/CarrierDeparted missing"

    # Alarm path exercised
    assert alarms_received, "expected at least one S5F1 alarm during the lot"
    set_alarms = [a for a in alarms_received if a.get("is_set")]
    assert set_alarms, "expected at least one alarm with is_set=True"
    assert any(a.get("alid") == 5010001 for a in set_alarms)

    # Telemetry shape
    telemetry = [
        p for t, p in publisher.fake_client.publishes
        if t == LINKSTUFFS_TOPIC_TELEMETRY
    ]
    by_event_type = {}
    for p in telemetry:
        entries = p[display]
        for entry in entries:
            by_event_type.setdefault(entry["values"]["event_type"], []).append(entry)

    # Each expected event_type at least once
    for et in ("mounted", "process_start", "process_end", "wafer_end",
               "unloaded", "alarm"):
        assert et in by_event_type, f"missing telemetry event_type={et}"

    # process_end MUST carry the measurement payload (TestResults)
    process_end_payloads = by_event_type["process_end"]
    pe_values = process_end_payloads[0]["values"]
    assert pe_values["lot_id"].startswith("LOT_SIM_"), pe_values["lot_id"]
    assert pe_values["wafer_id"]
    assert pe_values["recipe"] == "Recipe_Overlay_v3"
    assert pe_values["load_port"] == "1"  # JobTracker filled it
    assert "raw_TestResults" in pe_values, "measurement payload missing!"
    # TestResults survives as a JSON string in MQTT (could be a list-of-strings
    # since simulator JSON-encodes each dict, OR a list-of-dicts depending on
    # secsgem's recursive list handling). Either way it must parse and be non-empty.
    parsed = json.loads(pe_values["raw_TestResults"])
    assert isinstance(parsed, list) and len(parsed) >= 5, parsed

    # Per-lot CSV file produced (closed on CarrierDeparted)
    csvs = list((tmp_path / "local").glob("*.csv"))
    assert csvs, "expected at least one per-lot CSV file"
    assert any(display in c.name for c in csvs)
