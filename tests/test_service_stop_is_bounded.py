"""Shutdown must finish on a budget, and free the tool either way.

Two faults this file guards against.

First, stop() used to spend a fixed 10s join on each of ~7 workers per
machine, all sequential, so a four-machine service needed 209s to stop even
when nothing was wrong. The control panel re-enables "Run service here" only
in the `finally` after stop() returns, so the button sat dead for three and a
half minutes and looked broken.

Second, GatewayHost.retire() called secsgem's disable() with no timeout.
secsgem 0.3.0's passive-mode shutdown can block indefinitely - a known
upstream defect, filtered by name in pyproject.toml - and every line after
that call is the fallback for exactly that case, so a wedged disable() made
the recovery unreachable and hung stop() forever.
"""

from __future__ import annotations

import threading
import time

import pytest

from eap_middleware.service import STOP_TIMEOUT_SEC, EapMiddlewareService


# ----- the shared budget -----

def test_budget_is_shared_not_spent_per_join():
    """Each join gets what is LEFT of the deadline, never a fresh timeout."""
    deadline = time.monotonic() + 10.0
    assert EapMiddlewareService._budget(deadline, 10.0) == pytest.approx(10.0, abs=0.2)

    spent = time.monotonic() + 3.0          # 7s of the budget already gone
    assert EapMiddlewareService._budget(spent, 10.0) == pytest.approx(3.0, abs=0.2)


def test_budget_never_returns_zero_or_negative():
    """An exhausted budget still joins briefly.

    A 0-second join is a no-op that skips even an already-finished thread,
    which throws away the ordering guarantee the join exists for.
    """
    assert EapMiddlewareService._budget(time.monotonic() - 60.0, 10.0) > 0.0


def test_budget_without_a_deadline_falls_back_to_the_cap():
    """Direct callers that pass no deadline keep the original per-join cap."""
    assert EapMiddlewareService._budget(None, 7.5) == 7.5


def test_join_within_returns_promptly_when_the_worker_outlives_the_budget(caplog):
    """A stuck worker costs the budget, not forever - and is named in the log."""
    release = threading.Event()
    stuck = threading.Thread(target=release.wait, daemon=True)
    stuck.start()
    try:
        service = EapMiddlewareService.__new__(EapMiddlewareService)
        started = time.monotonic()
        with caplog.at_level("WARNING"):
            service._join_within(
                stuck, time.monotonic() + 0.2, 10.0, "Wedged worker"
            )
        assert time.monotonic() - started < 3.0
        assert "Wedged worker did not stop" in caplog.text
    finally:
        release.set()
        stuck.join(timeout=5)


def test_total_budget_is_bounded_for_a_realistic_machine_count():
    """The whole point: N machines cost one budget, not N x per-join."""
    deadline = time.monotonic() + STOP_TIMEOUT_SEC
    # Four machines x the joins each one makes, all drawing on one deadline.
    total = sum(
        EapMiddlewareService._budget(deadline, cap)
        for _machine in range(4)
        for cap in (10.0, 10.0, 10.0, 10.0, 5.0)
    )
    # Before the shared deadline this summed to 4 x 45 = 180s of timeout.
    # It is now capped by the deadline no matter how many machines there are.
    assert total <= 4 * 5 * STOP_TIMEOUT_SEC
    assert EapMiddlewareService._budget(deadline, 10.0) <= STOP_TIMEOUT_SEC


# ----- retire() and secsgem -----
#
# There is deliberately no test here bounding GatewayHost.retire().
#
# An earlier revision ran secsgem's disable() on a worker thread with a
# timeout, so a wedged disable() could not hang stop(). That let
# _force_close_socket() run while disable() was still tearing the connection
# down, and secsgem 0.3.0 keeps module-level state that is not safe for it:
# across a full suite run the lingering workers deadlocked each other and
# pytest froze at 0% CPU partway through the MG loopback tests. The file
# passed in isolation, which is what made it worth recording here.
#
# retire() is therefore synchronous again. A wedged secsgem disable() can
# still block one machine's teardown; the service-level budget below caps
# everything else, which is what actually fixed the reported symptom.

# ----- the worker stop() methods take the budget -----

@pytest.mark.parametrize(
    ("module", "cls"),
    [
        ("eap_middleware.linkstuffs", "LinkstuffsGatewayPublisher"),
        ("eap_middleware.linkstuffs_http", "LinkstuffsHttpPublisher"),
        ("eap_middleware.legacy_api", "LegacyApiPublisher"),
        ("eap_middleware.outbox", "SQLiteOutbox"),
    ],
)
def test_worker_stop_accepts_a_timeout(module, cls):
    """Every worker stop() the service calls must accept the shared budget."""
    import importlib
    import inspect

    target = getattr(importlib.import_module(module), cls)
    name = "stop_maintenance" if cls == "SQLiteOutbox" else "stop"
    parameters = inspect.signature(getattr(target, name)).parameters
    assert "timeout" in parameters, f"{cls}.{name}() ignores the stop budget"


# ----- the provisioning worker must not outlive its session -----

