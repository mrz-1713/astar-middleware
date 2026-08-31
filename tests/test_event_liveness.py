"""Event-liveness watchdog: detect an acked-but-silent subscription.

The DaVinci HostInterface can be configured for E40 event-report style, in which
collection events are delivered on Stream 16 instead of S6F11. The middleware
only consumes S6F11, so in E40 mode it connects, subscribes (DRACK/LRACK/ERACK
all 0), and then receives ZERO telemetry while looking perfectly healthy.

The service polls the tool's LastEventID status variable (which advances on
every internal collection event regardless of report delivery) and raises a
loud `no_event_reports` health alarm when it advances while no S6F11 has been
received. These tests pin that behaviour and its idle false-positive safety.
"""

from __future__ import annotations

import threading
from typing import Dict, List

from eap_middleware.models import (
    LegacyApiConfig,
    LinkstuffsConfig,
    LinkstuffsHttpConfig,
    MachineConfig,
    MiddlewarePaths,
    ServiceConfig,
)
from eap_middleware.profiles import ProfileRegistry
from eap_middleware.service import EapMiddlewareService, event_liveness_decision


# ── pure decision function ───────────────────────────────────────────────────

def test_decision_idle_tool_never_alarms():
    # LastEventID unchanged (idle) -> no alarm even past grace.
    assert event_liveness_decision(
        baseline=42, current=42, delivered=False,
        seconds_since_connect=10_000, grace=120, alarmed=False,
    ) is None


def test_decision_advancing_but_silent_alarms_after_grace():
    assert event_liveness_decision(
        baseline=42, current=43, delivered=False,
        seconds_since_connect=200, grace=120, alarmed=False,
    ) == "alarm"


def test_decision_within_grace_holds_fire():
    # Subscription provisioning may still be running -> don't alarm yet.
    assert event_liveness_decision(
        baseline=42, current=43, delivered=False,
        seconds_since_connect=30, grace=120, alarmed=False,
    ) is None


def test_decision_delivered_clears_prior_alarm():
    assert event_liveness_decision(
        baseline=42, current=99, delivered=True,
        seconds_since_connect=300, grace=120, alarmed=True,
    ) == "clear"


def test_decision_delivered_when_never_alarmed_is_noop():
    assert event_liveness_decision(
        baseline=42, current=99, delivered=True,
        seconds_since_connect=300, grace=120, alarmed=False,
    ) is None


def test_decision_does_not_double_alarm():
    assert event_liveness_decision(
        baseline=42, current=43, delivered=False,
        seconds_since_connect=300, grace=120, alarmed=True,
    ) is None


def test_decision_waits_for_first_reading():
    assert event_liveness_decision(
        baseline=None, current=None, delivered=False,
        seconds_since_connect=300, grace=120, alarmed=False,
    ) is None


# ── wired into the service ───────────────────────────────────────────────────

class _FakeHost:
    def __init__(self, last_event_time=None):
        self.last_event_time = last_event_time
        self.is_connected = True


class _FakeSession:
    """Stands in for SecsMachineSession: serves canned SVID values and a host."""

    def __init__(self, sv_sequence: List[Dict[int, object]], host: _FakeHost):
        self._seq = sv_sequence
        self._i = 0
        self.host = host

    def request_svids(self, svids: List[int]) -> Dict[int, object]:
        values = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return {s: values.get(s) for s in svids if s in values}


def _service(tmp_path) -> EapMiddlewareService:
    machine = MachineConfig(
        endpoint_id="TOOL_02",
        display_name="DAVINCI200_MC4_HC1_01",
        machine_profile="davinci_200_mc4_hc1",
        host="10.0.0.2", port=5000,
    )
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
        ),
        event_liveness_grace_sec=120.0,
    )
    return EapMiddlewareService(cfg)


def _health_states(svc, machine) -> List[str]:
    """Capture the raw_event_name (state) of every health event published."""
    captured: List[str] = []
    orig = svc._publish_health

    def spy(m, state, details=""):
        captured.append(state)
        return orig(m, state, details)

    svc._publish_health = spy  # type: ignore[assignment]
    return captured


def _ds():
    from eap_middleware.profiles import DAVINCI_SVIDS
    return DAVINCI_SVIDS["LastEventID"], DAVINCI_SVIDS["EventsEnabled"]


