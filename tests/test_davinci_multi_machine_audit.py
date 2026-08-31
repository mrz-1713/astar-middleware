"""Multi-DaVinci edge-case audit.

When a fab has more than one DaVinci tool (DAVINCI200_MC4_HC1_01,
DAVINCI200_MC4_HC1_02, etc.) the middleware runs concurrent SECS sessions
that share core infrastructure: JobTracker, AlarmRateLimiter, SQLiteOutbox,
PerLotCsvWriter, and the MQTT publisher.

This file pins behavior under adversarial conditions that aren't covered by
the single-machine happy-path tests:

  1. State isolation between two DaVinci instances
  2. Out-of-order lifecycle events (deactivate before activate, etc.)
  3. Duplicate event delivery (equipment-side retry on missing S6F12 ACK)
  4. Malformed / minimal S6F11 payloads (empty V[], missing DVs)
  5. Concurrent threading - simulated 100 events from two machines
  6. CtrlJobID collisions across machines
  7. Network share write failure
  8. Empty/oversized payload data
  9. Alarm storms isolated per-machine
 10. Multiple machines firing the same CEID in the same millisecond
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import List


from eap_middleware.alarms import AlarmRateLimiter
from eap_middleware.csv_store import PerLotCsvWriter
from eap_middleware.job_tracker import JobTracker
from eap_middleware.mapper import CanonicalMapper
from eap_middleware.models import MachineConfig
from eap_middleware.profiles import ProfileRegistry


def _davinci(tmp_path: Path, suffix: str) -> MachineConfig:
    """Build a DaVinci MachineConfig with unique paths per instance."""
    return MachineConfig(
        endpoint_id=f"TOOL_{suffix}",
        display_name=f"DAVINCI200_MC4_HC1_{suffix}",
        machine_profile="davinci_200_mc4_hc1",
        host=f"10.10.20.{30 + int(suffix)}",
        port=5000,
        secs_device_id=0,
        local_csv_path=str(tmp_path / suffix / "local"),
        network_csv_path=str(tmp_path / suffix / "network"),
        admin_config_path=str(tmp_path / suffix / "admin"),
    )


# ----- 1. State isolation between two DaVinci instances -----

def test_two_davinci_machines_have_independent_lp_state(tmp_path):
    """Activating LP1 on machine A must not affect machine B's LP state."""
    tracker = JobTracker()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    mA = _davinci(tmp_path, "01")
    mB = _davinci(tmp_path, "02")
    mapper_A = CanonicalMapper(profile, tracker=tracker)
    mapper_B = CanonicalMapper(profile, tracker=tracker)

    # Machine A: LP1 activates
    mapper_A.from_secs_event(mA, 3050001, {"_v_raw": [1]})
    # Machine B: LP2 activates
    mapper_B.from_secs_event(mB, 3050001, {"_v_raw": [2]})

    snapA = tracker.snapshot(mA.endpoint_id)
    snapB = tracker.snapshot(mB.endpoint_id)
    assert snapA["active_lp"] == "1"
    assert snapB["active_lp"] == "2"

    # PM event on machine B should get its own LP, not A's
    ev = mapper_B.from_secs_event(mB, 3140002, {"_v_raw": ["W", "L", "R"]})
    assert ev.load_port == "2"


def test_two_davinci_machines_with_identical_ctrl_job_ids(tmp_path):
    """Two machines can have ControlJobs with colliding IDs (CJ-1, CJ-1) -
    the tracker must keep them per-machine, not in a shared global map."""
    tracker = JobTracker()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    mA = _davinci(tmp_path, "01")
    mB = _davinci(tmp_path, "02")
    mapper_A = CanonicalMapper(profile, tracker=tracker)
    mapper_B = CanonicalMapper(profile, tracker=tracker)

    # Both machines activate LP1 and start CtrlJob "CJ-1"
    mapper_A.from_secs_event(mA, 3050001, {"_v_raw": [1]})
    mapper_A.from_secs_event(mA, 3200017, {"CtrlJobID": "CJ-1"})
    mapper_B.from_secs_event(mB, 3050001, {"_v_raw": [2]})
    mapper_B.from_secs_event(mB, 3200017, {"CtrlJobID": "CJ-1"})

    # Machine A's CJ-1 lives on LP1, machine B's on LP2 - no cross-talk
    assert tracker.snapshot(mA.endpoint_id)["ctrl_jobs"] == {"CJ-1": "1"}
    assert tracker.snapshot(mB.endpoint_id)["ctrl_jobs"] == {"CJ-1": "2"}