def test_provisioning_worker_is_started_under_the_lock_that_stop_snapshots():
    """stop() must never miss a worker, or join one that has not begun.

    _on_connect appends the worker to _provision_threads under the session
    lock, and stop() snapshots that list under the same lock and joins what it
    finds. While start() sat *outside* the lock there was a window between the
    append and the start in which stop() could take its snapshot: it then
    either joined a thread that had not begun (RuntimeError) or returned while
    the worker started a moment later and outlived the session, issuing SECS
    round-trips against a connection already torn down. Under a loaded machine
    that window is wide enough to hit - it surfaced as
    "provisioning threads outlived the session: ['Provision-TOOL_04']".
    """
    import inspect

    from eap_middleware import secs_runtime

    lines = inspect.getsource(
        secs_runtime.SecsMachineSession._on_connect
    ).splitlines()

    def _indent(predicate) -> int:
        line = next(text for text in lines if predicate(text))
        return len(line) - len(line.lstrip())

    lock_indent = _indent(lambda text: "with self._lock:" in text)
    start_indent = _indent(lambda text: "worker.start()" in text)
    assert start_indent > lock_indent, (
        "worker.start() must run inside the `with self._lock` block so stop() "
        "cannot snapshot the thread list between the append and the start "
        f"(lock at column {lock_indent}, start at column {start_indent})"
    )


def test_a_started_worker_is_always_visible_to_stop():
    """Behavioural counterpart: every started worker is in the join list."""
    from eap_middleware.models import MachineConfig
    from eap_middleware.secs_runtime import SecsMachineSession

    machine = MachineConfig(
        endpoint_id="TOOL_RACE", display_name="RACE",
        machine_profile="nexgen_mg_series", host="127.0.0.1", port=1,
        event_subscription_enabled=False,
    )
    session = SecsMachineSession(
        machine=machine,
        event_callback=lambda *a: None, alarm_callback=lambda *a: None,
        connect_callback=lambda *a: None, disconnect_callback=lambda *a: None,
    )
    session._stopped = False
    session._epoch = 1
    session.host = None

    session._on_connect(1)
    tracked = list(session._provision_threads)
    assert len(tracked) == 1
    # Started, therefore joinable - join() on an unstarted thread raises.
    tracked[0].join(timeout=5)
    assert not tracked[0].is_alive()


# ----- a sick file share must not stall the SECS acknowledgement -----

def test_writing_a_lot_file_does_not_copy_to_the_network_on_this_thread(tmp_path):
    """_write_buffer runs inside the S6F11 acknowledgement path.

    The tool holds the transaction open waiting for S6F12, and its T3 reply
    timeout is 30-45s depending on the profile. A copy to an unreachable SMB
    share blocks for the OS timeout - comfortably longer than that on Windows
    - so copying inline let a sick file share push the *equipment* into
    declaring a communications failure. The copy belongs to CsvMirrorWorker,
    which has a durable queue and backoff; the local file is already fsynced
    and the journal is what carries the no-loss guarantee.
    """
    import threading

    from eap_middleware.csv_store import PerLotCsvWriter
    from eap_middleware.journal import IngressJournal
    from eap_middleware.models import CanonicalEvent, MachineConfig, utc_now
    from eap_middleware.profiles import ProfileRegistry

    journal = IngressJournal(tmp_path / "j.sqlite3")
    writer = PerLotCsvWriter(journal=journal)
    copied: list = []
    writer._copy_atomic = lambda src, dst: copied.append((src, dst))
    writer._mirror_wake = threading.Event()

    machine = MachineConfig(
        endpoint_id="TOOL_MIRROR", display_name="MIRROR_TEST",
        machine_profile="nexgen_mg_series", host="127.0.0.1", port=1,
        local_csv_path=str(tmp_path / "local"),
        network_csv_path=str(tmp_path / "share"),
    )
    profile = ProfileRegistry().get("nexgen_mg_series")

    def event(kind: str) -> CanonicalEvent:
        return CanonicalEvent(
            timestamp=utc_now(), endpoint_id=machine.endpoint_id,
            display_name=machine.display_name,
            machine_profile=machine.machine_profile,
            vendor=profile.vendor, model=profile.model,
            event_type=kind, raw_event_name=kind.upper(),
            lot_id="LOT_MIRROR", raw_payload={},
        )

    writer.append(machine, profile, event("lot_start"))
    written = writer.flush_all(reason="test")

    assert written, "no local lot file was written"
    assert written[0].exists(), "the local CSV must be on disk before we return"
    assert copied == [], (
        "the network copy ran on the acknowledgement thread; a dead share "
        "would stall S6F12 past T3 and the tool would drop the link"
    )
    assert journal.stats()["mirrors_pending"] == 1, (
        "the mirror must be durably queued for CsvMirrorWorker, not dropped"
    )
    assert writer._mirror_wake.is_set(), (
        "the worker should be woken so deferring costs latency, not a poll "
        "interval"
    )

    # And the worker still performs the copy.
    assert writer.retry_mirrors() == 1
    assert len(copied) == 1
