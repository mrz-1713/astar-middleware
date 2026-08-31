"""Regression tests for the 2026-08-17 parallel-operation and data-loss audit.

Covers the defects found by the four audit tracks:

- PerLotCsvWriter is a shared sink mutated by every machine's dispatcher
  thread plus the supervisor's journal replay; it must not lose or duplicate
  rows under concurrency, and its journal refcounts must stay consistent.
- The live dispatch path and the supervisor's replay pass can both discover
  the same journal entry; exactly one of them may apply the entry's sinks.
- A full outbox (OutboxFullError) must be backpressure, never permanent
  telemetry loss: entries stay pending and replayable instead of being
  parked after MAX_DISPATCH_ATTEMPTS.
- Pre-lot TTL pruning must release its journal references so entries reach a
  terminal state and retention can purge them.
- Unknown-CEID warnings are per machine, not per profile.
- The provision worker must not issue SECS round-trips against a host that
  superseded it.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List


from eap_middleware.config import MiddlewarePaths, ServiceConfig
from eap_middleware.csv_store import PerLotCsvWriter
from eap_middleware.journal import IngressJournal, KIND_EVENT, PENDING
from eap_middleware.legacy_api import LegacyApiConfig
from eap_middleware.linkstuffs import LinkstuffsConfig
from eap_middleware.linkstuffs_http import LinkstuffsHttpConfig
from eap_middleware.mapper import CanonicalMapper, _UNKNOWN_CEID_WARNED
from eap_middleware.models import (
    CanonicalEvent,
    MachineConfig,
    MachineStorageConfig,
    utc_now,
)
from eap_middleware.outbox import OutboxFullError, SQLiteOutbox
from eap_middleware.profiles import ProfileRegistry, profile_with_subscription_file
from eap_middleware.secs_runtime import SecsMachineSession
from eap_middleware.service import EapMiddlewareService


def _machine(tmp_path: Path, endpoint: str = "TOOL_A") -> MachineConfig:
    return MachineConfig(
        endpoint_id=endpoint,
        display_name=f"EAP_{endpoint}",
        machine_profile="davinci_200_mc4_hc1",
        host="127.0.0.1",
        port=5000,
        storage=MachineStorageConfig(
            local_csv_path=str(tmp_path / "csv" / endpoint),
            log_dir=str(tmp_path / "logs" / endpoint),
            simulator_log_dir=str(tmp_path / "logs" / endpoint / "sim"),
            admin_config_path=str(tmp_path / "admin" / endpoint),
        ),
    )


def _service(tmp_path: Path) -> EapMiddlewareService:
    machine = _machine(tmp_path)
    cfg = ServiceConfig(
        machines=[machine],
        linkstuffs=LinkstuffsConfig(enabled=False),
        linkstuffs_http=LinkstuffsHttpConfig(enabled=False),
        legacy_api=LegacyApiConfig(enabled=False),
        paths=MiddlewarePaths(
            install_dir=str(tmp_path / "install"),
            outbox_db=str(tmp_path / "o.sqlite3"),
            legacy_api_outbox_db=str(tmp_path / "l.sqlite3"),
            http_outbox_db=str(tmp_path / "h.sqlite3"),
            ingress_journal_db=str(tmp_path / "ingress.sqlite3"),
        ),
    )
    return EapMiddlewareService(cfg)


def _event(
    machine: MachineConfig,
    event_type: str,
    raw: str,
    ceid: int,
    load_port: str = "1",
    lot_id: str = "",
) -> CanonicalEvent:
    return CanonicalEvent(
        timestamp=utc_now(),
        endpoint_id=machine.endpoint_id,
        display_name=machine.display_name,
        machine_profile=machine.machine_profile,
        vendor="MueTec",
        model="DaVinci 200 MC4 HC1",
        event_type=event_type,
        raw_event_name=raw,
        ceid=ceid,
        load_port=load_port,
        chamber="NA",
        lot_id=lot_id,
        wafer_id="",
        recipe="",
        secs_raw_event=raw,
        raw_payload={},
    )


# ---------------------------------------------------------------------------
# PerLotCsvWriter thread safety
# ---------------------------------------------------------------------------


def test_csv_writer_parallel_append_loses_and_duplicates_nothing(tmp_path):
    """Eight dispatcher threads (four machines x two load ports) appending
    concurrently must produce exactly one buffered row per append - no lost
    rows, no duplicates - and flush must account for every journal ref."""
    journal = IngressJournal(tmp_path / "ingress.sqlite3")
    writer = PerLotCsvWriter(journal=journal)
    machines = [_machine(tmp_path, f"TOOL_{i}") for i in range(4)]
    rows_per_thread = 200
    barrier = threading.Barrier(8)
    errors: List[BaseException] = []
    lock = threading.Lock()
    expected: Dict[tuple, int] = {}

    def worker(machine: MachineConfig, port: str) -> None:
        try:
            barrier.wait()
            for i in range(rows_per_thread):
                seq = journal.append(
                    endpoint_id=machine.endpoint_id,
                    kind=KIND_EVENT,
                    stream=6,
                    function=11,
                    ceid=1000 + i,
                    payload={"v": i},
                )[0].seq
                event = _event(
                    machine, "wafer_start", "Wfr", 1000 + i,
                    load_port=port, lot_id="LOT_1",
                )
                writer.append(machine, machine_profile_for(machine), event, seq=seq)
                with lock:
                    key = (machine.endpoint_id, port)
                    expected[key] = expected.get(key, 0) + 1
        except BaseException as exc:  # pragma: no cover - failure reporting
            errors.append(exc)

    threads = []
    for machine in machines:
        for port in ("1", "2"):
            threads.append(
                threading.Thread(target=worker, args=(machine, port), daemon=True)
            )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors

    for (endpoint, port), count in expected.items():
        buffer = writer._buffers.get((endpoint, port))
        assert buffer is not None, f"no buffer for {endpoint}/{port}"
        assert len(buffer.rows) == count, (
            f"{endpoint}/{port}: expected {count} rows, got {len(buffer.rows)}"
        )
    # Every journal entry is still referenced by a buffered row.
    for seq, refs in writer._seq_refs.items():
        assert refs > 0
    written = writer.flush_all(reason="test")
    assert len(written) == len(expected), written
    assert not writer._seq_refs, "flush must resolve every journal reference"
    assert journal.stats()["csv_pending"] == 0


def machine_profile_for(machine: MachineConfig):
    return ProfileRegistry().get(machine.machine_profile)


# ---------------------------------------------------------------------------
# Live dispatch vs replay: exactly-once sink application
# ---------------------------------------------------------------------------


def test_replay_and_live_dispatch_apply_sinks_once(tmp_path, monkeypatch):
    """While the live callback path is mid-dispatch, a replay pass must not
    apply the same entry's sinks again (duplicate CSV row / double publish)."""
    svc = _service(tmp_path)
    machine = svc.config.machines[0]
    svc._machines_by_endpoint[machine.endpoint_id] = machine
    # Make the mapper slow so the live dispatch overlaps the replay pass.
    slow = {"sleeping": False}

    def slow_map(self, machine, ceid, data):
        if slow["sleeping"]:
            time.sleep(0.3)
        return [CanonicalEvent(
            timestamp=utc_now(),
            endpoint_id=machine.endpoint_id,
            display_name=machine.display_name,
            machine_profile=machine.machine_profile,
            vendor="MueTec", model="DaVinci 200 MC4 HC1",
            event_type="wafer_start", raw_event_name="Wfr",
            ceid=ceid, load_port="1", chamber="NA",
            lot_id="LOT_1", wafer_id="", recipe="",
            secs_raw_event="Wfr", raw_payload=dict(data),
        )]

    monkeypatch.setattr(CanonicalMapper, "from_secs_events", slow_map)

    def live_dispatch() -> None:
        svc._on_secs_event(
            machine,
            3140002,
            {"_stream": 6, "_function": 11, "WaferID": "W1", "LotID": "LOT_1"},
        )

    # The mapper sleeps on every call, so the live dispatch holds the
    # dispatch lock while the replay pass below runs.
    slow["sleeping"] = True
    thread = threading.Thread(target=live_dispatch, daemon=True)
    thread.start()
    time.sleep(0.15)  # live is mid-dispatch now (mapping, lock held)
    svc._replay_journal()  # blocks on the lock, then sees dispatch done
    thread.join(timeout=10)
    assert not thread.is_alive()

    # The slow mapper labels the event with load_port "1".
    buffer = svc.csv_writer._buffers.get((machine.endpoint_id, "1"))
    assert buffer is not None, "live dispatch must have appended the row"
    assert len(buffer.rows) == 1, (
        "replay must not duplicate a row the live path already appended; "
        f"got {len(buffer.rows)}"
    )
    pending = [e for e in svc.journal.pending_dispatch() if e.endpoint_id == machine.endpoint_id]
    assert not pending, "dispatch must reach a terminal state"


