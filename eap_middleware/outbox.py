"""SQLite durable MQTT outbox with retention and retry accounting."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class OutboxFullError(RuntimeError):
    """The partition hit its safety cap; ingress must remain replayable."""


@dataclass(frozen=True)
class OutboxItem:
    """One queued publish attempt, read back from the outbox."""

    id: int
    topic: str
    payload: Dict[str, Any]
    attempts: int
    key: str
    partition_key: str


class SQLiteOutbox:
    """Durable publish queue backed by SQLite.

    Survives restarts, bounds growth per partition so one stuck sink cannot
    fill the disk, and ages rows out after the retention window.
    """

    def __init__(
        self,
        db_path: str | Path,
        retention_days: int = 30,
        max_pending_per_partition: int = 100_000,
    ):
        self.db_path = Path(db_path)
        self.retention_days = retention_days
        self.max_pending_per_partition = max(1, int(max_pending_per_partition))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()
        # v2 Track B: dedicated purge thread so retention enforcement keeps
        # running even when the publish loop is wedged on a hung MQTT broker.
        self._purge_thread: Optional[threading.Thread] = None
        self._purge_stop = threading.Event()

    def start_maintenance(self, interval_sec: float = 3600.0) -> None:
        """Spawn a daemon thread that runs purge_old() on a fixed interval.
        Idempotent. Call once at service startup; stop_maintenance() unsets
        it cleanly on shutdown."""
        if self._purge_thread is not None and self._purge_thread.is_alive():
            return
        self._purge_stop.clear()

        def loop() -> None:
            while not self._purge_stop.is_set():
                # Wait first so the very-first purge isn't done during the
                # already-busy startup window. Event-based wait makes shutdown
                # responsive (no sleep-then-check-flag race).
                if self._purge_stop.wait(interval_sec):
                    return
                try:
                    purged = self.purge_old()
                    if purged:
                        logger.info(
                            "Outbox maintenance purged %d expired rows from %s",
                            purged, self.db_path.name,
                        )
                except Exception:
                    logger.exception("Outbox purge failed")

        self._purge_thread = threading.Thread(
            target=loop, name=f"OutboxPurge-{self.db_path.name}", daemon=True,
        )
        self._purge_thread.start()

    def stop_maintenance(self, timeout: float = 5.0) -> None:
        self._purge_stop.set()
        if self._purge_thread is not None:
            self._purge_thread.join(timeout=max(0.05, timeout))
            self._purge_thread = None

    def _connect(self) -> sqlite3.Connection:
        """Open one short-lived connection. Callers MUST wrap it in
        ``closing()``.

        ``with sqlite3.connect(...) as conn`` is a *transaction* context
        manager, not a closing one - it commits and leaves the connection
        open. sqlite3.Connection takes part in reference cycles, so an
        unclosed one survives until the cyclic collector runs, and a service
        publishing steadily accumulated well over a hundred live connections
        between collections. Each of those pins the database's -wal and -shm
        files and blocks WAL checkpointing, so the write-ahead log grows
        instead of being truncated. ``closing(...)`` plus the ``, conn``
        transaction scope gives both behaviours, and matches IngressJournal.
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        # The ingress journal marks its dispatch leg complete immediately after
        # enqueue() commits here. FULL is therefore required: with NORMAL a
        # power cut could lose this row after the journal had stopped replaying
        # it, recreating the acknowledged-event loss window one layer later.
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    key TEXT NOT NULL UNIQUE,
                    partition_key TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    available_at REAL NOT NULL,
                    sent_at REAL,
                    last_error TEXT
                )
                """
            )
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(outbox)")
            }
            if "partition_key" not in columns:
                conn.execute(
                    "ALTER TABLE outbox ADD COLUMN partition_key TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_pending "
                "ON outbox(status, available_at, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_partition_pending "
                "ON outbox(partition_key, status, id)"
            )

    def enqueue(
        self,
        topic: str,
        payload: Dict[str, Any],
        key: str,
        partition_key: str = "",
    ) -> None:
        now = time.time()
        payload_json = json.dumps(payload, sort_keys=True, default=str)
        with self._lock, closing(self._connect()) as conn, conn:
            if conn.execute(
                "SELECT 1 FROM outbox WHERE key=?", (key,)
            ).fetchone() is not None:
                return
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM outbox "
                "WHERE partition_key=? AND status='pending'",
                (partition_key,),
            ).fetchone()
            if int(pending["n"]) >= self.max_pending_per_partition:
                raise OutboxFullError(
                    f"outbox partition {partition_key!r} reached "
                    f"{self.max_pending_per_partition} pending rows"
                )
            conn.execute(
                """
                INSERT OR IGNORE INTO outbox
                (topic, payload_json, key, partition_key, status, attempts,
                 created_at, available_at)
                VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (topic, payload_json, key, partition_key, now, now),
            )

    def pending(self, limit: int = 100) -> List[OutboxItem]:
        """All currently available rows, for diagnostics and compatibility."""
        return self._pending(limit, heads_only=False)

    def pending_heads(self, limit: int = 100) -> List[OutboxItem]:
        """Only the oldest unresolved row in each machine partition."""
        return self._pending(limit, heads_only=True)

    def _pending(self, limit: int, heads_only: bool) -> List[OutboxItem]:
        now = time.time()
        head_filter = ""
        if heads_only:
            head_filter = """
                  AND NOT EXISTS (
                      SELECT 1 FROM outbox AS earlier
                      WHERE earlier.partition_key = current.partition_key
                        AND earlier.status = 'pending'
                        AND earlier.id < current.id
                  )
            """
        # ``head_filter`` is one of the two literals above, never caller data.
        query = f"""
                SELECT current.id, current.topic, current.payload_json,
                       current.attempts, current.key, current.partition_key
                FROM outbox AS current
                WHERE current.status = 'pending'
                  AND current.available_at <= ?
                  {head_filter}
                ORDER BY current.id
                LIMIT ?
                """  # nosec B608
        with self._lock, closing(self._connect()) as conn, conn:
            rows = conn.execute(
                query,
                (now, limit),
            ).fetchall()
        return [
            OutboxItem(
                id=int(row["id"]),
                topic=str(row["topic"]),
                payload=json.loads(str(row["payload_json"])),
                attempts=int(row["attempts"]),
                key=str(row["key"]),
                partition_key=str(row["partition_key"]),
            )
            for row in rows
        ]

    def mark_sent(self, item_id: int) -> None:
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE outbox SET status='sent', sent_at=? WHERE id=?",
                (time.time(), item_id),
            )

    def mark_failed(self, item_id: int, error: str) -> None:
        with self._lock, closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT attempts FROM outbox WHERE id=?",
                (item_id,),
            ).fetchone()
            attempts = int(row["attempts"]) + 1 if row else 1
            # Exponential backoff capped at 300s. The exponent must reach past
            # 300 for the cap to bind: min(attempts, 9) -> 512 -> 300. With a
            # cap of 8 the maximum was 256 and the `min(300, ...)` was dead.
            delay = min(300, 2 ** min(attempts, 9))
            conn.execute(
                """
                UPDATE outbox
                SET attempts=?, available_at=?, last_error=?
                WHERE id=?
                """,
                (attempts, time.time() + delay, error[:1000], item_id),
            )

    def attempts(self, item_id: int) -> int:
        """Current failure count for one row (0 when unknown)."""
        with self._lock, closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT attempts FROM outbox WHERE id=?", (int(item_id),)
            ).fetchone()
        return int(row["attempts"]) if row else 0

    def mark_dead(self, item_id: int, error: str) -> None:
        """Mark an item as permanently undeliverable (e.g. a 4xx bad-token
        response). Status 'dead' is excluded from pending() so it is never
        retried, but the row is kept for diagnostics until retention purges it."""
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE outbox SET status='dead', last_error=?, sent_at=? WHERE id=?",
                (error[:1000], time.time(), item_id),
            )

    def requeue_dead(self, limit: int = 1000) -> int:
        """Operator recovery path after a token/route/payload correction."""
        with self._lock, closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT id FROM outbox WHERE status='dead' ORDER BY id LIMIT ?",
                (int(limit),),
            ).fetchall()
            conn.executemany(
                "UPDATE outbox SET status='pending', available_at=?, "
                "last_error=NULL, sent_at=NULL WHERE id=?",
                [(time.time(), int(row["id"])) for row in rows],
            )
        return len(rows)

    def purge_old(self) -> int:
        cutoff = time.time() - (self.retention_days * 86400)
        with self._lock, closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "DELETE FROM outbox WHERE status IN ('sent','dead') AND sent_at < ?",
                (cutoff,),
            )
            return int(cur.rowcount)

    def stats(self) -> Dict[str, int]:
        with self._lock, closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM outbox GROUP BY status"
            ).fetchall()
        result = {"pending": 0, "sent": 0, "dead": 0}
        for row in rows:
            result[str(row["status"])] = int(row["count"])
        return result

    def integrity_check(self) -> bool:
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row and str(row[0]).lower() == "ok")

    def database_size_bytes(self) -> int:
        return sum(
            path.stat().st_size
            for path in (
                self.db_path,
                Path(f"{self.db_path}-wal"),
                Path(f"{self.db_path}-shm"),
            )
            if path.exists()
        )
