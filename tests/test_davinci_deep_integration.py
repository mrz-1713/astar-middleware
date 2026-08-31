"""Multi-DaVinci concurrent HTTPS publish to Linkstuffs — integration tests.

Full pipeline: SECS/GEM event → mapper → outbox → HTTP publisher → Linkstuffs.

Edge cases not covered by unit tests:
  1. Two machines burst simultaneously → one publisher → all events arrive
  2. Two machines → same Linkstuffs device token (fleet-merge)
  3. Connect/disconnect health events reach the HTTP endpoint
  4. EapMiddlewareService wiring: _on_secs_event → HTTP publisher
  5. 429 rate-limit: event stays in outbox, not dropped
  6. Alarm storm on one machine doesn't starve the other machine's HTTP path
  7. Reconnect watchdog publishes health events
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

from eap_middleware.alarms import AlarmRateLimiter
from eap_middleware.job_tracker import JobTracker
from eap_middleware.linkstuffs_http import LinkstuffsHttpPublisher
from eap_middleware.mapper import CanonicalMapper
from eap_middleware.models import (
    CanonicalEvent,
    LegacyApiConfig,
    LinkstuffsConfig,
    LinkstuffsHttpConfig,
    MachineConfig,
    MiddlewarePaths,
    ServiceConfig,
)
from eap_middleware.outbox import SQLiteOutbox
from eap_middleware.profiles import ProfileRegistry


# ── helpers ───────────────────────────────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode() if n else ""
        status, hold = self.server.behavior({"path": self.path, "body": body})  # type: ignore[attr-defined]
        self.server.captured.append({"path": self.path, "body": body})          # type: ignore[attr-defined]
        if hold:
            time.sleep(hold)
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a, **k):
        pass


def _start_server(
    behavior: Optional[Callable[[Dict[str, Any]], Tuple[int, float]]] = None,
) -> Tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.captured = []                                            # type: ignore[attr-defined]
    srv.behavior = behavior or (lambda r: (200, 0.0))           # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _wait(pred: Callable[[], bool], timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _machine(n: int, display: Optional[str] = None, tmp_path=None) -> MachineConfig:
    return MachineConfig(
        endpoint_id=f"TOOL_{n:02d}",
        display_name=display or f"DAV_{n:02d}",
        machine_profile="davinci_200_mc4_hc1",
        host=f"10.10.20.{n}",
        port=5000,
        local_csv_path=str(tmp_path / f"csv_{n}") if tmp_path else None,
        admin_config_path=str(tmp_path / f"admin_{n}") if tmp_path else None,
        svid_collection_enabled=False,
    )


def _unique_events(machine: MachineConfig, count: int = 5) -> List[CanonicalEvent]:
    """Distinct lot IDs → distinct outbox keys → no dedup between calls."""
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    mapper = CanonicalMapper(profile, tracker=JobTracker())
    mapper.from_secs_event(machine, 3050001, {"_v_raw": [1]})  # LP1 activate
    return [
        mapper.from_secs_event(
            machine, 3140002,
            {"_v_raw": ["W001", f"LOT-{machine.endpoint_id}-{i:04d}", "Recipe_X"]},
        )
        for i in range(count)
    ]


def _http_pub(tmp_path, base_url: str, tokens: Dict[str, str], db="ob.sqlite3") -> LinkstuffsHttpPublisher:
    return LinkstuffsHttpPublisher(
        LinkstuffsHttpConfig(enabled=True, base_url=base_url, device_tokens=tokens),
        SQLiteOutbox(tmp_path / db),
    )


def _service_cfg(tmp_path, machine, base_url, tokens, reconnect_sec=10.0) -> ServiceConfig:
    return ServiceConfig(
        machines=[machine],
        linkstuffs=LinkstuffsConfig(enabled=False),
        linkstuffs_http=LinkstuffsHttpConfig(
            enabled=True, base_url=base_url, device_tokens=tokens,
        ),
        legacy_api=LegacyApiConfig(enabled=False),
        paths=MiddlewarePaths(
            install_dir=str(tmp_path / "install"),
            outbox_db=str(tmp_path / "out.sqlite3"),
            legacy_api_outbox_db=str(tmp_path / "legacy.sqlite3"),
            http_outbox_db=str(tmp_path / "http.sqlite3"),
        ),
        reconnect_interval_sec=reconnect_sec,
        startup_stagger_sec=0.0,
    )


# ── 1. Concurrent burst: 2 machines → 1 publisher → all arrive ───────────────


def test_two_machines_concurrent_burst_all_delivered(tmp_path):
    """Two threads queue 10 events each into the same publisher simultaneously.
    SQLite lock contention must not drop any event; 20 POSTs reach the server,
    10 per token path."""
    server, base_url = _start_server()
    mA, mB = _machine(1, tmp_path=tmp_path), _machine(2, tmp_path=tmp_path)
    pub = _http_pub(tmp_path, base_url, {mA.display_name: "TOK-A", mB.display_name: "TOK-B"})
    EACH = 10
    errors: List[Exception] = []

    def fire(machine: MachineConfig) -> None:
        try:
            for ev in _unique_events(machine, EACH):
                pub.queue_event(ev)
        except Exception as e:
            errors.append(e)

    try:
        pub.start()
        tA = threading.Thread(target=fire, args=(mA,))
        tB = threading.Thread(target=fire, args=(mB,))
        tA.start(); tB.start()
        tA.join(); tB.join()
        assert not errors, errors
        assert _wait(lambda: len(server.captured) >= EACH * 2, timeout=10.0), (
            f"Expected {EACH*2} POSTs, got {len(server.captured)} after 10s"
        )
    finally:
        pub.stop()
        server.shutdown()

    paths = [r["path"] for r in server.captured]
    assert sum(1 for p in paths if "/TOK-A/" in p) == EACH
    assert sum(1 for p in paths if "/TOK-B/" in p) == EACH
    for rec in server.captured:
        values = json.loads(rec["body"])[0]["values"]
        expected_id = mA.endpoint_id if "/TOK-A/" in rec["path"] else mB.endpoint_id
        assert values["endpoint_id"] == expected_id


# ── 2. Two machines → same Linkstuffs device (fleet-merge) ───────────────────


def test_two_machines_merged_to_same_device(tmp_path):
    """Two DaVinci tools mapped to one Linkstuffs device token (fleet dashboard).
    Both events must reach the same URL; endpoint_id in each payload tells them apart."""
    server, base_url = _start_server()
    mA = _machine(1, display="LINE1_DAV01", tmp_path=tmp_path)
    mB = _machine(2, display="LINE1_DAV02", tmp_path=tmp_path)
    FLEET_TOKEN = "FLEET-TOK-LINE1"
    pub = _http_pub(tmp_path, base_url, {mA.display_name: FLEET_TOKEN, mB.display_name: FLEET_TOKEN})

    try:
        pub.start()
        pub.queue_event(_unique_events(mA, 1)[0])
        pub.queue_event(_unique_events(mB, 1)[0])
        assert _wait(lambda: len(server.captured) >= 2, timeout=5.0), (
            f"Expected 2 POSTs, got {len(server.captured)}"
        )
    finally:
        pub.stop()
        server.shutdown()

    paths = [r["path"] for r in server.captured]
    assert all(f"/api/v1/{FLEET_TOKEN}/telemetry" == p for p in paths), paths
    endpoint_ids = {json.loads(r["body"])[0]["values"]["endpoint_id"] for r in server.captured}
    assert endpoint_ids == {mA.endpoint_id, mB.endpoint_id}


# ── 3. Connect/disconnect health events reach the HTTP endpoint ───────────────


def test_connect_disconnect_health_events_published(tmp_path):
    """_on_connect/_on_disconnect fire connection_state events through the HTTP
    publisher. Verifies Phase 1 of the pipeline without a real HSMS peer."""
    server, base_url = _start_server()
    machine = _machine(1, tmp_path=tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile, tracker=JobTracker())
    pub = _http_pub(tmp_path, base_url, {machine.display_name: "TOK-HEALTH"})

    try:
        pub.start()
        pub.queue_event(mapper.connection_event(machine, "connected"))
        pub.queue_event(mapper.connection_event(machine, "disconnected"))
        assert _wait(lambda: len(server.captured) >= 2, timeout=5.0), (
            f"Expected 2 health POSTs, got {len(server.captured)}"
        )
    finally:
        pub.stop()
        server.shutdown()

    bodies = [json.loads(r["body"]) for r in server.captured]
    raw_names = {b[0]["values"]["raw_event_name"] for b in bodies}
    assert raw_names == {"connected", "disconnected"}
    assert all(b[0]["values"]["event_type"] == "connection_state" for b in bodies)
    assert all(b[0]["values"]["endpoint_id"] == machine.endpoint_id for b in bodies)


# ── 4. EapMiddlewareService wiring: SECS event → HTTP publisher ───────────────


@patch("eap_middleware.service.lifecycle.SecsMachineSession")
def test_service_secs_event_reaches_http_publisher(MockSession, tmp_path):
    """EapMiddlewareService._on_secs_event → mapper → HTTP publisher → Linkstuffs.
    Mock session so no real HSMS peer is needed."""
    server, base_url = _start_server()
    machine = _machine(1, tmp_path=tmp_path)

    mock_session = MagicMock()
    mock_session.start.return_value = None
    mock_session.stop.return_value = None
    MockSession.return_value = mock_session

    from eap_middleware.service import EapMiddlewareService
    svc = EapMiddlewareService(_service_cfg(tmp_path, machine, base_url, {machine.display_name: "TOK-SVC"}))
    try:
        svc.start()
        svc._on_secs_event(
            machine, 3140002,
            {"DATETIME": "20251128094700", "_v_raw": ["W001", "LOT_SVC_TEST", "Recipe_Y"]},
        )
        assert _wait(lambda: any("/telemetry" in r["path"] for r in server.captured), timeout=6.0), (
            f"SECS event did not reach Linkstuffs HTTP endpoint; paths: {[r['path'] for r in server.captured]}"
        )
    finally:
        svc.stop()
        server.shutdown()

    # service.start() also queues attributes; filter to telemetry only
    telemetry = [r for r in server.captured if "/telemetry" in r["path"]]
    values = json.loads(telemetry[0]["body"])[0]["values"]
    assert values["event_type"] == "process_start"
    assert values["lot_id"] == "LOT_SVC_TEST"
    assert values["endpoint_id"] == machine.endpoint_id


# ── 5. 429 rate-limit: event stays in outbox ─────────────────────────────────


def test_429_rate_limit_not_silently_dropped(tmp_path):
    """Linkstuffs 429 consumes the bounded retry budget and remains queued."""
    calls: List[int] = []

    server, base_url = _start_server(behavior=lambda r: (calls.append(1) or (429, 0.0)))  # type: ignore[func-returns-value]
    machine = _machine(1, tmp_path=tmp_path)
    outbox = SQLiteOutbox(tmp_path / "ob.sqlite3")
    pub = LinkstuffsHttpPublisher(
        LinkstuffsHttpConfig(
            enabled=True, base_url=base_url,
            device_tokens={machine.display_name: "TOK-429"},
            retry_count=3,
            retry_delay_sec=0.05,
        ),
        outbox,
    )

    event = CanonicalMapper(ProfileRegistry().get("davinci_200_mc4_hc1")).from_secs_event(
        machine, 3140002, {"_v_raw": ["W001", "LOT-429-TEST", "Recipe_X"]},
    )

    try:
        pub.start()
        pub.queue_event(event)
        _wait(lambda: len(calls) >= 1, timeout=3.0)
        time.sleep(0.3)  # let the publish loop finish processing the response
    finally:
        pub.stop()
        server.shutdown()

    assert len(calls) == 4
    # Event is retained in the outbox (not dropped, not marked sent)
    assert sum(outbox.stats().values()) >= 1, "429 silently dropped the event from the outbox"


# ── 6. Alarm storm on A doesn't starve B's HTTP path ─────────────────────────


def test_alarm_storm_on_one_machine_doesnt_starve_another(tmp_path):
    """AlarmRateLimiter is per-machine. Machine A's storm (50 alarms, 3 admitted)
    must not delay machine B's 5 regular events."""
    server, base_url = _start_server()
    mA = _machine(1, display="STORM_MACHINE", tmp_path=tmp_path)
    mB = _machine(2, display="QUIET_MACHINE", tmp_path=tmp_path)
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    limiter = AlarmRateLimiter(max_per_window=3, window_sec=1.0)
    mapperA = CanonicalMapper(profile, tracker=JobTracker())
    mapperB = CanonicalMapper(profile, tracker=JobTracker())
    pub = _http_pub(tmp_path, base_url, {mA.display_name: "TOK-STORM", mB.display_name: "TOK-QUIET"})

    try:
        pub.start()
        admitted_a = 0
        for i in range(50):
            if limiter.admit(mA.endpoint_id):
                pub.queue_event(mapperA.alarm_event(mA, {
                    "alid": 5010001 + i, "altx": "Storm", "is_set": True,
                }))
                admitted_a += 1

        mapperB.from_secs_event(mB, 3050001, {"_v_raw": [1]})
        for ev in _unique_events(mB, 5):
            pub.queue_event(ev)

        assert _wait(lambda: len(server.captured) >= admitted_a + 5, timeout=10.0), (
            f"Expected {admitted_a + 5} POSTs, got {len(server.captured)}"
        )
    finally:
        pub.stop()
        server.shutdown()

    paths = [r["path"] for r in server.captured]
    assert len([p for p in paths if "/TOK-STORM/" in p]) == 3   # rate-limited to 3
    assert len([p for p in paths if "/TOK-QUIET/" in p]) == 5   # unaffected