# ----- 2. Out-of-order lifecycle events -----

def test_carrier_departure_before_activation_is_safe(tmp_path):
    """Equipment may send LP1/CarrierDeparted without our middleware having
    seen the corresponding MaterialReceived (e.g. we connected mid-lot).
    Deactivating an LP that was never active must NOT crash or corrupt state."""
    tracker = JobTracker()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _davinci(tmp_path, "01")
    mapper = CanonicalMapper(profile, tracker=tracker)

    # Stale-state scenario: middleware reconnected mid-lot, never saw arrival
    ev = mapper.from_secs_event(machine, 3160002, {"_v_raw": ["CID"]})
    # Should not crash, event_type resolved, state still consistent
    assert ev.event_type == "unloaded"
    assert tracker.snapshot(machine.endpoint_id)["active_lp"] is None


def test_pm_event_arrives_before_any_lp_activation(tmp_path):
    """If we connect mid-process, PM events arrive before we've seen any
    LP activation. load_port falls back to '' (NA bucket) - acceptable
    degradation, not corruption."""
    tracker = JobTracker()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _davinci(tmp_path, "01")
    mapper = CanonicalMapper(profile, tracker=tracker)

    ev = mapper.from_secs_event(
        machine, 3140002,  # PM1/ProcessingStarted
        {"_v_raw": ["W001", "LOT", "Rcp"]},
    )
    assert ev.event_type == "process_start"
    assert ev.load_port == ""  # caller normalizes to "NA"


def test_ctrl_job_end_before_start_is_safe(tmp_path):
    """ControlJob:Completed for a CJ-ID we never saw start."""
    tracker = JobTracker()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _davinci(tmp_path, "01")
    mapper = CanonicalMapper(profile, tracker=tracker)

    ev = mapper.from_secs_event(machine, 3200002, {"CtrlJobID": "CJ-ghost"})
    assert ev.event_type == "lot_end"
    # No crash, ctrl_jobs map stays empty
    assert tracker.snapshot(machine.endpoint_id)["ctrl_jobs"] == {}


# ----- 3. Duplicate event delivery (equipment retry) -----

def test_duplicate_event_dedupes_via_outbox_key(tmp_path):
    """Equipment may retransmit S6F11 if it doesn't get S6F12 fast enough.
    Outbox keys events deterministically so duplicates collapse to one row."""
    from eap_middleware.outbox import SQLiteOutbox
    from eap_middleware.linkstuffs import LINKSTUFFS_TOPIC_TELEMETRY

    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _davinci(tmp_path, "01")
    tracker = JobTracker()
    mapper = CanonicalMapper(profile, tracker=tracker)

    payload = {
        "DATETIME": "20251128094700",
        "_v_raw": ["W001", "LOT_DUP", "Rcp"],
    }

    # Build the same event twice (equipment retransmit)
    ev1 = mapper.from_secs_event(machine, 3140002, payload)
    ev2 = mapper.from_secs_event(machine, 3140002, payload)
    assert ev1.event_key() == ev2.event_key(), (
        "deterministic event_key required for outbox dedup to work"
    )

    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    outbox.enqueue(
        LINKSTUFFS_TOPIC_TELEMETRY, ev1.linkstuffs_telemetry_payload(),
        key=f"telemetry:{ev1.event_key()}",
    )
    outbox.enqueue(
        LINKSTUFFS_TOPIC_TELEMETRY, ev2.linkstuffs_telemetry_payload(),
        key=f"telemetry:{ev2.event_key()}",
    )
    pending = outbox.pending(limit=10)
    assert len(pending) == 1, "duplicate event_key must produce one outbox row"


# ----- 4. Malformed / minimal S6F11 payloads -----

def test_empty_v_array_doesnt_crash(tmp_path):
    """Equipment fires an event with an empty V[] (some E87 carrier events
    have no DVs even when subscribed)."""
    tracker = JobTracker()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _davinci(tmp_path, "01")
    mapper = CanonicalMapper(profile, tracker=tracker)

    ev = mapper.from_secs_event(machine, 3210002, {"_v_raw": []})  # CarrierClamped
    assert ev.event_type == "clamped"  # resolved by alias
    assert ev.lot_id == ""  # no DV payload, no lot_id


