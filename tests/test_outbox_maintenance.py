"""Outbox purge thread tests (v2 Track B)."""

from __future__ import annotations

import time

from eap_middleware.outbox import SQLiteOutbox


def test_start_maintenance_purges_on_schedule(tmp_path, monkeypatch):
    """The maintenance thread eventually calls purge_old() on the configured
    interval, independent of any publish loop."""
    db = tmp_path / "ob.sqlite3"
    outbox = SQLiteOutbox(db, retention_days=0)
    outbox.enqueue("t", {"x": 1}, key="k1")

    purge_calls = []
    original = outbox.purge_old

    def tracking_purge() -> int:
        purge_calls.append(time.time())
        return original()

    monkeypatch.setattr(outbox, "purge_old", tracking_purge)

    outbox.start_maintenance(interval_sec=0.2)
    try:
        # Wait up to 1.5s for at least one purge cycle
        deadline = time.time() + 1.5
        while time.time() < deadline and not purge_calls:
            time.sleep(0.05)
        assert purge_calls, "purge thread should have fired at least once"
    finally:
        outbox.stop_maintenance()


def test_stop_maintenance_is_responsive(tmp_path):
    """stop_maintenance() must return promptly, not wait out the interval."""
    outbox = SQLiteOutbox(tmp_path / "ob.sqlite3", retention_days=30)
    outbox.start_maintenance(interval_sec=3600.0)
    start = time.time()
    outbox.stop_maintenance()
    elapsed = time.time() - start
    assert elapsed < 1.0, f"stop_maintenance took {elapsed:.2f}s; should be <1s"


def test_start_maintenance_is_idempotent(tmp_path):
    outbox = SQLiteOutbox(tmp_path / "ob.sqlite3")
    outbox.start_maintenance(interval_sec=60.0)
    first = outbox._purge_thread
    outbox.start_maintenance(interval_sec=60.0)
    assert outbox._purge_thread is first
    outbox.stop_maintenance()


def test_purge_thread_survives_one_purge_exception(tmp_path, monkeypatch):
    """If purge_old() raises once, the thread logs and keeps running for the
    next interval. A transient SQLite lock shouldn't kill the maintenance."""
    outbox = SQLiteOutbox(tmp_path / "ob.sqlite3")
    call_count = {"n": 0}

    def flaky_purge() -> int:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient")
        return 0

    monkeypatch.setattr(outbox, "purge_old", flaky_purge)
    outbox.start_maintenance(interval_sec=0.1)
    try:
        deadline = time.time() + 1.0
        while time.time() < deadline and call_count["n"] < 2:
            time.sleep(0.05)
        assert call_count["n"] >= 2, "thread should have retried after exception"
    finally:
        outbox.stop_maintenance()


def _live_sqlite_connections() -> int:
    """Connections the interpreter is still holding open, without collecting.

    Deliberately does NOT call gc.collect() first: the point is what the
    process holds *between* collections, which is what pins a WAL database's
    -wal/-shm files and blocks checkpointing.
    """
    import gc
    import sqlite3

    return sum(1 for obj in gc.get_objects() if isinstance(obj, sqlite3.Connection))


def test_outbox_does_not_accumulate_open_connections(tmp_path):
    """Every operation must close its connection before returning.

    `with sqlite3.connect(...) as conn` is a *transaction* context manager,
    not a closing one - it commits and leaves the handle open. Connections
    take part in reference cycles, so the ones this class opened survived
    until the cyclic collector ran; a steadily publishing service held well
    over a hundred at a time. Each one pins the database's -wal and -shm
    files and blocks WAL checkpointing, so the write-ahead log grows instead
    of being truncated. IngressJournal has always used contextlib.closing;
    this pins that the outbox does too.
    """
    outbox = SQLiteOutbox(tmp_path / "ob.sqlite3")
    baseline = _live_sqlite_connections()

    for index in range(400):
        outbox.enqueue("t/x", {"i": index}, key=f"k{index}", partition_key="p")
    for _ in range(50):
        outbox.stats()
        outbox.pending(limit=1)
        outbox.pending_heads(limit=1)
        outbox.attempts(1)
    outbox.mark_failed(1, "transient")
    outbox.mark_sent(2)
    outbox.mark_dead(3, "bad token")
    outbox.requeue_dead()
    outbox.purge_old()

    assert _live_sqlite_connections() <= baseline