# ── 7. Reconnect watchdog publishes health events ─────────────────────────────


@patch("eap_middleware.service.lifecycle.SecsMachineSession")
def test_reconnect_health_event_fires_after_session_restart(MockSession, tmp_path):
    """Watchdog restarts a disconnected session and fires reconnect_attempted
    health event through the HTTP publisher — visible in Linkstuffs without
    reading server logs."""
    server, base_url = _start_server()
    machine = _machine(1, tmp_path=tmp_path)

    mock_session = MagicMock()
    mock_session.start.return_value = None
    mock_session.stop.return_value = None
    mock_session.host.is_connected = False   # always disconnected → watchdog keeps firing
    MockSession.return_value = mock_session

    from eap_middleware.service import EapMiddlewareService
    svc = EapMiddlewareService(_service_cfg(
        tmp_path, machine, base_url,
        {machine.display_name: "TOK-RECON"},
        reconnect_sec=0.5,
    ))

    def _has_reconnect():
        for r in server.captured:
            if "/telemetry" not in r["path"] or not r["body"]:
                continue
            try:
                if "reconnect" in json.loads(r["body"])[0]["values"].get("raw_event_name", ""):
                    return True
            except (KeyError, IndexError, json.JSONDecodeError):
                pass
        return False

    try:
        svc.start()
        assert _wait(_has_reconnect, timeout=5.0), (
            f"Watchdog did not publish reconnect event; paths: {[r['path'] for r in server.captured]}"
        )
    finally:
        svc.stop()
        server.shutdown()

    reconnect = next(
        json.loads(r["body"])[0]["values"]
        for r in server.captured
        if "/telemetry" in r["path"] and r["body"]
        and "reconnect" in json.loads(r["body"])[0]["values"].get("raw_event_name", "")
    )
    assert reconnect["event_type"] == "connection_state"
    assert reconnect["endpoint_id"] == machine.endpoint_id