def test_minimal_data_dict_doesnt_crash(tmp_path):
    """Just a CEID and nothing else - host parser delivered malformed data
    or the SECS message had unexpected structure."""
    tracker = JobTracker()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _davinci(tmp_path, "01")
    mapper = CanonicalMapper(profile, tracker=tracker)

    ev = mapper.from_secs_event(machine, 3140002, {})
    assert ev.event_type == "process_start"  # CEID still resolves
    assert ev.lot_id == ""


def test_v_array_with_none_values(tmp_path):
    """Equipment can deliver None for an absent DV. Mapper must tolerate it
    in any position."""
    tracker = JobTracker()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _davinci(tmp_path, "01")
    mapper = CanonicalMapper(profile, tracker=tracker)

    ev = mapper.from_secs_event(
        machine, 3140003,  # PM1/ProcessingFinished
        {"_v_raw": ["W001", "LOT", None, None, None, None, None]},
    )
    assert ev.event_type == "process_end"
    assert ev.wafer_id == "W001"
    assert ev.lot_id == "LOT"
    assert ev.recipe == ""  # was None in V[2]


def test_empty_ctrl_job_id_in_start_event(tmp_path):
    """ControlJob:Selected-Executing with empty CtrlJobID - we shouldn't map
    an empty string into ctrl_jobs (would mask a real later mapping)."""
    tracker = JobTracker()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _davinci(tmp_path, "01")
    mapper = CanonicalMapper(profile, tracker=tracker)

    mapper.from_secs_event(machine, 3050001, {"_v_raw": [1]})
    mapper.from_secs_event(machine, 3200017, {"CtrlJobID": ""})

    # Empty ID isn't recorded
    assert tracker.snapshot(machine.endpoint_id)["ctrl_jobs"] == {}


# ----- 5. Concurrent threading across two machines -----

def test_concurrent_events_from_two_machines_dont_corrupt_state(tmp_path):
    """Two threads simulating two DaVinci machines firing events
    simultaneously. After they finish, both machines' state should be
    correct - no torn writes, no cross-pollination."""
    tracker = JobTracker()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    mA = _davinci(tmp_path, "01")
    mB = _davinci(tmp_path, "02")
    mapper_A = CanonicalMapper(profile, tracker=tracker)
    mapper_B = CanonicalMapper(profile, tracker=tracker)
    errors: List[Exception] = []

    def hammer(mapper, machine, port):
        try:
            for i in range(50):
                # Activate, run a job, complete, repeat
                mapper.from_secs_event(machine, 3050001, {"_v_raw": [port]})
                mapper.from_secs_event(
                    machine, 3200017, {"CtrlJobID": f"CJ-{port}-{i}"},
                )
                mapper.from_secs_event(
                    machine, 3140002, {"_v_raw": [f"W{i}", "LOT", "R"]},
                )
                mapper.from_secs_event(
                    machine, 3200002, {"CtrlJobID": f"CJ-{port}-{i}"},
                )
        except Exception as e:
            errors.append(e)

    tA = threading.Thread(target=hammer, args=(mapper_A, mA, 1))
    tB = threading.Thread(target=hammer, args=(mapper_B, mB, 2))
    tA.start(); tB.start()
    tA.join(timeout=10); tB.join(timeout=10)
    assert not errors, f"Threading errors: {errors}"

    # After all CtrlJobs ended, ctrl_jobs maps should be empty
    assert tracker.snapshot(mA.endpoint_id)["ctrl_jobs"] == {}
    assert tracker.snapshot(mB.endpoint_id)["ctrl_jobs"] == {}
    # active_lp may be 1 or 2 depending on last activation
    assert tracker.snapshot(mA.endpoint_id)["active_lp"] == "1"
    assert tracker.snapshot(mB.endpoint_id)["active_lp"] == "2"


# ----- 6. CSV writer multi-machine isolation -----

