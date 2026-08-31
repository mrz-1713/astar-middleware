"""Crash-boundary and ordered-fanout tests for Option A."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from eap_middleware.csv_store import PerLotCsvWriter
from eap_middleware.journal import IngressJournal
from eap_middleware.linkstuffs_http import LinkstuffsHttpPublisher
from eap_middleware.models import (
    CanonicalEvent,
    LinkstuffsHttpConfig,
    MachineConfig,
)
from eap_middleware.outbox import OutboxFullError, SQLiteOutbox
from eap_middleware.profiles import ProfileRegistry
from eap_middleware.service import EapMiddlewareService


def _machine(tmp_path, endpoint_id: str = "TOOL/A") -> MachineConfig:
    return MachineConfig(
        endpoint_id=endpoint_id,
        display_name=endpoint_id.replace("/", "_"),
        machine_profile="davinci_200_mc4_hc1",
        host="127.0.0.1",
        port=5000,
        local_csv_path=str(tmp_path / endpoint_id.replace("/", "_")),
    )


def _event(
    machine: MachineConfig, event_type: str, ingress: str, ceid: int = 3140002
) -> CanonicalEvent:
    return CanonicalEvent(
        timestamp=datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc),
        endpoint_id=machine.endpoint_id,
        display_name=machine.display_name,
        machine_profile=machine.machine_profile,
        vendor="MueTec",
        model="DaVinci 200 MC4 HC1",
        event_type=event_type,
        ceid=ceid,
        load_port="1",
        chamber="PM1",
        lot_id="LOT_DURABLE",
        wafer_id="W01",
        recipe="RCP_A",
        raw_payload={"_ingress_key": ingress},
    )


def test_journal_and_fanout_outboxes_use_full_synchronous_mode(tmp_path):
    journal = IngressJournal(tmp_path / "journal.sqlite3")
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    with journal._connect() as conn:  # durability is a connection property
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    with outbox._connect() as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_equipment_retransmission_reuses_one_ingress_identity(tmp_path):
    journal = IngressJournal(tmp_path / "journal.sqlite3")
    first, is_new = journal.append(
        endpoint_id="TOOL_A", kind="event", stream=6, function=11,
        ceid=55, system_bytes=1234,
        payload={"received_at": "first", "timestamp": "first", "value": 9},
    )
    repeated, repeated_is_new = journal.append(
        endpoint_id="TOOL_A", kind="event", stream=6, function=11,
        ceid=55, system_bytes=1234,
        payload={"received_at": "retry", "timestamp": "retry", "value": 9},
    )
    different, different_is_new = journal.append(
        endpoint_id="TOOL_A", kind="event", stream=6, function=11,
        ceid=55, system_bytes=1234,
        payload={"received_at": "retry", "timestamp": "retry", "value": 10},
    )
    assert is_new and not repeated_is_new and different_is_new
    assert repeated.seq == first.seq
    assert different.seq != first.seq


def test_without_transport_identity_identical_bodies_are_not_swallowed(tmp_path):
    journal = IngressJournal(tmp_path / "journal.sqlite3")
    first, _ = journal.append(
        endpoint_id="TOOL_A", kind="event", stream=6, function=11,
        ceid=55, payload={"received_at": "one", "value": 9},
    )
    second, is_new = journal.append(
        endpoint_id="TOOL_A", kind="event", stream=6, function=11,
        ceid=55, payload={"received_at": "two", "value": 9},
    )
    assert is_new and second.seq != first.seq


def test_open_lot_is_rebuilt_from_journal_after_process_crash(tmp_path):
    journal = IngressJournal(tmp_path / "journal.sqlite3")
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _machine(tmp_path)
    start_entry, _ = journal.append(
        endpoint_id=machine.endpoint_id, kind="event", stream=6, function=11,
        ceid=1, system_bytes=1, payload={"phase": "start"},
    )
    end_entry, _ = journal.append(
        endpoint_id=machine.endpoint_id, kind="event", stream=6, function=11,
        ceid=2, system_bytes=2, payload={"phase": "end"},
    )

    crashed_writer = PerLotCsvWriter(journal=journal)
    crashed_writer.append(
        machine, profile, _event(machine, "lot_start", start_entry.ingress_key),
        seq=start_entry.seq,
    )
    assert crashed_writer.holds(start_entry.seq)
    # A new process has no in-memory buffer, but the row remains pending on disk.
    recovered = PerLotCsvWriter(journal=IngressJournal(journal.db_path))
    assert [entry.seq for entry in journal.pending_csv()] == [
        start_entry.seq, end_entry.seq
    ]
    recovered.append(
        machine, profile, _event(machine, "lot_start", start_entry.ingress_key),
        seq=start_entry.seq,
    )
    written = recovered.append(
        machine, profile,
        _event(machine, "unloaded", end_entry.ingress_key, ceid=3160002),
        seq=end_entry.seq,
    )
    assert len(written) == 1 and written[0].is_file()
    assert journal.entry(start_entry.seq).csv_status == "done"  # type: ignore[union-attr]
    assert journal.entry(end_entry.seq).csv_status == "done"  # type: ignore[union-attr]


def test_outbox_preserves_order_per_machine_without_blocking_other_machines(tmp_path):
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    outbox.enqueue("t", {"n": 1}, "a1", partition_key="A")
    outbox.enqueue("t", {"n": 2}, "a2", partition_key="A")
    outbox.enqueue("t", {"n": 1}, "b1", partition_key="B")
    heads = outbox.pending_heads()
    assert [item.key for item in heads] == ["a1", "b1"]
    outbox.mark_failed(heads[0].id, "offline")
    # A2 cannot overtake A1; B remains independent.
    assert [item.key for item in outbox.pending_heads()] == ["b1"]


def test_outbox_backpressure_keeps_ingress_replayable_instead_of_dropping(tmp_path):
    outbox = SQLiteOutbox(
        tmp_path / "outbox.sqlite3", max_pending_per_partition=1
    )
    outbox.enqueue("t", {"n": 1}, "a1", partition_key="A")
    outbox.enqueue("t", {"n": 1}, "a1", partition_key="A")  # replay dedupe
    with pytest.raises(OutboxFullError):
        outbox.enqueue("t", {"n": 2}, "a2", partition_key="A")


def test_missing_http_token_is_queued_for_operator_repair(tmp_path):
    outbox = SQLiteOutbox(tmp_path / "http.sqlite3")
    publisher = LinkstuffsHttpPublisher(
        LinkstuffsHttpConfig(enabled=True, base_url="https://example.invalid"),
        outbox,
    )
    machine = _machine(tmp_path)
    publisher.queue_event(_event(machine, "process_start", "ingress-1"))
    assert outbox.stats()["pending"] == 1


def test_colliding_legacy_endpoint_names_get_distinct_http_queue_files(tmp_path):
    service = object.__new__(EapMiddlewareService)
    service.config = SimpleNamespace(
        paths=SimpleNamespace(http_outbox_db=str(tmp_path / "http.sqlite3"))
    )
    first = service._machine_http_outbox_path("A/B")
    second = service._machine_http_outbox_path("A?B")
    assert first != second
    assert first.name.startswith("http.A_B.")
    assert second.name.startswith("http.A_B.")


def test_dead_rows_have_an_explicit_requeue_operation(tmp_path):
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    outbox.enqueue("t", {"n": 1}, "a1", partition_key="A")
    item = outbox.pending()[0]
    outbox.mark_dead(item.id, "bad token")
    assert outbox.stats()["dead"] == 1
    assert outbox.requeue_dead() == 1
    assert [entry.key for entry in outbox.pending()] == ["a1"]
