"""DaVinci end-to-end: alarms, wafer start/stop, and measurement data
through the full pipeline (mapper -> outbox -> publisher) with a fake
MQTT client. Run with -s to print the exact JSON that would reach Linkstuffs.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple


from eap_middleware.mapper import CanonicalMapper
from eap_middleware.models import LinkstuffsConfig, MachineConfig
from eap_middleware.outbox import SQLiteOutbox
from eap_middleware.profiles import ProfileRegistry
from eap_middleware.job_tracker import JobTracker
from eap_middleware.linkstuffs import (
    LINKSTUFFS_TOPIC_ATTRIBUTES,
    LINKSTUFFS_TOPIC_CONNECT,
    LINKSTUFFS_TOPIC_TELEMETRY,
)
from tests.test_mqtt_loopback import _LoopbackPublisher, _wait_for_publish_count


DAVINCI_DISPLAY = "DAVINCI200_MC4_HC1_01"


def _davinci_machine(tmp_path) -> MachineConfig:
    return MachineConfig(
        endpoint_id="TOOL_02",
        display_name=DAVINCI_DISPLAY,
        machine_profile="davinci_200_mc4_hc1",
        host="10.10.20.32",
        port=5000,
        secs_device_id=0,
        local_csv_path=str(tmp_path / "local"),
        network_csv_path=str(tmp_path / "network"),
        admin_config_path=str(tmp_path / "admin"),
    )


def _print_published(label: str, publishes: List[Tuple[str, Dict[str, Any]]]) -> None:
    """Pretty-print what landed on the (fake) broker so the user can copy
    into a Linkstuffs ticket or run by their manager."""
    print(f"\n=== {label} ===")
    for topic, payload in publishes:
        print(f"  topic:   {topic}")
        print(f"  payload: {json.dumps(payload, indent=2, default=str)}")


def test_davinci_alarm_wafer_lifecycle_and_measurement_publish(tmp_path, capsys):
    """The single comprehensive proof. Drives all three data categories
    through the middleware against a fake broker and asserts the wire
    shape of each."""
    registry = ProfileRegistry()
    machine = _davinci_machine(tmp_path)
    profile = registry.get(machine.machine_profile)

    tracker = JobTracker()
    mapper = CanonicalMapper(profile, tracker=tracker)
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3", retention_days=1)
    publisher = _LoopbackPublisher(
        config=LinkstuffsConfig(
            enabled=True, host="127.0.0.1", port=1883,
            access_token="fake-token", client_id="davinci-proof",
        ),
        outbox=outbox,
    )

    try:
        publisher.start()

        # 1) Register DaVinci as a downstream device on the gateway
        publisher.queue_machine_connect(machine)
        publisher.queue_machine_attributes(machine, profile)

        # 2) Carrier arrives on LP1 so the JobTracker knows where PM events go
        carrier_ev = mapper.from_secs_event(
            machine, 3160001,  # LP1/CarrierArrived
            {"DATETIME": "20251128094659", "_v_raw": []},
        )
        publisher.queue_event(carrier_ev)
        assert tracker.snapshot(machine.endpoint_id)["active_lp"] == "1"

        # 3) DATA CATEGORY 1: Wafer start (E90 NeedsProcessing2InProcess)
        wafer_start = mapper.from_secs_event(
            machine, 3220013,
            {
                "DATETIME": "20251128094700",
                # 13-DV substrate list (E90 NeedsProcessing2InProcess)
                "_v_raw": [
                    ["W001"], ["LP1.Slot1"], ["PM1"], ["LP1"], [],
                    ["NeedsProcessing"], [], ["W001"], ["InProcess"],
                    ["WaitingForHost"], ["LOT_M42"], ["Production"], [],
                ],
            },
        )
        publisher.queue_event(wafer_start)
        assert wafer_start.event_type == "wafer_start"
        assert wafer_start.load_port == "1"  # via tracker, not payload

        # 4) DATA CATEGORY 2: Alarm (DaVinci ALID 5010001 - Aligner alarm)
        alarm_ev = mapper.alarm_event(
            machine,
            {
                "alid": 5010001,
                "altx": "Aligner: Analog Input Channels in Manual Mode",
                "is_set": True,
                "DATETIME": "20251128094705",
            },
        )
        publisher.queue_event(alarm_ev)
        assert alarm_ev.event_type == "alarm"
        assert alarm_ev.ceid == 5010001
        assert "Aligner" in alarm_ev.secs_raw_event

        # 5) DATA CATEGORY 3: Measurement data
        # PM1/ProcessingFinished V = [WaferID, LotID, RecipeName, ResultFile,
        #                             ResultPath, PathOfImages, TestResults]
        # TestResults is a nested SECS list of per-die measurement values -
        # exactly the kind of payload that v1 silently dropped.
        test_results = [
            {"die": "1,1", "value": 1.234, "pass": True},
            {"die": "1,2", "value": 1.241, "pass": True},
            {"die": "2,1", "value": 0.998, "pass": False},
        ]
        measurement_ev = mapper.from_secs_event(
            machine, 3140003,  # PM1/ProcessingFinished
            {
                "DATETIME": "20251128094800",
                "_v_raw": [
                    "W001", "LOT_M42", "Recipe_Overlay_v3",
                    "result_20251128_094800.csv",
                    "D:/MachineData/EAP_DAVINCI200_MC4_HC1_01/results/",
                    "D:/MachineData/EAP_DAVINCI200_MC4_HC1_01/images/W001/",
                    test_results,
                ],
            },
        )
        publisher.queue_event(measurement_ev)
        assert measurement_ev.event_type == "process_end"
        assert measurement_ev.lot_id == "LOT_M42"
        assert measurement_ev.wafer_id == "W001"
        assert measurement_ev.recipe == "Recipe_Overlay_v3"
        assert measurement_ev.load_port == "1"  # via tracker

        # 6) DATA CATEGORY 1 (close): Wafer end (E90 InProcess2Processed)
        wafer_end = mapper.from_secs_event(
            machine, 3220016,
            {"DATETIME": "20251128094805", "_v_raw": []},
        )
        publisher.queue_event(wafer_end)
        assert wafer_end.event_type == "wafer_end"

        # 7) Wait for the publisher to drain everything to the fake broker.
        # Expect: 1 connect + 1 attributes + 5 telemetry = 7 publishes
        _wait_for_publish_count(publisher.fake_client, expected=7, timeout=5.0)

    finally:
        publisher.stop()

    publishes = publisher.fake_client.publishes
    _print_published("DaVinci -> Linkstuffs wire payloads", publishes)

    # --- assertions ---

    topics = [t for t, _ in publishes]
    assert topics.count(LINKSTUFFS_TOPIC_CONNECT) == 1
    assert topics.count(LINKSTUFFS_TOPIC_ATTRIBUTES) == 1
    assert topics.count(LINKSTUFFS_TOPIC_TELEMETRY) == 5

    telemetry = [p for t, p in publishes if t == LINKSTUFFS_TOPIC_TELEMETRY]
    by_event_type = {
        p[DAVINCI_DISPLAY][0]["values"]["event_type"]: p[DAVINCI_DISPLAY][0]
        for p in telemetry
    }
    assert set(by_event_type) == {
        "loaded", "wafer_start", "alarm", "process_end", "wafer_end",
    }

    # ALARM payload shape
    alarm_payload = by_event_type["alarm"]["values"]
    assert alarm_payload["event_type"] == "alarm"
    assert alarm_payload["ceid"] == 5010001
    assert "Aligner" in alarm_payload["secs_raw_event"]
    assert alarm_payload["raw_event_name"] == "AlarmSet"

    # WAFER_START payload shape
    ws = by_event_type["wafer_start"]["values"]
    assert ws["event_type"] == "wafer_start"
    assert ws["load_port"] == "1"
    assert ws["ceid"] == 3220013

    # MEASUREMENT payload - this is the load-bearing test for the v2 fix.
    # TestResults must be present as a JSON-serialized string (NOT dropped).
    meas = by_event_type["process_end"]["values"]
    assert meas["event_type"] == "process_end"
    assert meas["lot_id"] == "LOT_M42"
    assert meas["wafer_id"] == "W001"
    assert meas["recipe"] == "Recipe_Overlay_v3"
    assert meas["load_port"] == "1"
    # Per-DV fields from V[] decode
    assert meas["raw_ResultFile"] == "result_20251128_094800.csv"
    assert "EAP_DAVINCI200_MC4_HC1_01/results" in meas["raw_ResultPath"]
    # Measurement payload as JSON string
    assert isinstance(meas["raw_TestResults"], str), (
        "TestResults must be JSON-serialized for Linkstuffs transport "
        "(was previously silently dropped because lists aren't scalars)"
    )
    parsed = json.loads(meas["raw_TestResults"])
    assert isinstance(parsed, list) and len(parsed) == 3
    assert parsed[0]["die"] == "1,1"
    assert parsed[2]["pass"] is False

    # WAFER_END payload shape
    we = by_event_type["wafer_end"]["values"]
    assert we["event_type"] == "wafer_end"
    assert we["ceid"] == 3220016