def test_service_raises_alarm_when_events_advance_but_no_reports(tmp_path, monkeypatch):
    svc = _service(tmp_path)
    machine = svc.config.machines[0]
    last_ev, ev_en = _ds()
    host = _FakeHost(last_event_time=None)  # no S6F11 ever delivered
    session = _FakeSession(
        [{last_ev: 100, ev_en: True}, {last_ev: 105, ev_en: True}], host,
    )
    states = _health_states(svc, machine)

    # connect at t0; checks happen well past the grace window.
    svc._event_liveness[machine.endpoint_id] = {
        "connect_ts": 0.0, "baseline": None, "alarmed": False,
    }
    import time as time_mod
    monkeypatch.setattr(time_mod, "time", lambda: 1_000.0)

    svc._check_event_liveness(machine, session)   # samples baseline=100
    svc._check_event_liveness(machine, session)   # 105 != 100 -> alarm

    assert "no_event_reports" in states
    assert svc._event_liveness[machine.endpoint_id]["alarmed"] is True
    # Idempotent: a third tick does not re-alarm.
    svc._check_event_liveness(machine, session)
    assert states.count("no_event_reports") == 1


def test_service_stays_quiet_on_idle_tool(tmp_path, monkeypatch):
    svc = _service(tmp_path)
    machine = svc.config.machines[0]
    last_ev, ev_en = _ds()
    host = _FakeHost(last_event_time=None)
    # LastEventID never moves -> genuinely idle, not misconfigured.
    session = _FakeSession([{last_ev: 7, ev_en: True}], host)
    states = _health_states(svc, machine)

    svc._event_liveness[machine.endpoint_id] = {
        "connect_ts": 0.0, "baseline": None, "alarmed": False,
    }
    import time as time_mod
    monkeypatch.setattr(time_mod, "time", lambda: 1_000.0)

    for _ in range(4):
        svc._check_event_liveness(machine, session)

    assert "no_event_reports" not in states


def test_service_detects_offline_tool_not_answering_status(tmp_path, monkeypatch):
    # An OFF-LINE DaVinci answers establish-comm but ignores S1F3, so the poll
    # returns nothing. After the grace window we must raise no_status_response.
    svc = _service(tmp_path)
    machine = svc.config.machines[0]
    host = _FakeHost(last_event_time=None)
    session = _FakeSession([{}], host)  # request_svids returns {} -> current None
    states = _health_states(svc, machine)

    svc._event_liveness[machine.endpoint_id] = {
        "connect_ts": 0.0, "baseline": None, "alarmed": False,
        "offline_alarmed": False, "spool_alarmed": False,
    }
    import time as time_mod
    monkeypatch.setattr(time_mod, "time", lambda: 1_000.0)

    svc._check_event_liveness(machine, session)
    assert "no_status_response" in states
    # Idempotent.
    svc._check_event_liveness(machine, session)
    assert states.count("no_status_response") == 1


def test_service_offline_alarm_does_not_fire_within_grace(tmp_path, monkeypatch):
    svc = _service(tmp_path)
    machine = svc.config.machines[0]
    session = _FakeSession([{}], _FakeHost(last_event_time=None))
    states = _health_states(svc, machine)
    svc._event_liveness[machine.endpoint_id] = {
        "connect_ts": 950.0, "baseline": None, "alarmed": False,
        "offline_alarmed": False, "spool_alarmed": False,
    }
    import time as time_mod
    monkeypatch.setattr(time_mod, "time", lambda: 1_000.0)  # 50s < 120s grace
    svc._check_event_liveness(machine, session)
    assert "no_status_response" not in states


def test_service_detects_pending_spool(tmp_path, monkeypatch):
    from eap_middleware.profiles import DAVINCI_SVIDS
    last_ev, _ = _ds()
    spool_sv = DAVINCI_SVIDS["SpoolCountActual"]
    svc = _service(tmp_path)
    machine = svc.config.machines[0]
    # Tool answers status (online) but reports 7 spooled messages.
    session = _FakeSession([{last_ev: 5, spool_sv: 7}], _FakeHost(last_event_time=None))
    states = _health_states(svc, machine)
    svc._event_liveness[machine.endpoint_id] = {
        "connect_ts": 0.0, "baseline": None, "alarmed": False,
        "offline_alarmed": False, "spool_alarmed": False,
    }
    import time as time_mod
    monkeypatch.setattr(time_mod, "time", lambda: 1_000.0)
    svc._check_event_liveness(machine, session)
    assert "spooled_messages_pending" in states
    assert states.count("spooled_messages_pending") == 1  # deduped