def test_live_dispatch_does_not_redo_a_sink_replay_already_applied(
    tmp_path, monkeypatch
):
    """The other direction of the same race, with the sinks disagreeing.

    A replay pass can pick up a brand-new entry between `journal.append()` and
    the live callback taking the dispatch lock. If that pass writes the CSV row
    but hits a full outbox, `mark_dispatch_failed` only counts the attempt - so
    dispatch is still PENDING while the row already sits in the open lot
    buffer. Gating the live path on dispatch_status alone let it re-apply the
    CSV sink and silently duplicate a wafer row in the lot file.
    """
    svc = _service(tmp_path)
    machine = svc.config.machines[0]
    svc._machines_by_endpoint[machine.endpoint_id] = machine

    def one_event(self, m, ceid, data):
        return [CanonicalEvent(
            timestamp=utc_now(), endpoint_id=m.endpoint_id,
            display_name=m.display_name, machine_profile=m.machine_profile,
            vendor="MueTec", model="DaVinci 200 MC4 HC1",
            event_type="wafer_start", raw_event_name="Wfr", ceid=ceid,
            load_port="1", chamber="NA", lot_id="LOT_1", wafer_id="W1",
            recipe="", secs_raw_event="Wfr", raw_payload=dict(data),
        )]

    monkeypatch.setattr(CanonicalMapper, "from_secs_events", one_event)

    def full(*_args, **_kwargs):
        raise OutboxFullError("outbox full")

    monkeypatch.setattr(svc.publisher, "queue_event", full)

    # Run the replay pass in the window the race opens: after the journal row
    # exists, before the live callback reaches the dispatch lock.
    real_append = svc.journal.append
    fired: List[int] = []

    def append_then_replay(*args, **kwargs):
        result = real_append(*args, **kwargs)
        if not fired:
            fired.append(1)
            svc._replay_journal()
        return result

    monkeypatch.setattr(svc.journal, "append", append_then_replay)

    svc._on_secs_event(
        machine, 3140002,
        {"_stream": 6, "_function": 11, "WaferID": "W1", "LotID": "LOT_1"},
    )

    buffer = svc.csv_writer._buffers.get((machine.endpoint_id, "1"))
    assert buffer is not None, "the replay pass must have appended the row"
    assert len(buffer.rows) == 1, (
        "the live path must not re-append a CSV row the replay pass already "
        f"wrote; got {len(buffer.rows)} copies of one collection event"
    )
    assert svc.csv_writer._seq_refs.get(1) == 1, (
        "one journal entry must hold exactly one buffered-row reference"
    )


