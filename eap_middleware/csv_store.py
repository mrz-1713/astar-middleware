"""Per-lot CSV storage matching the IME EAP file contract."""

from __future__ import annotations

import csv
import logging
import os
import shutil
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional, Tuple

from .journal import IngressJournal
from .models import CanonicalEvent, CsvRow, MachineConfig
from .profiles import MachineProfile

logger = logging.getLogger(__name__)


CSV_EVENT_TYPES = {
    "ready_to_load",
    "loaded",
    "clamped",
    # Cassette slot-map result (NexGen MG portNCasMapped). Between placed and
    # lot_start, so it belongs in the lot file rather than being dropped.
    "mapped",
    "mounted",
    "lot_start",
    "wafer_start",
    "process_start",
    "process_end",
    "wafer_end",
    "lot_end",
    "unmounted",
    "unclamped",
    "ready_to_unload",
    "unloaded",
    # Fix #1: capture unknown/unaliased events too. The mapper logs a WARN
    # the first time it sees an unknown CEID; keep the row in the per-lot
    # CSV instead of silently dropping it.
    "unknown",
}


@dataclass
class LotBuffer:
    """Rows buffered for one open lot.

    Each row carries the ingress-journal sequence behind it, so a lot still
    open when the process died can be replayed rather than lost.
    """

    machine: MachineConfig
    rows: List[CsvRow] = field(default_factory=list)
    # Ingress-journal sequence behind each row, positionally aligned with
    # `rows`. A row with no journal entry (direct/legacy callers) carries None.
    # This is what lets a lot that was still open when the process died be
    # rebuilt from the journal instead of vanishing with the buffer.
    row_seqs: List[Optional[int]] = field(default_factory=list)
    lot_id: str = ""
    load_port: str = ""
    start_timestamp: Optional[datetime] = None
    ended: bool = False

    def add(self, row: CsvRow, seq: Optional[int]) -> None:
        self.rows.append(row)
        # Pad first: a buffer built by an external caller may have rows and no
        # seqs, and the two lists must stay index-aligned.
        while len(self.row_seqs) < len(self.rows) - 1:
            self.row_seqs.append(None)
        self.row_seqs.append(seq)

    def seqs(self) -> List[int]:
        return [seq for seq in self.row_seqs if seq is not None]


_PRE_LOT_DEFAULT_TTL_SEC = 3600.0  # 1 hour
_PRE_LOT_MAX_ENTRIES_PER_KEY = 200

# Ceiling on rows held for one open lot *once its write has already failed*.
# A lot file that cannot be written keeps its buffer (see _write_and_remove),
# so the next lot's rows land in the same buffer and every close re-serialises
# all of them - unbounded memory and O(n^2) work while the sink stays down.
# Past this many rows the buffer is evicted instead. Nothing is dropped: the
# rows leave memory still csv_status='pending' in the journal, so replay
# rebuilds them. A healthy lot never reaches this path however long it runs,
# because eviction requires a recorded write failure.
_MAX_ROWS_PER_LOT_BUFFER = 20000

# Rolling diagnostic windows, not ledgers. The journal is the durable record of
# what happened to every row; these only answer "what happened lately?".
_RECENT_FILES_MAX = 500
_RECENT_ERRORS_MAX = 200