def test_request_online_invoked_only_when_enabled(tmp_path):
    from unittest.mock import MagicMock
    from eap_middleware.secs_runtime import SecsMachineSession

    def _make(req_online: bool):
        machine = MachineConfig(
            endpoint_id="TOOL_02", display_name="D", machine_profile="davinci_200_mc4_hc1",
            host="10.0.0.2", port=5000, request_online=req_online,
            event_subscription_enabled=False, enable_alarms=False,
        )
        sess = SecsMachineSession(
            machine, lambda *a: None, lambda *a: None, lambda *a: None, lambda *a: None,
            subscription_path=None,
        )
        sess.host = MagicMock()
        sess.host.request_online.return_value = True
        # Provisioning only runs for a live connection generation, so that a
        # worker left over from a previous connection cannot reconfigure the
        # session that replaced it. start() opens one; this drives the worker
        # directly, so open one by hand.
        sess._epoch = 1
        sess._stopped = False
        return sess

    on = _make(True)
    on._provision_after_connect(1)
    on.host.request_online.assert_called_once()

    off = _make(False)
    off._provision_after_connect(1)
    off.host.request_online.assert_not_called()


def test_provisioning_stands_down_once_its_connection_is_superseded():
    """A worker from a retired connection must not touch the new session.

    Its SECS round-trips can block for up to T3, so it can still be running
    when the reconnect watchdog has already stopped the session and started a
    fresh one - and a late S1F17/S2F33 then lands on the wrong generation.
    """
    from unittest.mock import MagicMock
    from eap_middleware.secs_runtime import SecsMachineSession

    machine = MachineConfig(
        endpoint_id="TOOL_02", display_name="D",
        machine_profile="davinci_200_mc4_hc1", host="10.0.0.2", port=5000,
        request_online=True, event_subscription_enabled=False,
        enable_alarms=False,
    )
    session = SecsMachineSession(
        machine, lambda *a: None, lambda *a: None, lambda *a: None,
        lambda *a: None, subscription_path=None,
    )
    session.host = MagicMock()
    session._epoch = 2
    session._stopped = False

    session._provision_after_connect(1)  # the superseded generation
    session.host.request_online.assert_not_called()


def test_service_clears_alarm_once_reports_flow(tmp_path, monkeypatch):
    svc = _service(tmp_path)
    machine = svc.config.machines[0]
    last_ev, ev_en = _ds()
    host = _FakeHost(last_event_time=None)
    session = _FakeSession([{last_ev: 1, ev_en: True}, {last_ev: 2, ev_en: True}], host)
    states = _health_states(svc, machine)

    svc._event_liveness[machine.endpoint_id] = {
        "connect_ts": 0.0, "baseline": None, "alarmed": False,
    }
    import time as time_mod
    monkeypatch.setattr(time_mod, "time", lambda: 1_000.0)

    svc._check_event_liveness(machine, session)   # baseline=1
    svc._check_event_liveness(machine, session)   # 2 != 1 -> alarm
    assert "no_event_reports" in states

    # A real S6F11 finally arrives -> next tick clears.
    host.last_event_time = object()
    svc._check_event_liveness(machine, session)
    assert states[-1] == "event_reports_ok"
    assert svc._event_liveness[machine.endpoint_id]["alarmed"] is False


# ── spooled-backlog alert ────────────────────────────────────────────────────

def _health_details(svc):
    """Capture (state, details) for every health event published."""
    captured: List[tuple] = []
    orig = svc._publish_health

    def spy(m, state, details=""):
        captured.append((state, details))
        return orig(m, state, details)

    svc._publish_health = spy  # type: ignore[assignment]
    return captured