def test_replay_delivers_entry_the_live_path_never_reached(tmp_path):
    """The symmetric case: replay must deliver an entry that the live path
    journaled but never dispatched (crash simulation)."""
    svc = _service(tmp_path)
    machine = svc.config.machines[0]
    svc._machines_by_endpoint[machine.endpoint_id] = machine
    entry, is_new = svc.journal.append(
        endpoint_id=machine.endpoint_id,
        kind=KIND_EVENT,
        stream=6, function=11, ceid=3140002,
        payload={"_stream": 6, "_function": 11, "WaferID": "W1", "LotID": "LOT_1"},
    )
    assert is_new
    replayed = svc._replay_journal()
    assert replayed == 1
    buffer = svc.csv_writer._buffers.get((machine.endpoint_id, "NA"))
    assert buffer is not None and len(buffer.rows) == 1
    assert svc.journal.entry(entry.seq).dispatch_status != PENDING


# ---------------------------------------------------------------------------
# Outbox-full backpressure must never park entries
# ---------------------------------------------------------------------------


def test_outbox_full_never_parks_journal_entry(tmp_path):
    """A full outbox is backpressure, not a defect: the entry must stay
    pending and replayable no matter how many replay passes fail."""
    svc = _service(tmp_path)
    machine = svc.config.machines[0]
    small_outbox = SQLiteOutbox(
        tmp_path / "small.sqlite3", max_pending_per_partition=1
    )
    small_outbox.enqueue("t", {"blocker": True}, "blocker-key", partition_key="p")
    svc.http_outbox = small_outbox  # swap in the capacity-1 outbox
    svc._http_publishers[machine.endpoint_id] = SimpleNamespace(
        queue_event=lambda event: small_outbox.enqueue(
            "v1/devices/x/telemetry", {}, "k", partition_key="p"
        )
    )

    svc._machines_by_endpoint[machine.endpoint_id] = machine
    entry, is_new = svc.journal.append(
        endpoint_id=machine.endpoint_id,
        kind=KIND_EVENT, stream=6, function=11, ceid=3140002,
        payload={"_stream": 6, "_function": 11, "WaferID": "W1", "LotID": "LOT_1"},
    )
    assert is_new
    # Replay passes, not direct _dispatch_entry calls: replay consults the
    # writer's holds() guard so a buffered row is never appended twice.
    for _ in range(15):
        svc._replay_journal()
    fresh = svc.journal.entry(entry.seq)
    assert fresh.dispatch_status == PENDING, (
        "outbox-full entries must never be parked; "
        f"got dispatch_status={fresh.dispatch_status}"
    )
    # The CSV sink still ran on the first attempt: the row is buffered
    # (under the NA port bucket - the payload has no PortID).
    buffer = svc.csv_writer._buffers.get((machine.endpoint_id, "NA"))
    assert buffer is not None and len(buffer.rows) == 1
    assert svc.journal.entry(entry.seq).csv_status == PENDING


