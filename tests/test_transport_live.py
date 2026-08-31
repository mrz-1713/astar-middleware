"""Live integration tests: verify HTTPS and MQTT transports reach the real
Linkstuffs platform.

Run explicitly:
    python -m pytest tests/test_transport_live.py -v -s

These hit the network. Set ``EAP_HTTPS_TEST_TOKEN`` and
``EAP_MQTT_TEST_TOKEN`` for the AStar development tenant before running.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("EAP_RUN_LIVE_TESTS") != "1",
        reason="set EAP_RUN_LIVE_TESTS=1 and use -m live to run external tests",
    ),
]

from eap_middleware.linkstuffs import LinkstuffsGatewayPublisher
from eap_middleware.linkstuffs_http import LinkstuffsHttpPublisher
from eap_middleware.mapper import CanonicalMapper
from eap_middleware.models import LinkstuffsConfig, LinkstuffsHttpConfig, MachineConfig
from eap_middleware.outbox import SQLiteOutbox
from eap_middleware.profiles import ProfileRegistry

HTTPS_TOKEN = os.environ.get("EAP_HTTPS_TEST_TOKEN", "").strip()
MQTT_TOKEN = os.environ.get("EAP_MQTT_TEST_TOKEN", "").strip()
BASE_URL    = "https://astar-monitoring.linkstuffs.com"
MQTT_HOST   = "astar-monitoring.linkstuffs.com"
MQTT_PORT   = 1883

TEST_DEVICE = "EAP_TRANSPORT_TEST"


def _require_token(token, variable):
    if not token:
        pytest.skip(f"set {variable} to run this live integration test")


def _test_machine(tmp_path, display=TEST_DEVICE):
    return MachineConfig(
        endpoint_id="TOOL_LIVE",
        display_name=display,
        machine_profile="davinci_200_mc4_hc1",
        host="127.0.0.1",
        port=5000,
        local_csv_path=str(tmp_path / "local"),
        admin_config_path=str(tmp_path / "admin"),
    )


def _lot_start_event(machine, profile):
    return CanonicalMapper(profile).from_secs_event(
        machine, 3200017,
        {
            "DATETIME": "20260625120000",
            "SECSGEM_RAW_EVENT": "ControlJob:Selected-Executing",
            "LotID": "LIVE-TEST-LOT-001",
            "RecipeName": "Test_Recipe_v1",
            "LoadPort": "LP1",
        },
    )


# ---------- HTTPS ----------

def test_https_telemetry_reaches_linkstuffs(tmp_path):
    """POST a minimal telemetry payload directly via urllib and assert 200."""
    _require_token(HTTPS_TOKEN, "EAP_HTTPS_TEST_TOKEN")
    url = f"{BASE_URL}/api/v1/{HTTPS_TOKEN}/telemetry"
    ts_ms = int(time.time() * 1000)
    body = json.dumps([{"ts": ts_ms, "values": {"transport_test": "https", "ok": True}}],
                      separators=(",", ":")).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "astar-eap-middleware/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        assert 200 <= resp.status < 300, f"unexpected status {resp.status}"
    masked_url = url.replace(HTTPS_TOKEN, f"{HTTPS_TOKEN[:4]}***")
    print(f"\n[HTTPS] POST {masked_url} -> {resp.status} OK")


def test_https_publisher_sends_event(tmp_path):
    """Full publisher path: queue a canonical event, drain to real Linkstuffs."""
    _require_token(HTTPS_TOKEN, "EAP_HTTPS_TEST_TOKEN")
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _test_machine(tmp_path)

    publisher = LinkstuffsHttpPublisher(
        LinkstuffsHttpConfig(
            enabled=True,
            base_url=BASE_URL,
            device_tokens={TEST_DEVICE: HTTPS_TOKEN},
            timeout_sec=15,
            retry_count=2,
            retry_delay_sec=1,
        ),
        SQLiteOutbox(tmp_path / "ob_https.sqlite3"),
    )
    event = _lot_start_event(machine, profile)
    try:
        publisher.start()
        publisher.queue_event(event)
        # Give publisher worker time to drain (max 10s)
        deadline = time.time() + 10
        while time.time() < deadline:
            pending = list(publisher.outbox.pending(limit=10))
            if not pending:
                break
            time.sleep(0.2)
    finally:
        publisher.stop()

    assert not list(publisher.outbox.pending(limit=1)), "outbox not drained - check token/URL"
    print(f"\n[HTTPS publisher] drained {event.event_type} event to Linkstuffs OK")


# ---------- MQTT ----------

def _require_mqtt_reachable():
    """Skip the test if port 1883 is firewalled (Cloudflare blocks it)."""
    import socket
    try:
        s = socket.create_connection((MQTT_HOST, MQTT_PORT), timeout=5)
        s.close()
    except (OSError, TimeoutError) as e:
        pytest.skip(
            f"MQTT port {MQTT_PORT} unreachable ({e}). "
            "Linkstuffs is behind Cloudflare which blocks 1883/8883. "
            "Use HTTPS transport (linkstuffs_http) instead."
        )


def test_mqtt_gateway_connect_and_telemetry(tmp_path):
    """Connect MQTT gateway, register a test device, publish one telemetry batch."""
    _require_token(MQTT_TOKEN, "EAP_MQTT_TEST_TOKEN")
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        pytest.skip("paho-mqtt not installed")

    _require_mqtt_reachable()

    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _test_machine(tmp_path)

    received_connack = []
    published_mids = []

    callback_api = getattr(mqtt, "CallbackAPIVersion", None)
    client_kwargs = {"client_id": "astar-live-test", "protocol": mqtt.MQTTv311}
    if callback_api:
        client_kwargs["callback_api_version"] = callback_api.VERSION2

    client = mqtt.Client(**client_kwargs)
    client.username_pw_set(MQTT_TOKEN)

    if callback_api:
        def on_connect(c, userdata, flags, rc, props):
            received_connack.append(int(getattr(rc, "value", rc) or 0))
        def on_publish(c, userdata, mid, rc, props):
            published_mids.append(mid)
    else:
        def on_connect(c, userdata, flags, rc):
            received_connack.append(rc)
        def on_publish(c, userdata, mid):
            published_mids.append(mid)

    client.on_connect = on_connect
    client.on_publish = on_publish

    loop_started = False
    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        client.loop_start()
        loop_started = True

        # Wait for CONNACK
        deadline = time.time() + 10
        while time.time() < deadline and not received_connack:
            time.sleep(0.1)

        assert received_connack, "No CONNACK received - broker unreachable?"
        assert received_connack[0] == 0, f"CONNACK rc={received_connack[0]} (bad token?)"
        print(f"\n[MQTT] Connected to {MQTT_HOST}:{MQTT_PORT} (rc=0 OK)")

        # Register test device
        connect_payload = json.dumps(
            {"device": TEST_DEVICE, "type": "davinci_200_mc4_hc1"},
            separators=(",", ":"),
        )
        info = client.publish("v1/gateway/connect", connect_payload, qos=1)
        info.wait_for_publish(timeout=10)
        assert info.is_published(), "gateway/connect publish failed"

        # Send telemetry
        ts_ms = int(time.time() * 1000)
        event = _lot_start_event(machine, profile)
        telemetry = {
            TEST_DEVICE: [{
                "ts": ts_ms,
                "values": {"transport_test": "mqtt", "event_type": event.event_type},
            }]
        }
        info2 = client.publish(
            "v1/gateway/telemetry",
            json.dumps(telemetry, separators=(",", ":")),
            qos=1,
        )
        info2.wait_for_publish(timeout=10)
        assert info2.is_published(), "gateway/telemetry publish failed"
        print(f"[MQTT] Published {event.event_type} telemetry for {TEST_DEVICE} OK")
    finally:
        if loop_started:
            client.loop_stop()
        client.disconnect()


def test_mqtt_publisher_drains_outbox_to_real_broker(tmp_path):
    """Full publisher path: queue event, let worker drain to real Linkstuffs broker."""
    _require_token(MQTT_TOKEN, "EAP_MQTT_TEST_TOKEN")
    try:
        import paho.mqtt.client  # noqa
    except ImportError:
        pytest.skip("paho-mqtt not installed")

    _require_mqtt_reachable()

    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = _test_machine(tmp_path)
    outbox = SQLiteOutbox(tmp_path / "ob_mqtt.sqlite3")

    publisher = LinkstuffsGatewayPublisher(
        LinkstuffsConfig(
            enabled=True,
            host=MQTT_HOST,
            port=MQTT_PORT,
            access_token=MQTT_TOKEN,
            qos=1,
            client_id="astar-live-pub-test",
        ),
        outbox,
    )
    event = _lot_start_event(machine, profile)
    try:
        publisher.start()
        publisher.queue_machine_connect(machine)
        publisher.queue_event(event)

        # Wait for connected + drained (max 15s)
        deadline = time.time() + 15
        while time.time() < deadline:
            if publisher._connected and not list(outbox.pending(limit=1)):
                break
            time.sleep(0.3)
    finally:
        publisher.stop()

    print(f"\n[MQTT publisher] drained to real broker, connected={publisher._connected}")
    # The outbox state is authoritative; _connected can change during shutdown.
    remaining = list(outbox.pending(limit=10))
    assert not remaining, f"outbox has {len(remaining)} unsent items - broker/token issue"