def test_spool_alert_points_at_drain_spool_on_connect(tmp_path, monkeypatch):
    """The alert must send the operator to the setting that fixes it.

    It used to say the middleware "does not auto-drain the spool (no S6F23)"
    and tell the operator to "Disable tool-side spooling or drain it manually".
    Both halves were wrong and the advice was actively harmful:
    `GatewayHost.drain_spool()` sends exactly that S6F23, wired to
    `drain_spool_on_connect`, and the spool is what preserved the events in the
    first place - so disabling it turns a recoverable backlog into real loss.

    On a spooling tool nothing but a host S6F23 empties the spool, and a tool
    that refuses to send while a backlog exists then spools everything after
    it, so a stranded backlog can silence a healthy link indefinitely. The
    operator has to be pointed at the flag, not away from it.
    """
    from eap_middleware.profiles import DAVINCI_SVIDS

    svc = _service(tmp_path)
    machine = svc.config.machines[0]
    last_ev, ev_en = _ds()
    spool_sv = DAVINCI_SVIDS["SpoolCountActual"]

    host = _FakeHost(last_event_time=None)
    session = _FakeSession([{last_ev: 100, ev_en: True, spool_sv: 7}], host)
    events = _health_details(svc)

    svc._event_liveness[machine.endpoint_id] = {
        "connect_ts": 0.0, "baseline": None, "alarmed": False,
        "offline_alarmed": False, "spool_alarmed": False,
    }
    import time as time_mod
    monkeypatch.setattr(time_mod, "time", lambda: 1_000.0)

    svc._check_event_liveness(machine, session)

    detail = next(d for state, d in events if state == "spooled_messages_pending")
    assert "drain_spool_on_connect" in detail
    assert "SpoolCountActual=7" in detail
    # The two pieces of harmful advice must not come back.
    assert "does not auto-drain" not in detail
    assert "Disable tool-side spooling" not in detail


def test_nexgen_never_raises_a_spool_alert():
    """The MG manual documents spooling as unsupported (§2.1 "Spooling: No";
    SVIDs 17-20 SpoolCount*/Spool*Time all "Not supported"), so the profile
    carries no spool counter and the watchdog has nothing to poll. Pinned
    because wiring one in would poll a VID the tool answers for with nothing.
    """
    registry = ProfileRegistry()
    assert registry.get("nexgen_mg_series").health_spool_count_svid is None
    # The two tools whose manuals DO document spooling keep theirs.
    for name in ("spts_fxp_omega", "davinci_200_mc4_hc1"):
        assert registry.get(name).health_spool_count_svid is not None


# ── watchdog thread-leak guard ───────────────────────────────────────────────

def test_liveness_check_does_not_spawn_a_second_thread_while_one_is_outstanding(
    tmp_path,
):
    """A connected-but-silent tool (the OFF-LINE trap: `alarmed` never gets
    set because the code sets `offline_alarmed` instead - see
    `_check_event_liveness`) must not accumulate one liveness thread per
    watchdog tick for as long as the outage lasts. Regression for a real
    unbounded-thread-growth bug: ~250-300 threads/hour at the shipped 10s
    watchdog interval against a tool that never answers S1F3 inside T3
    (30-45s across profiles)."""
    svc = _service(tmp_path)
    machine = svc.config.machines[0]
    release = threading.Event()
    started = threading.Event()

    class _BlockingSession:
        host = _FakeHost(last_event_time=None)

        def request_svids(self, svids):
            started.set()
            release.wait(timeout=5.0)
            return {}

    session = _BlockingSession()
    svc._sessions[machine.endpoint_id] = session
    svc._machines_by_endpoint[machine.endpoint_id] = machine
    svc._event_liveness[machine.endpoint_id] = {
        "connect_ts": 0.0, "baseline": None, "alarmed": False,
        "offline_alarmed": False, "spool_alarmed": False,
    }

    try:
        # Tick 1: nothing outstanding yet -> spawns the one check thread.
        svc._maybe_start_liveness_check(machine.endpoint_id, machine, session)
        assert started.wait(timeout=2.0), "liveness thread never started"
        assert machine.endpoint_id in svc._liveness_inflight

        # Ticks 2-5: the S1F3 round-trip is still outstanding (simulating an
        # unresponsive tool held past several watchdog intervals). Before the
        # fix, each of these spawned another thread.
        for _ in range(4):
            svc._maybe_start_liveness_check(machine.endpoint_id, machine, session)
        live_liveness_threads = [
            t for t in threading.enumerate()
            if t.name == f"Liveness-{machine.endpoint_id}"
        ]
        assert len(live_liveness_threads) == 1
    finally:
        release.set()

    # Once the round-trip finally returns, the guard clears so the next
    # outage tick is free to check again.
    for t in threading.enumerate():
        if t.name == f"Liveness-{machine.endpoint_id}":
            t.join(timeout=2.0)
    assert machine.endpoint_id not in svc._liveness_inflight