# ---------------------------------------------------------------------------
# Pre-lot TTL prune journal accounting
# ---------------------------------------------------------------------------


def test_pre_lot_ttl_prune_releases_journal_references(tmp_path):
    """TTL pruning must resolve the dropped rows' journal entries (they were
    never written anywhere and are not buffered), so the journal does not
    grow without bound and retention can purge them."""
    journal = IngressJournal(tmp_path / "ingress.sqlite3")
    writer = PerLotCsvWriter(pre_lot_ttl_sec=1.0, journal=journal)
    machine = _machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)

    seqs = []
    for i in range(3):
        seqs.append(
            journal.append(
                endpoint_id=machine.endpoint_id, kind=KIND_EVENT,
                stream=6, function=11, ceid=1000 + i, payload={"v": i},
            )[0].seq
        )
        writer.append(
            machine, profile,
            _event(machine, "loaded", "Loaded", 1000 + i, load_port="2"),
            seq=seqs[-1],
        )
    key = (machine.endpoint_id, "2")
    assert len(writer._pending_pre_lot[key]) == 3

    # Backdate all three entries past the TTL and force a prune.
    for idx in range(3):
        row = writer._pending_pre_lot[key][idx]
        writer._pending_pre_lot[key][idx] = (
            datetime.now(timezone.utc) - timedelta(seconds=10), row[1], row[2]
        )
    writer.append(
        machine, profile,
        _event(machine, "clamped", "Clamped", 2000, load_port="2"),
        seq=journal.append(
            endpoint_id=machine.endpoint_id, kind=KIND_EVENT,
            stream=6, function=11, ceid=2000, payload={"v": 9},
        )[0].seq,
    )
    # The fresh entry is still pending (its lot never started); the three
    # stale ones were pruned and released.
    assert len(writer._pending_pre_lot[key]) == 1
    for seq in seqs:
        assert not writer.holds(seq), f"TTL-pruned seq {seq} must be released"
        assert journal.entry(seq).csv_status == "dropped"


