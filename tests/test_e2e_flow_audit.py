"""End-to-end flow audit: machine -> middleware -> Linkstuffs (MQTT + HTTPS).

The existing tests cover each layer in isolation. This file pins the
*interactions* between layers - the failure modes that only show up when
you stress the whole stack:

  1. Dual upstream independence (MQTT wedged shouldn't block HTTPS)
  2. Same event flows through both publishers identically
  3. HTTPS oversized payload (5000-die TestResults)
  4. HTTPS verify_tls=false for private-CA platforms
  5. HTTPS slow-then-fast (transient timeout, retry succeeds)
  6. Outbox survives publisher process restart
  7. Outbox dedupe across both publishers (same event_key)
  8. HTTPS publisher tolerates server returning unexpected 5xx burst
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Tuple


from eap_middleware.linkstuffs_http import (
    LinkstuffsHttpPublisher,
)
from eap_middleware.mapper import CanonicalMapper
from eap_middleware.models import (
    LinkstuffsConfig,
    LinkstuffsHttpConfig,
    MachineConfig,
)
from eap_middleware.outbox import SQLiteOutbox
from eap_middleware.profiles import ProfileRegistry

from tests.test_mqtt_loopback import _LoopbackPublisher


# ---------- helpers ----------

class _BehaviorHandler(BaseHTTPRequestHandler):
    """HTTP handler whose behavior is controlled by a callable so each test
    can simulate timeouts, 5xx bursts, large-body acceptance, etc."""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        record = {"path": self.path, "body": body, "size": length}
        self.server.captured.append(record)  # type: ignore[attr-defined]
        status, hold = self.server.behavior(record)  # type: ignore[attr-defined]
        if hold > 0:
            time.sleep(hold)
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args, **kwargs):  # silence
        pass


def _start_behavior_server(
    behavior: Callable[[Dict[str, Any]], Tuple[int, float]] = lambda r: (200, 0.0),
) -> Tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BehaviorHandler)
    server.captured: List[Dict[str, Any]] = []  # type: ignore[attr-defined]
    server.behavior = behavior  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _machine(tmp_path, display="DAVINCI_E2E"):
    return MachineConfig(
        endpoint_id="TOOL_E2E",
        display_name=display,
        machine_profile="davinci_200_mc4_hc1",
        host="127.0.0.1", port=5000,
        local_csv_path=str(tmp_path / "local"),
        admin_config_path=str(tmp_path / "admin"),
    )


def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _process_event(machine, profile):
    return CanonicalMapper(profile).from_secs_event(
        machine, 3140003,  # PM1/ProcessingFinished
        {
            "DATETIME": "20251128094700",
            "_v_raw": ["W001", "LOT_AUDIT", "Recipe_X",
                       "result.csv", "/path/result", "/path/images",
                       [{"die": "1,1", "v": 1.2}]],
        },
    )


# ---------- 1. Dual upstream independence ----------

def test_mqtt_wedged_does_not_block_http_publisher(tmp_path):
    """MQTT publisher constructor swapped for one that never connects.
    HTTP publisher must continue draining its own outbox independently."""
    server, base_url = _start_behavior_server()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _machine(tmp_path)
    event = _process_event(machine, profile)

    # MQTT side: outbox accumulates but never drains (publisher not started)
    mqtt_outbox = SQLiteOutbox(tmp_path / "mqtt.sqlite3")
    mqtt_pub = _LoopbackPublisher(
        config=LinkstuffsConfig(enabled=False, access_token="x"),  # not started
        outbox=mqtt_outbox,
    )

    # HTTP side: separate outbox, fully functional
    http_outbox = SQLiteOutbox(tmp_path / "http.sqlite3")
    http_pub = LinkstuffsHttpPublisher(
        LinkstuffsHttpConfig(
            enabled=True, base_url=base_url,
            device_tokens={machine.display_name: "TOK"},
        ),
        http_outbox,
    )

    mqtt_pub.queue_event(event)  # piles into mqtt outbox, unread
    http_pub.queue_event(event)
    http_pub.start()
    try:
        assert _wait_for(lambda: len(server.captured) == 1, timeout=3.0)
    finally:
        http_pub.stop()
        server.shutdown()

    assert server.captured[0]["path"].endswith("/telemetry")


# ---------- 2. Same event lands on both ----------

def test_same_canonical_event_flows_to_both_publishers_with_matching_payload(tmp_path):
    """A single CanonicalEvent fed to both publishers must produce
    structurally identical telemetry content (ts, event_type, lot_id, etc.)."""
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _machine(tmp_path)
    event = _process_event(machine, profile)

    server, base_url = _start_behavior_server()
    http_outbox = SQLiteOutbox(tmp_path / "http.sqlite3")
    http_pub = LinkstuffsHttpPublisher(
        LinkstuffsHttpConfig(
            enabled=True, base_url=base_url,
            device_tokens={machine.display_name: "TOK"},
        ),
        http_outbox,
    )

    mqtt_outbox = SQLiteOutbox(tmp_path / "mqtt.sqlite3")
    mqtt_pub = _LoopbackPublisher(
        config=LinkstuffsConfig(
            enabled=True, host="127.0.0.1", port=1883,
            access_token="x", client_id="dual-e2e",
        ),
        outbox=mqtt_outbox,
    )

    try:
        mqtt_pub.start()
        http_pub.start()
        mqtt_pub.queue_event(event)
        http_pub.queue_event(event)
        assert _wait_for(lambda: len(server.captured) >= 1, timeout=3.0)
        from eap_middleware.linkstuffs import LINKSTUFFS_TOPIC_TELEMETRY
        assert _wait_for(
            lambda: any(t == LINKSTUFFS_TOPIC_TELEMETRY
                        for t, _ in mqtt_pub.fake_client.publishes),
            timeout=3.0,
        )
    finally:
        http_pub.stop()
        mqtt_pub.stop()
        server.shutdown()

    # HTTP body: [{ts, values}]
    http_body = json.loads(server.captured[0]["body"])
    http_values = http_body[0]["values"]

    # MQTT body: {display_name: [{ts, values}]}
    mqtt_telemetry = next(
        p for t, p in mqtt_pub.fake_client.publishes
        if t == LINKSTUFFS_TOPIC_TELEMETRY
    )
    mqtt_values = mqtt_telemetry[machine.display_name][0]["values"]

    # Same canonical fields on both paths
    for key in ("event_type", "lot_id", "wafer_id", "recipe", "ceid", "load_port"):
        assert http_values.get(key) == mqtt_values.get(key), (
            f"divergence on {key}: http={http_values.get(key)} "
            f"mqtt={mqtt_values.get(key)}"
        )


# ---------- 3. Oversized payload ----------

def test_https_publisher_survives_5000_die_measurement_payload(tmp_path):
    """Real DaVinci wafers can produce TestResults with thousands of die
    entries. Confirm the HTTP body is ~MB-scale and still POSTed in one shot."""
    server, base_url = _start_behavior_server()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _machine(tmp_path)

    big_results = [
        {"die": f"{x},{y}", "v": x * 0.01 + y * 0.001, "p": True}
        for x in range(50) for y in range(100)
    ]
    event = CanonicalMapper(profile).from_secs_event(
        machine, 3140003,
        {
            "DATETIME": "20251128094700",
            "_v_raw": ["W001", "LOT", "Rcp", "rf", "/rp", "/ip", big_results],
        },
    )

    http_outbox = SQLiteOutbox(tmp_path / "ob.sqlite3")
    http_pub = LinkstuffsHttpPublisher(
        LinkstuffsHttpConfig(
            enabled=True, base_url=base_url,
            device_tokens={machine.display_name: "TOK"},
        ),
        http_outbox,
    )
    try:
        http_pub.start()
        http_pub.queue_event(event)
        assert _wait_for(lambda: len(server.captured) == 1, timeout=5.0)
    finally:
        http_pub.stop()
        server.shutdown()

    body_size = server.captured[0]["size"]
    assert body_size > 100_000, f"expected ~100KB+ body, got {body_size}"
    body = json.loads(server.captured[0]["body"])
    test_results = json.loads(body[0]["values"]["raw_TestResults"])
    assert len(test_results) == 5000


# ---------- 4. verify_tls=false ----------

def test_https_publisher_with_verify_tls_disabled_doesnt_break_http_target(tmp_path):
    """When the platform uses a private CA, verify_tls=false is the
    workaround. Confirm the publisher still POSTs successfully against a
    plain-http server (no TLS to verify) with that flag off."""
    server, base_url = _start_behavior_server()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _machine(tmp_path)
    event = _process_event(machine, profile)

    http_pub = LinkstuffsHttpPublisher(
        LinkstuffsHttpConfig(
            enabled=True, base_url=base_url,
            device_tokens={machine.display_name: "TOK"},
            verify_tls=False,
        ),
        SQLiteOutbox(tmp_path / "ob.sqlite3"),
    )
    try:
        http_pub.start()
        http_pub.queue_event(event)
        assert _wait_for(lambda: len(server.captured) == 1, timeout=3.0)
    finally:
        http_pub.stop()
        server.shutdown()


# ---------- 5. Transient timeout, then success ----------

def test_https_publisher_recovers_from_transient_timeout(tmp_path):
    """First POST hangs past timeout; second succeeds. The retry budget
    inside _post() catches it and the event still lands."""
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _machine(tmp_path)
    event = _process_event(machine, profile)

    attempt = {"n": 0}

    def behavior(record):
        attempt["n"] += 1
        # First call: sleep past the publisher's timeout (forces a TimeoutError);
        # subsequent calls: respond fast with 200.
        if attempt["n"] == 1:
            return (200, 3.0)  # hold 3s -> exceeds 1s timeout below
        return (200, 0.0)

    server, base_url = _start_behavior_server(behavior=behavior)
    http_pub = LinkstuffsHttpPublisher(
        LinkstuffsHttpConfig(
            enabled=True, base_url=base_url,
            device_tokens={machine.display_name: "TOK"},
            timeout_sec=1.0,
            retry_count=3,
            retry_delay_sec=0.1,
        ),
        SQLiteOutbox(tmp_path / "ob.sqlite3"),
    )
    try:
        http_pub.start()
        http_pub.queue_event(event)
        # First attempt hangs and times out (~1s), retries succeed.
        assert _wait_for(
            lambda: any(s == 200 for s in [200])
            and len([r for r in server.captured]) >= 2,
            timeout=6.0,
        )
    finally:
        http_pub.stop()
        server.shutdown()

    # At least 2 attempts: the first (timed out) and at least one successful retry
    assert len(server.captured) >= 2


# ---------- 6. Outbox survives "process restart" ----------

def test_outbox_replays_on_publisher_restart(tmp_path):
    """Events queued by publisher A persist in SQLite. When publisher A
    stops and a new publisher B reopens the same outbox file, the events
    deliver correctly."""
    server, base_url = _start_behavior_server()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _machine(tmp_path)
    event = _process_event(machine, profile)

    db_path = tmp_path / "shared_outbox.sqlite3"
    cfg = LinkstuffsHttpConfig(
        enabled=True, base_url=base_url,
        device_tokens={machine.display_name: "TOK"},
    )

    # "First process": queue but don't start
    outbox_a = SQLiteOutbox(db_path)
    pub_a = LinkstuffsHttpPublisher(cfg, outbox_a)
    pub_a.queue_event(event)
    # never started -> nothing published yet
    assert server.captured == []

    # "Second process": fresh outbox handle on the same file, fresh publisher
    outbox_b = SQLiteOutbox(db_path)
    pub_b = LinkstuffsHttpPublisher(cfg, outbox_b)
    try:
        pub_b.start()
        assert _wait_for(lambda: len(server.captured) == 1, timeout=3.0)
    finally:
        pub_b.stop()
        server.shutdown()


# ---------- 7. Outbox dedupe within a single publisher ----------

def test_duplicate_event_dedupes_at_outbox_layer(tmp_path):
    """Queueing the exact same canonical event twice must only produce one
    POST (outbox keys on event_key which is content-deterministic)."""
    server, base_url = _start_behavior_server()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _machine(tmp_path)
    event = _process_event(machine, profile)

    http_pub = LinkstuffsHttpPublisher(
        LinkstuffsHttpConfig(
            enabled=True, base_url=base_url,
            device_tokens={machine.display_name: "TOK"},
        ),
        SQLiteOutbox(tmp_path / "ob.sqlite3"),
    )
    try:
        http_pub.start()
        http_pub.queue_event(event)
        http_pub.queue_event(event)  # identical -> dedupe
        http_pub.queue_event(event)
        assert _wait_for(lambda: len(server.captured) == 1, timeout=3.0)
        time.sleep(0.5)  # confirm no further publishes accrue
    finally:
        http_pub.stop()
        server.shutdown()
    assert len(server.captured) == 1


# ---------- 8. Server 5xx burst then recovery ----------

def test_https_publisher_eventually_drains_after_server_5xx_burst(tmp_path):
    """Linkstuffs returns 503 for 2 attempts then 200. Outbox-level backoff
    keeps trying so the event lands."""
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _machine(tmp_path)
    event = _process_event(machine, profile)

    state = {"calls": 0}

    def behavior(record):
        state["calls"] += 1
        if state["calls"] <= 2:
            return (503, 0.0)
        return (200, 0.0)

    server, base_url = _start_behavior_server(behavior=behavior)
    http_pub = LinkstuffsHttpPublisher(
        LinkstuffsHttpConfig(
            enabled=True, base_url=base_url,
            device_tokens={machine.display_name: "TOK"},
            retry_count=5,        # in-call retries; each catches the 5xx
            retry_delay_sec=0.05,
        ),
        SQLiteOutbox(tmp_path / "ob.sqlite3"),
    )
    try:
        http_pub.start()
        http_pub.queue_event(event)
        assert _wait_for(lambda: state["calls"] >= 3, timeout=5.0)
    finally:
        http_pub.stop()
        server.shutdown()

    # First 2 calls were 503; the 3rd (or later) returned 200
    assert state["calls"] >= 3