def test_two_machines_each_produce_their_own_csv_file(tmp_path):
    """Per-lot CSV files must be machine-scoped. A close event on machine A
    must NOT trigger a write of machine B's buffered rows."""
    tracker = JobTracker()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    mA = _davinci(tmp_path, "01")
    mB = _davinci(tmp_path, "02")
    writer = PerLotCsvWriter()
    mapper_A = CanonicalMapper(profile, tracker=tracker)
    mapper_B = CanonicalMapper(profile, tracker=tracker)

    # Both machines: activate -> lot_start -> close
    for mapper, machine, lot in [
        (mapper_A, mA, "LOT_A"), (mapper_B, mB, "LOT_B"),
    ]:
        mapper.from_secs_event(machine, 3050001, {"_v_raw": [1]})
        for ceid in (3140002, 3160002):  # PM start, then LP1 depart (closes)
            ev = mapper.from_secs_event(
                machine, ceid,
                {"_v_raw": ["W", lot, "R"] if ceid == 3140002 else ["CID"]},
            )
            writer.append(machine, profile, ev)

    csv_a = list((tmp_path / "01" / "local").glob("*.csv"))
    csv_b = list((tmp_path / "02" / "local").glob("*.csv"))
    assert len(csv_a) == 1 and "_01_" in csv_a[0].name
    assert len(csv_b) == 1 and "_02_" in csv_b[0].name


def test_two_machines_simultaneous_carriers_dont_collide_on_filename(tmp_path):
    """Two DaVincis closing a lot in the exact same microsecond would have
    collided pre-v1-fix. Filename now includes display_name + LP suffix."""
    tracker = JobTracker()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    mA = _davinci(tmp_path, "01")
    mB = _davinci(tmp_path, "02")
    writer = PerLotCsvWriter()
    mapper_A = CanonicalMapper(profile, tracker=tracker)
    mapper_B = CanonicalMapper(profile, tracker=tracker)

    SAME_TS = "20251128094700"
    for mapper, machine in [(mapper_A, mA), (mapper_B, mB)]:
        mapper.from_secs_event(machine, 3050001, {"_v_raw": [1], "DATETIME": SAME_TS})
        for ceid in (3140002, 3160002):
            ev = mapper.from_secs_event(
                machine, ceid,
                {
                    "DATETIME": SAME_TS,
                    "_v_raw": ["W", "LOT", "R"] if ceid == 3140002 else ["CID"],
                },
            )
            writer.append(machine, profile, ev)

    # Both CSVs written, names differ by display_name
    files_a = list((tmp_path / "01" / "local").glob("*.csv"))
    files_b = list((tmp_path / "02" / "local").glob("*.csv"))
    assert len(files_a) == 1 and len(files_b) == 1
    assert files_a[0].name != files_b[0].name


# ----- 7. Network share write failure -----

def test_network_share_unreachable_does_not_break_local_csv(tmp_path):
    """If \\\\TD-DATASVR-F2C4\\... is unreachable, local CSV must still
    succeed and the mirror error is recorded but non-fatal."""
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    bogus_network = "/this/path/does/not/exist/and/cannot/be/created"
    machine = MachineConfig(
        endpoint_id="TOOL_NS",
        display_name="DAVINCI_NS",
        machine_profile="davinci_200_mc4_hc1",
        host="127.0.0.1", port=5000,
        local_csv_path=str(tmp_path / "local"),
        network_csv_path=bogus_network,  # unreachable
        admin_config_path=str(tmp_path / "admin"),
    )
    writer = PerLotCsvWriter()
    mapper = CanonicalMapper(profile, tracker=JobTracker())

    # Activate LP1, then a process_end + LP1 depart to trigger CSV write
    mapper.from_secs_event(machine, 3050001, {"_v_raw": [1]})
    ev1 = mapper.from_secs_event(
        machine, 3140002, {"_v_raw": ["W", "LOT_NS", "R"]},
    )
    writer.append(machine, profile, ev1)
    ev2 = mapper.from_secs_event(machine, 3160002, {"_v_raw": ["CID"]})
    written = writer.append(machine, profile, ev2)

    # Local CSV was written despite network share failure
    local_files = list((tmp_path / "local").glob("*.csv"))
    assert len(local_files) == 1
    assert local_files[0] in written
    # Mirror error captured
    # The copy no longer happens on this thread (it would stall the S6F11
    # acknowledgement past T3 when the share is sick - which is precisely
    # the condition this test sets up). CsvMirrorWorker owns it, so the
    # failure is recorded when its pass runs.
    assert writer.retry_mirrors() == 0, "an unreachable share cannot succeed"
    assert writer.mirror_errors, "expected a recorded mirror error"


# ----- 8. Recipe name with special chars and very long values -----