# ---------------------------------------------------------------------------
# Unknown-CEID warnings are per machine
# ---------------------------------------------------------------------------


def test_unknown_ceid_warning_is_per_machine(tmp_path, caplog):
    machine_a = _machine(tmp_path, "TOOL_A")
    machine_b = _machine(tmp_path, "TOOL_B")
    profile = ProfileRegistry().get(machine_a.machine_profile)
    mapper = CanonicalMapper(profile)
    _UNKNOWN_CEID_WARNED.clear()
    try:
        with caplog.at_level(logging.WARNING, logger="eap_middleware.mapper"):
            mapper.from_secs_event(machine_a, 999999, {})
            mapper.from_secs_event(machine_b, 999999, {})
            mapper.from_secs_event(machine_a, 999999, {})
        warnings = [r.message for r in caplog.records if "Unknown CEID" in r.getMessage()]
        assert len(warnings) == 2, (
            "machine B must still warn even though machine A already saw the "
            f"same CEID; got {len(warnings)} warning(s)"
        )
    finally:
        _UNKNOWN_CEID_WARNED.clear()


# ---------------------------------------------------------------------------
# Provision worker must not touch a superseded host
# ---------------------------------------------------------------------------


def test_subscribe_retry_never_uses_superseded_host(tmp_path):
    machine = _machine(tmp_path)
    session = SecsMachineSession(
        machine=machine,
        event_callback=lambda *_: None,
        alarm_callback=lambda *_: None,
        connect_callback=lambda *_: None,
        disconnect_callback=lambda *_: None,
        subscription_path="whatever.json",
    )
    calls: List[str] = []
    old_host = SimpleNamespace(subscribe_to_events=lambda *a, **k: calls.append("old"))
    new_host = SimpleNamespace(subscribe_to_events=lambda *a, **k: calls.append("new"))
    session.host = new_host
    session._stopped = True  # superseded: epoch check must fail first
    assert session._subscribe_with_retry(epoch=1, host=old_host) is False
    assert calls == [], "a superseded worker must not issue round-trips"


# ---------------------------------------------------------------------------
# SPTS formula-derived alarm CEIDs can be aliased by name
# ---------------------------------------------------------------------------


def test_spts_alarm_ceids_can_be_aliased_by_name(tmp_path):
    """SPTS alarm CEIDs are computed per tool layout (ALID = station*1e7 +
    type*1e5 + offset; ON = ALID + 10000 + offset; OFF = ALID + 1000010000 +
    offset), so no static profile table can name them. A commissioning
    engineer adds the layout's CEIDs to the machine's subscription file with
    the canonical alarm names and the overlay must route them through the
    alarm pipeline."""
    import json

    sub = {
        "events": [
            {"ceid": 12410002, "name": "AlarmNDetected", "rptids": [], "enabled": True},
            {"ceid": 1012410002, "name": "AlarmNCleared", "rptids": [], "enabled": True},
        ],
        "reports": [],
        "dvid_names": {},
    }
    path = tmp_path / "subscription.json"
    path.write_text(json.dumps(sub), encoding="utf-8")
    profile = ProfileRegistry().get("spts_fxp_omega")
    overlaid = profile_with_subscription_file(profile, str(path))
    assert overlaid.resolve_event(ceid=12410002).event_type == "alarm"
    assert overlaid.resolve_event(ceid=1012410002).csv_tool_event == "AlarmCleared"


# ---------------------------------------------------------------------------
# DaVinci active subscription covers every aliased CEID
# ---------------------------------------------------------------------------


def test_davinci_active_subscription_covers_every_alias():
    import json

    root = Path(__file__).resolve().parent.parent
    active = json.loads(
        (root / "output" / "davinci200_mc4_hc1" / "EventSubscription.json")
        .read_text(encoding="utf-8")
    )
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    subscribed = {int(e["ceid"]) for e in active["events"]}
    missing = sorted(set(profile.ceid_aliases) - subscribed)
    assert not missing, (
        "every CEID the profile classifies must be subscribed so the alias "
        f"can ever fire; missing: {missing}"
    )
