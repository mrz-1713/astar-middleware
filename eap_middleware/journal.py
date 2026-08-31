"""Durable ingress journal - the middleware's record of what it accepted.

Every SECS message the gateway is about to acknowledge is written here first,
with ``synchronous=FULL``, so "the tool was told we have it" and "we actually
have it" cannot disagree across a crash or a power cut. Everything downstream
(MQTT, HTTPS, the legacy API, the per-lot CSV files) is a derived view that
replays from this log, in arrival order, per machine.

Two independent completion states are tracked per entry because the sinks have
different durability. ``dispatch`` covers the publishers, which own durable
SQLite outboxes of their own and only need the entry until it is queued.
``csv`` covers the per-lot files, which accumulate in memory for the length of
a lot and are only safe once the file is on disk - so a lot's rows stay
replayable here until the CSV that contains them has been written.

Nothing is ever discarded silently. A row that a sink deliberately refuses
(rate-shed alarm, pre-lot row past its TTL) is marked ``dropped`` with a
reason and keeps its full payload for the retention window, so an operator can
always answer "what happened to that event?".
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Stamped by the middleware on arrival rather than carried by the equipment.
# They must not take part in identity: a message the tool retransmits after a
# T3 timeout arrives with a new wall clock but is the same event.
VOLATILE_PAYLOAD_KEYS = ("received_at", "timestamp")

PENDING = "pending"
DONE = "done"
DROPPED = "dropped"
SKIPPED = "skipped"

# Terminal states, i.e. this sink will never look at the row again.
_TERMINAL = (DONE, DROPPED, SKIPPED)

KIND_EVENT = "event"
KIND_ALARM = "alarm"

# Network-mirror retry policy. A copy to an unreachable SMB share blocks for the
# OS timeout - tens of seconds on Windows - so the queue is drained a few tasks
# at a time with exponential backoff rather than in full on every pass.
MIRROR_BATCH_LIMIT = 8
MIRROR_BACKOFF_BASE_SEC = 5.0
MIRROR_BACKOFF_MAX_SEC = 300.0
# How long a claimed task stays claimed. Long enough to outlast a blocking
# copy, short enough that a crashed worker's tasks come back on their own.
MIRROR_LEASE_SEC = 300.0


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def compute_ingress_key(
    *,
    endpoint_id: str,
    stream: int,
    function: int,
    ceid: int,
    system_bytes: Optional[int],
    payload: Dict[str, Any],
) -> str:
    """Identity of one accepted SECS transaction, derived from its content.

    SEMI E5 retransmits an unacknowledged primary message with its original
    system bytes, so (endpoint, S/F, system bytes, body) names the transaction
    rather than the moment it landed. That is what makes a retry collapse onto
    the delivery it is retrying instead of becoming a second event downstream.

    The body digest is carried alongside the system bytes rather than trusting
    them alone: a tool that restarts its counter can reuse a value for a
    genuinely different message, and that must stay a separate event.

    When the transport gives us no system bytes the arrival stamp is folded
    back in, so two otherwise indistinguishable messages stay two events.
    Failing towards a duplicate is recoverable; failing towards a swallowed
    event is not.
    """
    if system_bytes is None:
        content: Dict[str, Any] = dict(payload)
    else:
        content = {
            key: value
            for key, value in payload.items()
            if key not in VOLATILE_PAYLOAD_KEYS
        }
    # The CEID is carried explicitly because it does not always appear in the
    # payload - E40 notifications and direct callers pass it alongside - and
    # two different collection events can otherwise share a body verbatim.
    blob = _canonical(
        [endpoint_id, stream, function, int(ceid), system_bytes, content]
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class JournalEntry:
    """One accepted SECS transaction, exactly as it arrived."""

    seq: int
    endpoint_id: str
    kind: str
    stream: int
    function: int
    ceid: int
    ingress_key: str
    payload: Dict[str, Any]
    received_at: float
    last_received_at: float
    generation: int
    dispatch_status: str = PENDING
    csv_status: str = PENDING


@dataclass(frozen=True)
class MirrorTask:
    """A per-lot CSV copy to the network share that has not landed yet."""

    id: int
    source: Path
    destination: Path
    attempts: int


class IngressJournal:
    """Append-only, crash-safe record of accepted SECS traffic."""

    def __init__(
        self,
        db_path: str | Path,
        retention_days: int = 30,
        cross_generation_window_sec: float = 120.0,
    ):
        self.db_path = Path(db_path)
        self.retention_days = retention_days
        self.cross_generation_window_sec = float(cross_generation_window_sec)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    # ---------- connection ----------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        # The whole point of this file. NORMAL lets a commit return before the
        # write-ahead log reaches the platter, which is exactly the window in
        # which an acknowledged event can evaporate.
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _init_db(self) -> None:
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ingress (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    endpoint_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    stream INTEGER NOT NULL,
                    function INTEGER NOT NULL,
                    ceid INTEGER NOT NULL DEFAULT 0,
                    ingress_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    received_at REAL NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 0,
                    dispatch_status TEXT NOT NULL DEFAULT 'pending',
                    dispatch_attempts INTEGER NOT NULL DEFAULT 0,
                    dispatch_error TEXT,
                    csv_status TEXT NOT NULL DEFAULT 'pending',
                    csv_reason TEXT
                )
                """
            )
            # Versioned identity migration. ``identity_key`` is the stable
            # wire transaction fingerprint; ``ingress_key`` identifies one
            # accepted occurrence after the cross-generation window policy is
            # applied. Existing rows retain their previous identity.
            ingress_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(ingress)").fetchall()
            }
            for column, ddl in (
                ("identity_key", "TEXT"),
                ("last_received_at", "REAL"),
            ):
                if column not in ingress_columns:
                    conn.execute(f"ALTER TABLE ingress ADD COLUMN {column} {ddl}")
            conn.execute(
                "UPDATE ingress SET identity_key=ingress_key "
                "WHERE identity_key IS NULL"
            )
            conn.execute(
                "UPDATE ingress SET last_received_at=received_at "
                "WHERE last_received_at IS NULL"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ingress_identity "
                "ON ingress(identity_key, seq DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ingress_dispatch "
                "ON ingress(dispatch_status, seq)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ingress_csv "
                "ON ingress(csv_status, seq)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ingress_endpoint "
                "ON ingress(endpoint_id, seq)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS csv_mirror (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    UNIQUE(source, destination)
                )
                """
            )
            # Added after the original table shipped, so existing journals get
            # them by ALTER rather than losing their queued copies to a
            # recreate. next_attempt_at holds a task back after a failure;
            # leased_until stops two threads copying the same file at once.
            existing_columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(csv_mirror)"
                ).fetchall()
            }
            for column, ddl in (
                ("next_attempt_at", "REAL NOT NULL DEFAULT 0"),
                ("leased_until", "REAL NOT NULL DEFAULT 0"),
            ):
                if column in existing_columns:
                    continue
                try:
                    conn.execute(
                        f"ALTER TABLE csv_mirror ADD COLUMN {column} {ddl}"
                    )
                except sqlite3.OperationalError:
                    # A concurrent process may have won the migration race.
                    # Only a column still missing after the failure is a real
                    # error; blanket-swallowing every OperationalError here
                    # used to hide a locked/read-only DB until pending_mirrors()
                    # blew up at runtime with no hint at the cause.
                    still_missing = (
                        column
                        not in {
                            row["name"]
                            for row in conn.execute(
                                "PRAGMA table_info(csv_mirror)"
                            ).fetchall()
                        }
                    )
                    if still_missing:
                        logger.exception(
                            "Failed to add column %s to csv_mirror", column
                        )
                        raise
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_csv_mirror_due "
                "ON csv_mirror(next_attempt_at, id)"
            )

    # ---------- ingress ----------

    def append(
        self,
        *,
        endpoint_id: str,
        kind: str,
        stream: int,
        function: int,
        payload: Dict[str, Any],
        ceid: int = 0,
        system_bytes: Optional[int] = None,
        generation: int = 0,
        csv_status: str = PENDING,
    ) -> tuple[JournalEntry, bool]:
        """Record one accepted transaction. Returns (entry, is_new).

        ``is_new`` is False when this exact transaction is already journaled -
        an equipment retransmission. The caller must not dispatch it again;
        the original delivery already covers it.

        Raises on any storage failure, which is deliberate: the caller is
        between "message received" and "acknowledgement sent", and a message we
        cannot store must not be acknowledged.
        """
        identity_key = compute_ingress_key(
            endpoint_id=endpoint_id,
            stream=stream,
            function=function,
            ceid=int(ceid),
            system_bytes=system_bytes,
            payload=payload,
        )
        now = time.time()
        payload_json = _canonical(payload)
        with self._lock, closing(self._connect()) as conn, conn:
            # Serialize the read/decision/insert sequence across processes as
            # well as threads. This makes the boundary deterministic when two
            # callbacks append the same reconnect retry concurrently.
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT * FROM ingress WHERE identity_key=? "
                "ORDER BY seq DESC LIMIT 1",
                (identity_key,),
            ).fetchone()
            if previous is not None:
                same_generation = int(previous["generation"]) == int(generation)
                inside_window = (
                    now - float(previous["received_at"])
                    <= self.cross_generation_window_sec
                )
                if same_generation or inside_window:
                    conn.execute(
                        "UPDATE ingress SET last_received_at=? WHERE seq=?",
                        (now, int(previous["seq"])),
                    )
                    refreshed = conn.execute(
                        "SELECT * FROM ingress WHERE seq=?",
                        (int(previous["seq"]),),
                    ).fetchone()
                    conn.commit()
                    if refreshed is None:  # pragma: no cover - same transaction
                        raise RuntimeError("deduplicated ingress row vanished")
                    return (self._entry(refreshed), False)

            # A genuine repeat after a reconnect receives a new downstream
            # identity. Include the generation and prior sequence so clock
            # precision can never cause a UNIQUE collision.
            if previous is None:
                key = identity_key
            else:
                occurrence = _canonical(
                    [identity_key, int(generation), int(previous["seq"]), now]
                )
                key = hashlib.sha256(occurrence.encode("utf-8")).hexdigest()
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO ingress
                    (endpoint_id, kind, stream, function, ceid, ingress_key,
                     identity_key, payload_json, received_at, last_received_at,
                     generation, csv_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    endpoint_id, kind, stream, function, int(ceid), key,
                    identity_key, payload_json, now, now, int(generation),
                    csv_status,
                ),
            )
            if cursor.rowcount:
                conn.commit()
                return (
                    JournalEntry(
                        seq=int(cursor.lastrowid or 0),
                        endpoint_id=endpoint_id,
                        kind=kind,
                        stream=stream,
                        function=function,
                        ceid=int(ceid),
                        ingress_key=key,
                        payload=payload,
                        received_at=now,
                        last_received_at=now,
                        generation=int(generation),
                        csv_status=csv_status,
                    ),
                    True,
                )
            row = conn.execute(
                "SELECT * FROM ingress WHERE ingress_key=?", (key,)
            ).fetchone()
            conn.commit()
        if row is None:  # pragma: no cover - the UNIQUE index guarantees one
            raise RuntimeError(f"ingress row vanished for key {key}")
        return (self._entry(row), False)

    @staticmethod
    def _entry(row: sqlite3.Row) -> JournalEntry:
        return JournalEntry(
            seq=int(row["seq"]),
            endpoint_id=str(row["endpoint_id"]),
            kind=str(row["kind"]),
            stream=int(row["stream"]),
            function=int(row["function"]),
            ceid=int(row["ceid"]),
            ingress_key=str(row["ingress_key"]),
            payload=json.loads(str(row["payload_json"])),
            received_at=float(row["received_at"]),
            last_received_at=float(row["last_received_at"]),
            generation=int(row["generation"]),
            dispatch_status=str(row["dispatch_status"]),
            csv_status=str(row["csv_status"]),
        )

    def integrity_check(self) -> bool:
        """Return whether SQLite considers the durable journal intact."""
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row and str(row[0]).lower() == "ok")

    def database_size_bytes(self) -> int:
        """On-disk size including WAL/SHM sidecars when present."""
        # SQLite removes the -wal and -shm sidecars when the last connection
        # closes, so exists()-then-stat() is a race the status writer loses
        # under load. Treat a vanished sidecar as zero bytes.
        total = 0
        for path in (
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ):
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def latest_generation(self, endpoint_id: str) -> int:
        """Highest persisted connection generation for one endpoint."""
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT MAX(generation) FROM ingress WHERE endpoint_id=?",
                (endpoint_id,),
            ).fetchone()
        return int(row[0] or 0) if row else 0

    # ---------- dispatch state ----------

    def mark_dispatched(self, seq: int) -> None:
        self._set(seq, "dispatch_status", DONE)

    def mark_dispatch_dropped(self, seq: int, reason: str) -> None:
        """Refused on purpose (e.g. shed by the alarm rate limiter).

        The payload stays; only the delivery is abandoned, and the reason is
        recorded so the gap is explainable rather than mysterious.
        """
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE ingress SET dispatch_status=?, dispatch_error=? "
                "WHERE seq=?",
                (DROPPED, reason[:1000], int(seq)),
            )

    def mark_dispatch_failed(self, seq: int, error: str) -> None:
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                """
                UPDATE ingress
                SET dispatch_attempts = dispatch_attempts + 1,
                    dispatch_error = ?
                WHERE seq = ?
                """,
                (error[:1000], int(seq)),
            )

    def dispatch_attempts(self, seq: int) -> int:
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT dispatch_attempts FROM ingress WHERE seq=?", (int(seq),)
            ).fetchone()
        return int(row["dispatch_attempts"]) if row else 0

    # ---------- csv state ----------

    def mark_csv_done(self, seqs: Sequence[int]) -> None:
        """Called once the lot file containing these rows is on disk."""
        if not seqs:
            return
        with self._lock, closing(self._connect()) as conn, conn:
            conn.executemany(
                "UPDATE ingress SET csv_status=?, csv_reason=NULL WHERE seq=?",
                [(DONE, int(seq)) for seq in seqs],
            )

    def mark_csv_dropped(self, seqs: Sequence[int], reason: str) -> None:
        if not seqs:
            return
        with self._lock, closing(self._connect()) as conn, conn:
            conn.executemany(
                "UPDATE ingress SET csv_status=?, csv_reason=? WHERE seq=?",
                [(DROPPED, reason[:1000], int(seq)) for seq in seqs],
            )

    def mark_csv_skipped(self, seq: int) -> None:
        """This event type has no place in a per-lot CSV; nothing was lost."""
        self._set(seq, "csv_status", SKIPPED)

    def mark_csv_failed(self, seq: int, error: str) -> None:
        """The CSV sink raised. The entry stays pending so replay retries it.

        Recorded against csv_reason rather than the dispatch columns: a lot
        file that could not be written says nothing about whether the event
        reached the publishers. Collapsing the two used to overwrite a
        successful dispatch with a failure and, after ten replays, park the
        entry claiming a publish that did happen never did.
        """
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE ingress SET csv_reason=? WHERE seq=?",
                (error[:1000], int(seq)),
            )

    def _set(self, seq: int, column: str, value: str) -> None:
        # Not an assert: `python -O` strips those, and this guard is what keeps
        # the f-string below from interpolating a caller-supplied column name.
        if column not in {"dispatch_status", "csv_status"}:
            raise ValueError(f"refusing to update unknown column {column!r}")
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                f"UPDATE ingress SET {column}=? WHERE seq=?",  # nosec B608
                (value, int(seq)),
            )

    # ---------- replay ----------

    def pending_dispatch(self, limit: int = 500) -> List[JournalEntry]:
        return self._pending("dispatch_status", limit)

    def pending_csv(self, limit: int = 500) -> List[JournalEntry]:
        return self._pending("csv_status", limit)

    def _pending(self, column: str, limit: int) -> List[JournalEntry]:
        # Same guard, and for the same reason, as `_set`: the column name is
        # interpolated into the SQL below, so it must never be able to come
        # from a caller. Both wrappers above pass a literal today - this keeps
        # that true if a third one is ever added.
        if column not in {"dispatch_status", "csv_status"}:
            raise ValueError(f"refusing to query unknown column {column!r}")
        with self._lock, closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM ingress WHERE {column}=? ORDER BY seq LIMIT ?",  # nosec B608
                (PENDING, int(limit)),
            ).fetchall()
        return [self._entry(row) for row in rows]

    def entry(self, seq: int) -> Optional[JournalEntry]:
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM ingress WHERE seq=?", (int(seq),)
            ).fetchone()
        return self._entry(row) if row is not None else None

    # ---------- network mirror queue ----------

    def enqueue_mirror(self, source: Path, destination: Path) -> Optional[int]:
        """Record a pending network-copy task, returning its id.

        The task row is written before the copy is attempted so a crash
        between the local write and the copy leaves a durable record for
        retry_mirrors() instead of a silently skipped network copy.
        """
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO csv_mirror
                    (source, destination, created_at)
                VALUES (?, ?, ?)
                """,
                (str(source), str(destination), time.time()),
            )
            row = conn.execute(
                "SELECT id FROM csv_mirror WHERE source=? AND destination=?",
                (str(source), str(destination)),
            ).fetchone()
        return int(row["id"]) if row is not None else None

    def pending_mirrors(
        self, limit: int = MIRROR_BATCH_LIMIT, lease_sec: float = MIRROR_LEASE_SEC
    ) -> List[MirrorTask]:
        """Claim up to `limit` mirror copies that are due, and lease them.

        The dispatcher (PerLotCsvWriter._write_buffer) only *enqueues* the
        task; CsvMirrorWorker is the single thread that copies, calling this
        to claim rows and complete_mirror/fail_mirror to close them out. The
        lease set here is what stops two workers - or two processes after a
        restart while a copy was still blocked - from racing the same task to
        the same temp file. And it must not hand back every queued task on
        every call: with the share unreachable each copy blocks for the OS
        timeout, so an unfiltered queue turned one dead share into a
        multi-minute stall of the whole copy loop.
        """
        now = time.time()
        with self._lock, closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT id, source, destination, attempts FROM csv_mirror "
                "WHERE next_attempt_at <= ? AND leased_until <= ? "
                "ORDER BY next_attempt_at, id LIMIT ?",
                (now, now, int(limit)),
            ).fetchall()
            if rows:
                conn.executemany(
                    "UPDATE csv_mirror SET leased_until=? WHERE id=?",
                    [(now + float(lease_sec), int(r["id"])) for r in rows],
                )
        return [
            MirrorTask(
                id=int(row["id"]),
                source=Path(str(row["source"])),
                destination=Path(str(row["destination"])),
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]

    def complete_mirror(self, mirror_id: int) -> None:
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM csv_mirror WHERE id=?", (int(mirror_id),))

    def fail_mirror(self, mirror_id: int, error: str) -> None:
        """Record a failed copy and hold the task back with capped backoff."""
        with self._lock, closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT attempts FROM csv_mirror WHERE id=?", (int(mirror_id),)
            ).fetchone()
            attempts = (int(row["attempts"]) if row is not None else 0) + 1
            delay = min(
                MIRROR_BACKOFF_MAX_SEC,
                MIRROR_BACKOFF_BASE_SEC * (2 ** min(attempts - 1, 16)),
            )
            conn.execute(
                "UPDATE csv_mirror SET attempts=?, last_error=?, "
                "next_attempt_at=?, leased_until=0 WHERE id=?",
                (attempts, error[:1000], time.time() + delay, int(mirror_id)),
            )

    # ---------- maintenance ----------

    def purge_old(self) -> int:
        """Drop entries both sinks are finished with and that outlived
        retention. A row still pending anywhere is never purged - the queue
        it is waiting on may simply be down."""
        cutoff = time.time() - (self.retention_days * 86400)
        with self._lock, closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                DELETE FROM ingress
                WHERE dispatch_status IN (?, ?, ?)
                  AND csv_status IN (?, ?, ?)
                  AND received_at < ?
                """,
                (*_TERMINAL, *_TERMINAL, cutoff),
            )
            return int(cursor.rowcount)

    def stats(self) -> Dict[str, int]:
        with self._lock, closing(self._connect()) as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM ingress").fetchone()
            dispatch = conn.execute(
                "SELECT COUNT(*) AS n FROM ingress WHERE dispatch_status=?",
                (PENDING,),
            ).fetchone()
            csv_pending = conn.execute(
                "SELECT COUNT(*) AS n FROM ingress WHERE csv_status=?",
                (PENDING,),
            ).fetchone()
            dropped = conn.execute(
                "SELECT COUNT(*) AS n FROM ingress WHERE dispatch_status=? "
                "OR csv_status=?",
                (DROPPED, DROPPED),
            ).fetchone()
            mirrors = conn.execute(
                "SELECT COUNT(*) AS n FROM csv_mirror"
            ).fetchone()
        return {
            "entries": int(total["n"]),
            "dispatch_pending": int(dispatch["n"]),
            "csv_pending": int(csv_pending["n"]),
            "dropped": int(dropped["n"]),
            "mirrors_pending": int(mirrors["n"]),
        }