def test_recipe_name_with_special_chars_does_not_break_csv(tmp_path):
    """Recipe names from real tools sometimes contain commas, quotes, newlines.
    csv.writer must escape them; load + parse round-trip safely."""
    import csv as _csv
    tracker = JobTracker()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _davinci(tmp_path, "01")
    mapper = CanonicalMapper(profile, tracker=tracker)
    writer = PerLotCsvWriter()

    nasty_recipe = 'Recipe,"with",comma\nand_newline'
    mapper.from_secs_event(machine, 3050001, {"_v_raw": [1]})
    ev1 = mapper.from_secs_event(
        machine, 3140002, {"_v_raw": ["W", "LOT", nasty_recipe]},
    )
    writer.append(machine, profile, ev1)
    ev2 = mapper.from_secs_event(machine, 3160002, {"_v_raw": ["CID"]})
    writer.append(machine, profile, ev2)

    csv_file = list((tmp_path / "01" / "local").glob("*.csv"))[0]
    with csv_file.open(newline="", encoding="utf-8") as h:
        rows = list(_csv.reader(h))
    # Header + 2 events
    assert len(rows) >= 2
    # The nasty recipe survives the round trip
    assert any(nasty_recipe in r[7] for r in rows[1:])


def test_very_long_test_results_payload_serializes(tmp_path):
    """A wafer with 5000 dies produces a large TestResults list. Telemetry
    payload serializer must handle it without truncation."""
    import json
    tracker = JobTracker()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _davinci(tmp_path, "01")
    mapper = CanonicalMapper(profile, tracker=tracker)

    big_results = [
        {"die": f"{x},{y}", "v": x * y * 0.01, "p": True}
        for x in range(50) for y in range(100)
    ]  # 5000 entries
    mapper.from_secs_event(machine, 3050001, {"_v_raw": [1]})
    ev = mapper.from_secs_event(
        machine, 3140003,
        {"_v_raw": ["W", "L", "R", "rf", "rp", "ip", big_results]},
    )
    payload = ev.linkstuffs_telemetry_payload()
    values = payload[machine.display_name][0]["values"]
    assert "raw_TestResults" in values
    parsed = json.loads(values["raw_TestResults"])
    assert len(parsed) == 5000


# ----- 9. Alarm rate limit isolation across machines -----

def test_alarm_storm_on_one_machine_doesnt_throttle_another():
    """If DaVinci-01 goes into alarm storm mode, DaVinci-02's alarms must
    still pass through unimpeded."""
    rl = AlarmRateLimiter(max_per_window=3, window_sec=1.0)
    t = 1000.0
    # Storm on machine 01
    for _ in range(20):
        rl.admit("TOOL_01", now=t)
    # Machine 02 still has full capacity
    assert rl.admit("TOOL_02", now=t) is True
    assert rl.admit("TOOL_02", now=t) is True
    assert rl.admit("TOOL_02", now=t) is True
    # Drops only on TOOL_01
    drops = rl.drain_drops()
    assert "TOOL_01" in drops and "TOOL_02" not in drops


# ----- 10. Same CEID same millisecond from two machines -----

def test_same_ceid_same_ms_from_two_machines_produces_distinct_events(tmp_path):
    """Two DaVincis fire CEID 3140002 (PM1/ProcessingStarted) in the exact
    same millisecond. Event keys must differ so neither dedupes the other."""
    tracker = JobTracker()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    mA = _davinci(tmp_path, "01")
    mB = _davinci(tmp_path, "02")
    mapper_A = CanonicalMapper(profile, tracker=tracker)
    mapper_B = CanonicalMapper(profile, tracker=tracker)
    SAME_TS = "20251128094700"
    SAME_PAYLOAD = {"DATETIME": SAME_TS, "_v_raw": ["W", "L", "R"]}

    mapper_A.from_secs_event(mA, 3050001, {"_v_raw": [1]})
    mapper_B.from_secs_event(mB, 3050001, {"_v_raw": [1]})
    evA = mapper_A.from_secs_event(mA, 3140002, SAME_PAYLOAD)
    evB = mapper_B.from_secs_event(mB, 3140002, SAME_PAYLOAD)

    assert evA.event_key() != evB.event_key(), (
        "event_key must include endpoint_id so two machines with identical "
        "payloads don't dedupe each other in the outbox"
    )
