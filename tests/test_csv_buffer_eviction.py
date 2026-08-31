"""Bounded lot buffers when the local CSV sink stops accepting writes.

A lot file that cannot be written keeps its buffer (see `_write_and_remove`),
so without a ceiling the next lot's rows join it and every close re-serialises
all of them - unbounded memory and O(n^2) work for as long as the sink is down.
These tests pin the ceiling *and* the guarantee that paying it costs no rows:
an evicted row stays csv_status='pending' in the journal and is rewritten once
the sink recovers.
"""

from datetime import datetime, timedelta, timezone

import pytest

from eap_middleware.csv_store import PerLotCsvWriter
from eap_middleware.journal import IngressJournal
from eap_middleware.models import CanonicalEvent, MachineConfig
from eap_middleware.profiles import ProfileRegistry

CLOSES_LOT_CEID = 3160002
PLAIN_CEID = 3140002
CAP = 5
BASE_TS = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def _machine(tmp_path, endpoint_id: str = "TOOL/A") -> MachineConfig:
    return MachineConfig(
        endpoint_id=endpoint_id,
        display_name=endpoint_id.replace("/", "_"),
        machine_profile="davinci_200_mc4_hc1",
        host="127.0.0.1",
        port=5000,
        local_csv_path=str(tmp_path / endpoint_id.replace("/", "_")),
    )


def _event(machine, event_type, ingress, ceid=PLAIN_CEID, tick=0):
    # Distinct timestamps: _filename() is derived from the buffer's first row,
    # so identical ones would make two lot files collide via os.replace().
    return CanonicalEvent(
        timestamp=BASE_TS + timedelta(seconds=tick),
        endpoint_id=machine.endpoint_id,
        display_name=machine.display_name,
        machine_profile=machine.machine_profile,
        vendor="MueTec",
        model="DaVinci 200 MC4 HC1",
        event_type=event_type,
        ceid=ceid,
        load_port="1",
        chamber="PM1",
        lot_id="LOT_EVICT",
        wafer_id=f"W{tick:03d}",
        recipe="RCP_A",
        raw_payload={"_ingress_key": ingress},
    )


class _Feeder:
    """Journals an event and hands it to the writer, as the service does."""

    def __init__(self, writer, journal, machine, profile):
        self.writer, self.journal = writer, journal
        self.machine, self.profile = machine, profile
        self.tick = 0
        self.seqs = []

    def send(self, event_type, ceid=PLAIN_CEID):
        self.tick += 1
        entry, _ = self.journal.append(
            endpoint_id=self.machine.endpoint_id, kind="event", stream=6,
            function=11, ceid=ceid, system_bytes=self.tick, payload={},
        )
        self.seqs.append(entry.seq)
        return self.writer.append(
            self.machine, self.profile,
            _event(self.machine, event_type, entry.ingress_key,
                   ceid=ceid, tick=self.tick),
            seq=entry.seq,
        )


def _rig(tmp_path, broken, monkeypatch=None):
    journal = IngressJournal(tmp_path / "journal.sqlite3")
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _machine(tmp_path)
    writer = PerLotCsvWriter(journal=journal, max_lot_rows=CAP)
    if broken:
        def _boom(path, rows):
            raise OSError(28, "No space left on device")
        monkeypatch.setattr(writer, "_write_atomic", _boom)
    return journal, profile, machine, writer, _Feeder(writer, journal, machine, profile)


def _storm(feeder, extra_rows):
    """Fill a lot, fail its close, then keep feeding while the sink stays down."""
    for _ in range(3):
        feeder.send("wafer_start")
    with pytest.raises(OSError):
        feeder.send("unloaded", ceid=CLOSES_LOT_CEID)
    for _ in range(extra_rows):
        feeder.send("wafer_start")


def test_healthy_long_lot_is_never_evicted(tmp_path):
    """Eviction needs a recorded write failure, so a long healthy lot is safe."""
    journal, profile, machine, writer, feeder = _rig(tmp_path, broken=False)

    for _ in range(CAP * 8):
        feeder.send("wafer_start")

    buffer = writer._buffers[(machine.endpoint_id, "1")]
    assert len(buffer.rows) == CAP * 8
    assert all(writer.holds(seq) for seq in feeder.seqs)


def test_broken_sink_keeps_memory_bounded_and_every_row_replayable(
    tmp_path, monkeypatch
):
    journal, profile, machine, writer, feeder = _rig(
        tmp_path, broken=True, monkeypatch=monkeypatch
    )
    key = (machine.endpoint_id, "1")

    _storm(feeder, extra_rows=0)
    assert writer._write_failures[key] == 1
    assert len(writer._buffers[key].rows) == 4  # kept, exactly as before

    # Rows keep arriving. Memory must stay bounded however many turn up.
    for _ in range(200):
        feeder.send("wafer_start")
        held = writer._buffers.get(key)
        assert held is None or len(held.rows) <= CAP + 1

    # The whole point: bounding memory dropped nothing. Every row is still
    # pending, so replay - and purge_old, which never purges pending - keep it.
    assert [e.seq for e in journal.pending_csv(limit=1000)] == feeder.seqs
    assert all(
        journal.entry(seq).csv_status == "pending" for seq in feeder.seqs
    )


def test_evicted_rows_are_rewritten_once_the_sink_recovers(tmp_path, monkeypatch):
    journal, profile, machine, writer, feeder = _rig(
        tmp_path, broken=True, monkeypatch=monkeypatch
    )
    _storm(feeder, extra_rows=20)
    pending = journal.pending_csv(limit=1000)
    assert len(pending) == 24

    # Restart onto a healthy sink and replay in seq order, as
    # EapMiddlewareService._replay_journal does.
    healed = PerLotCsvWriter(journal=IngressJournal(journal.db_path))
    replay = _Feeder(healed, journal, machine, profile)
    written = []
    for entry in pending:
        replay.tick += 1
        event_type = "unloaded" if entry.ceid == CLOSES_LOT_CEID else "wafer_start"
        written += healed.append(
            machine, profile,
            _event(machine, event_type, entry.ingress_key,
                   ceid=entry.ceid, tick=replay.tick),
            seq=entry.seq,
        )
    # The tail of the stream is still an open lot; close it as the tool would.
    written += replay.send("unloaded", ceid=CLOSES_LOT_CEID)

    assert len(written) == 2 and all(path.is_file() for path in written)
    data_rows = sum(len(p.read_text().strip().splitlines()) - 1 for p in written)
    assert data_rows == 25  # the 24 replayed rows plus the closing event
    for entry in pending:
        assert journal.entry(entry.seq).csv_status == "done"