class PerLotCsvWriter:
    """Writes one CSV file per lot.

    Rows are buffered while a lot is open and flushed when the closing event
    arrives. Rows that arrive before a lot is identified are held in a
    bounded, TTL-expired pre-lot buffer.
    """

    def __init__(
        self,
        pre_lot_ttl_sec: float = _PRE_LOT_DEFAULT_TTL_SEC,
        pre_lot_max_entries: int = _PRE_LOT_MAX_ENTRIES_PER_KEY,
        journal: Optional[IngressJournal] = None,
        max_lot_rows: int = _MAX_ROWS_PER_LOT_BUFFER,
    ):
        self._buffers: Dict[Tuple[str, str], LotBuffer] = {}
        # v2 Track A: each pending row is now stamped with the moment it was
        # received so we can prune entries older than TTL on every append.
        self._pending_pre_lot: Dict[
            Tuple[str, str], List[Tuple[datetime, CsvRow, Optional[int]]]
        ] = {}
        self._pre_lot_ttl_sec = float(pre_lot_ttl_sec)
        self._pre_lot_max_entries = int(pre_lot_max_entries)
        self._max_lot_rows = int(max_lot_rows)
        # (endpoint_id, load_port) -> consecutive local-write failures. Keyed
        # by load port rather than held on LotBuffer because the buffer is
        # replaced - by eviction, or by a lot change - while the sink stays
        # broken, and a fresh buffer that forgot the failures would start
        # accumulating without a ceiling all over again.
        self._write_failures: Dict[Tuple[str, str], int] = {}
        # Bounded on purpose. These are diagnostics - "what did we write
        # recently", "what mirrors failed recently" - and a service that runs
        # for months writes hundreds of thousands of lot files. As plain lists
        # they grew for the life of the process and were never read back by
        # anything but a test.
        self.written_files: Deque[Path] = deque(maxlen=_RECENT_FILES_MAX)
        self.mirror_errors: Deque[str] = deque(maxlen=_RECENT_ERRORS_MAX)
        # Only used when there is no journal to hold the retry queue.
        self._pending_mirrors: List[Tuple[Path, Path]] = []
        # Set by the service so a freshly queued mirror wakes CsvMirrorWorker
        # immediately instead of waiting out its poll interval. None for
        # direct callers, which have no worker.
        self._mirror_wake: Optional[threading.Event] = None
        # The journal is this sink's completion record. A row stays replayable
        # there until the CSV holding it is on disk, which is what makes an
        # in-memory lot buffer survive a crash. None keeps the standalone
        # behaviour for direct callers that have no journal.
        self._journal = journal
        # seq -> how many buffered rows still reference it, plus whether any of
        # those rows was deliberately discarded. The journal entry is only
        # resolved when the last row referencing it is accounted for.
        self._seq_refs: Dict[int, int] = {}
        self._seq_dropped: Dict[int, str] = {}
        # One lock for the whole writer. Dispatcher threads of every machine
        # plus the supervisor's journal-replay thread mutate _buffers,
        # _pending_pre_lot and _seq_refs concurrently; without it the
        # refcounts could mark a journal entry done while a row is still
        # buffered (silent CSV row loss) or interleave buffer promotion.
        self._lock = threading.Lock()

    def set_mirror_wake_event(self, wake: threading.Event) -> None:
        """Attach the service-owned event used to wake the mirror worker."""
        self._mirror_wake = wake

    def holds(self, seq: int) -> bool:
        """True while some in-memory row still references this journal entry.

        Journal replay uses this to avoid re-adding rows that are simply
        sitting in an open lot buffer rather than lost.
        """
        return seq in self._seq_refs

    def append(
        self,
        machine: MachineConfig,
        profile: MachineProfile,
        event: CanonicalEvent,
        seq: Optional[int] = None,
    ) -> List[Path]:
        with self._lock:
            return self._append_locked(machine, profile, event, seq)

    def _append_locked(
        self,
        machine: MachineConfig,
        profile: MachineProfile,
        event: CanonicalEvent,
        seq: Optional[int],
    ) -> List[Path]:
        if event.event_type not in CSV_EVENT_TYPES:
            # Nothing was lost: this event type has no place in a lot file.
            if seq is not None and self._journal is not None:
                self._journal.mark_csv_skipped(seq)
            return []
        row = self._row_from_event(profile, event)
        load_port = event.load_port or "NA"
        key = (machine.endpoint_id, load_port)
        if not event.lot_id and not self._buffers.get(key):
            self._prune_pre_lot(key)
            now = datetime.now(timezone.utc)
            pending = self._pending_pre_lot.setdefault(key, [])
            pending.append((now, row, seq))
            self._track(seq)
            # v2 Track A: hard cap. If a lot_start never arrives the buffer
            # would otherwise grow forever. Drop the oldest entry and warn.
            if len(pending) > self._pre_lot_max_entries:
                dropped = pending.pop(0)
                logger.warning(
                    "PerLotCsvWriter pre-lot cap reached for %s (load_port=%s); "
                    "dropping oldest row dated %s",
                    machine.endpoint_id, load_port, dropped[0].isoformat(),
                )
                self._release([dropped[2]], reason="pre-lot buffer cap reached")
            return []

        buffer = self._get_buffer(machine, key, event)
        if buffer.lot_id and event.lot_id and buffer.lot_id != event.lot_id:
            written = self._write_buffer(key, buffer, reason="lot_changed")
            self._buffers[key] = self._new_buffer(machine, event)
            buffer = self._buffers[key]
            return written + self._append_locked(machine, profile, event, seq)

        buffer.add(row, seq)
        self._track(seq)
        if event.lot_id and not buffer.lot_id:
            buffer.lot_id = event.lot_id
        if event.event_type == "lot_end":
            buffer.ended = True
        if profile.resolve_event(event.raw_event_name, event.ceid).closes_lot_file:
            return self._write_and_remove(key, reason="carrier_unloaded")
        # Only once a write for this load port has already failed: a healthy
        # lot, however long it runs, keeps its buffer until it closes.
        if self._write_failures.get(key) and len(buffer.rows) > self._max_lot_rows:
            self._evict_buffer(key, buffer)
        return []

    # ---------- journal accounting ----------

    def _track(self, seq: Optional[int]) -> None:
        if seq is None:
            return
        self._seq_refs[seq] = self._seq_refs.get(seq, 0) + 1

    def _release(
        self, seqs: Iterable[Optional[int]], reason: Optional[str] = None
    ) -> None:
        """Account for rows leaving memory, and resolve any journal entry whose
        last row just left.

        `reason` means the rows were discarded rather than written. A journal
        entry whose rows split between the two is resolved as dropped, because
        "some of it reached a CSV" is not the same as "all of it did" and the
        weaker claim is the safe one to record.
        """
        if self._journal is None:
            return
        done: List[int] = []
        for seq in seqs:
            if seq is None:
                continue
            if reason is not None:
                self._seq_dropped[seq] = reason
            remaining = self._seq_refs.get(seq, 0) - 1
            if remaining > 0:
                self._seq_refs[seq] = remaining
                continue
            self._seq_refs.pop(seq, None)
            dropped = self._seq_dropped.pop(seq, None)
            if dropped is not None:
                self._journal.mark_csv_dropped([seq], dropped)
            else:
                done.append(seq)
        self._journal.mark_csv_done(done)

    def flush_all(self, reason: str = "shutdown") -> List[Path]:
        with self._lock:
            return self._flush_all_locked(reason)

    def _flush_all_locked(self, reason: str = "shutdown") -> List[Path]:
        written: List[Path] = []
        for key in list(self._buffers):
            written.extend(self._write_and_remove(key, reason=reason))
        # Rows received before their lot_start have no lot id yet, so there is
        # no file to write them to. Say so rather than dropping them silently.
        stranded = sum(len(rows) for rows in self._pending_pre_lot.values())
        if stranded:
            logger.warning(
                "%s: %d pre-lot row(s) on %d port(s) had no lot_start and are "
                "not in any CSV: %s",
                reason, stranded, len(self._pending_pre_lot),
                sorted(self._pending_pre_lot),
            )
        return written

    def flush_machine(self, endpoint_id: str, reason: str = "stopped") -> List[Path]:
        """Flush only one endpoint, leaving every other open lot untouched."""
        with self._lock:
            return self._flush_machine_locked(endpoint_id, reason)

    def _flush_machine_locked(
        self, endpoint_id: str, reason: str = "stopped"
    ) -> List[Path]:
        written: List[Path] = []
        for key in [key for key in self._write_failures if key[0] == endpoint_id]:
            self._write_failures.pop(key, None)
        for key in [key for key in self._buffers if key[0] == endpoint_id]:
            written.extend(self._write_and_remove(key, reason=reason))
        for key in [key for key in self._pending_pre_lot if key[0] == endpoint_id]:
            # These rows never reached a lot file, so they stay unresolved in
            # the journal on purpose: dropping the reference here lets a later
            # replay put them back into whichever lot eventually opens, instead
            # of them ending at the moment the machine stopped.
            for _ts, _row, seq in self._pending_pre_lot.pop(key, []):
                if seq is not None:
                    remaining = self._seq_refs.get(seq, 0) - 1
                    if remaining > 0:
                        self._seq_refs[seq] = remaining
                    else:
                        self._seq_refs.pop(seq, None)
                        self._seq_dropped.pop(seq, None)
        return written

    def _get_buffer(
        self,
        machine: MachineConfig,
        key: Tuple[str, str],
        event: CanonicalEvent,
    ) -> LotBuffer:
        if key not in self._buffers:
            self._buffers[key] = self._new_buffer(machine, event)
            # Prune any stale pending rows before promoting them into a buffer
            self._prune_pre_lot(key)
            pending_entries = self._pending_pre_lot.pop(key, [])
            pending_rows = [row for (_ts, row, _seq) in pending_entries]
            for _ts, row, seq in pending_entries:
                # The row changes container rather than arriving anew, so the
                # journal reference it already holds simply carries over.
                self._buffers[key].add(row, seq)
            if pending_rows:
                first_ts = self._parse_row_datetime(pending_rows[0])
                if first_ts is not None:
                    self._buffers[key].start_timestamp = first_ts
        return self._buffers[key]

    def _prune_pre_lot(self, key: Tuple[str, str]) -> None:
        """Drop pending entries whose age exceeds the TTL. Called on every
        append + buffer promotion so the structure can't grow unboundedly
        when a lot_start never arrives."""
        bucket = self._pending_pre_lot.get(key)
        if not bucket:
            return
        cutoff = datetime.now(timezone.utc).timestamp() - self._pre_lot_ttl_sec
        # Drop from the head while expired; entries are appended in order so
        # the first non-expired entry means everything after is also fresh.
        dropped: List[Tuple[datetime, CsvRow, Optional[int]]] = []
        while bucket and bucket[0][0].timestamp() < cutoff:
            dropped.append(bucket.pop(0))
        if dropped:
            logger.warning(
                "PerLotCsvWriter pruned %d pre-lot rows past TTL for %s",
                len(dropped), key,
            )
            # The cap path above records each dropped row against its journal
            # entry so the entry reaches a terminal state and retention can
            # purge it. The TTL path must do the same: without _release the
            # entry stays csv_status='pending' forever, holds() keeps replay
            # skipping it, and the journal grows without bound.
            self._release(
                [seq for (_ts, _row, seq) in dropped],
                reason="pre-lot TTL expired",
            )
        if not bucket:
            self._pending_pre_lot.pop(key, None)

    def _new_buffer(self, machine: MachineConfig, event: CanonicalEvent) -> LotBuffer:
        return LotBuffer(
            machine=machine,
            lot_id=event.lot_id,
            load_port=event.load_port or "NA",
            start_timestamp=event.timestamp,
        )

    def _row_from_event(self, profile: MachineProfile, event: CanonicalEvent) -> CsvRow:
        mapping = profile.resolve_event(event.raw_event_name, event.ceid)
        return CsvRow(
            # Local wall time, explicitly. The column is read next to the tool,
            # whose HMI shows the same clock, and `_parse_row_datetime` reads
            # it back on that basis. Rendering `event.timestamp` directly used
            # to emit whichever zone the event happened to carry - equipment
            # local when the report had a CLOCK, UTC when it did not - so one
            # lot file could hold rows on two clocks with nothing to say so.
            datetime=event.timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S.%f"),
            tool_event=mapping.csv_tool_event,
            eap_toolname=event.display_name,
            load_port=event.load_port,
            chamber=event.chamber,
            lot_id=event.lot_id,
            wafer_id=event.wafer_id,
            recipe=event.recipe,
            secsgem_raw_event=event.secs_raw_event or mapping.secs_raw_event,
        )

    def _write_and_remove(self, key: Tuple[str, str], reason: str) -> List[Path]:
        buffer = self._buffers.get(key)
        if buffer is None:
            return []
        written = self._write_buffer(key, buffer, reason=reason)
        # Keep the only in-memory copy until the local atomic write succeeds.
        self._buffers.pop(key, None)
        return written

    def _write_buffer(
        self, key: Tuple[str, str], buffer: LotBuffer, reason: str
    ) -> List[Path]:
        if not buffer.rows:
            return []
        local_dir = buffer.machine.csv_local_dir
        local_dir.mkdir(parents=True, exist_ok=True)
        partial = reason in {
            "stopped",
            "service_stop",
            "disabled_or_removed",
            "config_change",
        } and not buffer.ended
        filename = self._filename(buffer, partial=partial)
        local_path = local_dir / filename
        try:
            self._write_atomic(local_path, buffer.rows)
        except Exception:
            # The caller keeps the buffer so the rows are not lost, and the
            # service records csv_status against the journal. Count it here so
            # the row cap can tell a broken sink from a long lot.
            self._write_failures[key] = self._write_failures.get(key, 0) + 1
            raise
        self._write_failures.pop(key, None)
        self.written_files.append(local_path)

        written = [local_path]
        network_dir = buffer.machine.csv_network_dir
        if network_dir is not None:
            mirror_path = network_dir / filename
            # Queued, never copied on this thread.
            #
            # _write_buffer runs inside the S6F11 acknowledgement path: the
            # tool is holding the transaction open waiting for S6F12, and its
            # T3 reply timeout is 30-45s depending on the profile. A copy to
            # an unreachable SMB share blocks for the OS timeout, which on
            # Windows is comfortably longer than that - so copying inline let
            # a sick file share push the *equipment* into declaring a
            # communications failure. The tool then retransmits; the ingress
            # journal collapses the duplicate correctly, but throughput falls
            # apart for a reason that has nothing to do with SECS.
            #
            # Nothing is risked by deferring. The local file is fsynced above,
            # the task row is durable in the journal before we return, and
            # CsvMirrorWorker retries it with backoff across restarts. The
            # mirror was never part of the no-loss guarantee - the journal is.
            #
            # Enqueue the durable mirror task BEFORE releasing the journal
            # rows below: a crash between the local write and the release must
            # not leave the network copy neither performed nor recorded. Doing
            # it in the other order marked the rows CSV-done first, so a crash
            # in the gap skipped the copy silently.
            if self._journal is not None:
                self._journal.enqueue_mirror(local_path, mirror_path)
            else:
                self._enqueue_mirror(local_path, mirror_path)
            logger.debug(
                "Queued network mirror %s -> %s", local_path, mirror_path
            )
            # Nudge CsvMirrorWorker so deferring costs latency, not minutes:
            # without this the copy waits for the worker's next poll.
            if self._mirror_wake is not None:
                self._mirror_wake.set()

        # The rows are on local disk now and any mirror task is already durably
        # enqueued, so the journal no longer has to hold them open. The mirror
        # keeps its own durable queue and must not keep the lot replayable -
        # that would rebuild a file that now exists.
        self._release(buffer.row_seqs)

        logger.info("Wrote lot CSV %s (%s)", local_path, reason)
        return written

    def _evict_buffer(self, key: Tuple[str, str], buffer: LotBuffer) -> None:
        """Drop an over-long lot buffer from memory *without* resolving its
        journal rows, so replay rebuilds it.

        This is deliberately not `_release(..., reason=...)`: that marks the
        rows csv-dropped, which is data loss. Here the rows stay
        csv_status='pending', `holds()` stops shadowing them, `purge_old()`
        refuses to purge them, and `_replay_journal` re-appends them in seq
        order once the sink is writable again - so the rebuilt lot file is the
        same file, just written later.

        Rows carrying no journal seq (direct/legacy callers, which have no
        journal at all) cannot be rebuilt; those are reported separately
        because they really are gone.
        """
        self._buffers.pop(key, None)
        replayable = 0
        unrecoverable = 0
        for seq in buffer.row_seqs:
            if seq is None:
                unrecoverable += 1
                continue
            remaining = self._seq_refs.get(seq, 0) - 1
            if remaining > 0:
                self._seq_refs[seq] = remaining
                continue
            self._seq_refs.pop(seq, None)
            # The seq goes back to being purely the journal's business, so any
            # in-memory "was partly discarded" marker goes with it; replay
            # re-derives that decision from scratch.
            self._seq_dropped.pop(seq, None)
            replayable += 1
        logger.error(
            "PerLotCsvWriter evicted lot buffer for %s (load_port=%s, lot=%s) "
            "after %d rows and %d failed write(s): the local CSV sink is not "
            "accepting writes. %d row(s) stay pending in the journal and will "
            "be rewritten by replay once it recovers; %d row(s) had no journal "
            "entry and are lost.",
            key[0], key[1], buffer.lot_id or "?", len(buffer.rows),
            self._write_failures.get(key, 0), replayable, unrecoverable,
        )

    def _enqueue_mirror(self, source: Path, destination: Path) -> None:
        if self._journal is not None:
            self._journal.enqueue_mirror(source, destination)
            return
        pending = (source, destination)
        if pending not in self._pending_mirrors:
            self._pending_mirrors.append(pending)

    def retry_mirrors(self) -> int:
        """Retry failed mirrors without touching the durable local source.

        With a journal the queue is on disk, so a share that stays unreachable
        across a restart still gets its copies afterwards. It used to be a
        plain list, so a restart meant the network copy was simply never made
        and only the local file existed.

        Only a small, leased, due batch is attempted per call: each copy to an
        unreachable share blocks for the OS timeout. This runs on the dedicated
        CsvMirrorWorker, not the supervisor thread, so a dead share no longer
        stalls config reload or journal replay - the batch limit is a pure
        throughput cap (copies per poll) on a healthy share. See
        journal.MIRROR_BATCH_LIMIT.
        """
        completed = 0
        if self._journal is not None:
            for task in self._journal.pending_mirrors():
                if not task.source.exists():
                    logger.warning(
                        "Mirror source %s is gone; abandoning the copy to %s",
                        task.source, task.destination,
                    )
                    self._journal.complete_mirror(task.id)
                    continue
                try:
                    task.destination.parent.mkdir(parents=True, exist_ok=True)
                    self._copy_atomic(task.source, task.destination)
                except Exception as exc:
                    # Recorded here, not just logged. mirror_errors is the
                    # writer's "what failed lately" surface and the panel
                    # reads it; it used to be populated by the inline copy in
                    # _write_buffer, so moving every attempt onto this worker
                    # would otherwise have left an unreachable share silently
                    # absent from it.
                    message = (
                        f"Mirror failed for {task.source} -> "
                        f"{task.destination}: {exc}"
                    )
                    self.mirror_errors.append(message)
                    logger.warning("%s", message)
                    self._journal.fail_mirror(task.id, str(exc))
                    continue
                self._journal.complete_mirror(task.id)
                completed += 1
            return completed
        for source, destination in list(self._pending_mirrors):
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                self._copy_atomic(source, destination)
            except Exception as exc:
                message = f"Mirror failed for {source} -> {destination}: {exc}"
                self.mirror_errors.append(message)
                logger.warning("%s", message)
                continue
            self._pending_mirrors.remove((source, destination))
            completed += 1
        return completed

    def _filename(self, buffer: LotBuffer, partial: bool = False) -> str:
        start = buffer.start_timestamp or datetime.now(timezone.utc)
        # Local wall time, matching the Datetime column inside the file. Both
        # sources of `start_timestamp` are aware, so this is now one clock.
        date_part = start.astimezone().strftime("%Y%m%d_%H%M%S_%f")
        # Include load_port so two carriers that load in the same microsecond
        # (multi-LP tools that fire MBCStart1/MBCStart2 together) don't
        # collide and overwrite each other via os.replace().
        port = buffer.load_port or "NA"
        port_safe = "".join(c if c.isalnum() else "_" for c in port)
        suffix = ".partial.csv" if partial else ".csv"
        return f"{buffer.machine.display_name}_Lot_{date_part}_LP{port_safe}{suffix}"

    def _parse_row_datetime(self, row: CsvRow) -> Optional[datetime]:
        """Read back the local wall time `_row_from_event` wrote.

        This used to stamp the value UTC. The column has always been written
        in the tool's own zone, so on any site not on UTC the result was off
        by that offset - and it feeds `LotBuffer.start_timestamp`, which names
        the lot file. Two lots off one tool were therefore named on two
        different clocks depending on which path promoted the buffer.
        `astimezone()` on a naive value interprets it as host-local, which is
        exactly the zone it was written in.
        """
        try:
            return datetime.strptime(
                row.datetime, "%Y-%m-%d %H:%M:%S.%f"
            ).astimezone()
        except ValueError:
            return None

    def _write_atomic(self, path: Path, rows: List[CsvRow]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(CsvRow.header())
            for row in rows:
                writer.writerow(row.values())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        self._fsync_dir(path.parent)

    def _copy_atomic(self, source: Path, destination: Path) -> None:
        tmp = destination.with_suffix(destination.suffix + ".tmp")
        shutil.copyfile(source, tmp)
        os.replace(tmp, destination)
        self._fsync_dir(destination.parent)

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """Durably commit the rename on POSIX; a no-op on Windows, where
        directory handles cannot be fsynced (the file itself was fsynced
        before the replace)."""
        if os.name == "nt":
            return
        try:
            descriptor = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)
