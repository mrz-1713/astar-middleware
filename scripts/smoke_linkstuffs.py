#!/usr/bin/env python3
"""End-to-end smoke test against the real Linkstuffs broker.

Connects with the gateway access token, publishes one v1/gateway/connect +
one v1/gateway/telemetry, and reports whether each step succeeded.

If the broker accepts the publishes AND the device appears in the Linkstuffs
admin UI, the gateway flag is correctly flipped and the middleware will work
end-to-end. If the broker accepts publishes but no device appears, the flag
is still off (broker happily accepts any topic but silently drops gateway
messages from non-gateway tokens).

Usage:
    EAP_TEST_BROKER=astar-monitoring.linkstuffs.com \
    EAP_TEST_PORT=1883 \
    EAP_TEST_TOKEN=PASTE_TOKEN_HERE \
    python scripts/smoke_linkstuffs.py

After it completes, delete the test device from Linkstuffs admin so it
doesn't pollute the real environment.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt


BROKER = os.environ.get("EAP_TEST_BROKER", "astar-monitoring.linkstuffs.com")
PORT = int(os.environ.get("EAP_TEST_PORT", "1883"))
TOKEN = os.environ.get("EAP_TEST_TOKEN", "").strip()
TIMEOUT = float(os.environ.get("EAP_TEST_TIMEOUT", "10"))

# A clearly test-named device so it's obvious to delete afterwards.
DEVICE = f"ASTAR_SMOKE_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def main() -> int:
    if not TOKEN:
        print("FAIL: set EAP_TEST_TOKEN before running this live smoke test.")
        return 2

    print(f"==> smoke test against {BROKER}:{PORT} with token {TOKEN[:4]}***")
    print(f"    test device: {DEVICE}")

    connect_ok = {"v": False, "rc": None}
    publish_acks: list[int] = []

    def on_connect(client, userdata, connect_flags, reason_code, properties):
        rc = int(getattr(reason_code, "value", reason_code) or 0)
        connect_ok["v"] = rc == 0
        connect_ok["rc"] = rc
        print(f"    on_connect rc={rc} ({'ok' if rc == 0 else 'AUTH/CONFIG FAIL'})")

    def on_publish(client, userdata, mid, reason_code=None, properties=None):
        publish_acks.append(mid)

    def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
        print(f"    on_disconnect rc={reason_code}")

    client = mqtt.Client(
        client_id=f"astar-smoke-{int(time.time())}",
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        protocol=mqtt.MQTTv311,
    )
    client.username_pw_set(TOKEN)
    client.on_connect = on_connect
    client.on_publish = on_publish
    client.on_disconnect = on_disconnect

    try:
        client.connect(BROKER, PORT, keepalive=30)
    except Exception as exc:
        print(f"FAIL: cannot reach {BROKER}:{PORT} -> {exc}")
        return 2

    client.loop_start()
    deadline = time.time() + TIMEOUT
    while time.time() < deadline and not connect_ok["v"]:
        time.sleep(0.1)
    if not connect_ok["v"]:
        print(f"FAIL: MQTT connect did not complete in {TIMEOUT}s (rc={connect_ok['rc']})")
        client.loop_stop()
        return 3

    # Gateway connect: tells Linkstuffs to provision the downstream device
    print(f"==> publishing v1/gateway/connect for {DEVICE}")
    info1 = client.publish(
        "v1/gateway/connect",
        json.dumps({"device": DEVICE, "type": "astar_smoke_test"}),
        qos=1,
    )
    info1.wait_for_publish(timeout=TIMEOUT)
    if not info1.is_published():
        print("FAIL: v1/gateway/connect was not acknowledged (PUBACK timeout)")
        client.loop_stop()
        return 4
    print("    PUBACK ok")

    # Gateway telemetry: one fake event with a couple of values
    ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    payload = {
        DEVICE: [{
            "ts": ts_ms,
            "values": {
                "event_type": "smoke",
                "load_port": "1",
                "lot_id": "SMOKE_LOT",
                "hello": "world",
            },
        }],
    }
    print(f"==> publishing v1/gateway/telemetry for {DEVICE}")
    info2 = client.publish(
        "v1/gateway/telemetry",
        json.dumps(payload),
        qos=1,
    )
    info2.wait_for_publish(timeout=TIMEOUT)
    if not info2.is_published():
        print("FAIL: v1/gateway/telemetry was not acknowledged (PUBACK timeout)")
        client.loop_stop()
        return 5
    print("    PUBACK ok")

    # Gateway attributes - so the device shows useful identification metadata
    attrs = {
        DEVICE: {
            "endpoint_id": "ASTAR_SMOKE",
            "vendor": "AStar",
            "model": "Smoke Test",
            "test_run_at": datetime.now().isoformat(),
        },
    }
    info3 = client.publish(
        "v1/gateway/attributes",
        json.dumps(attrs),
        qos=1,
    )
    info3.wait_for_publish(timeout=TIMEOUT)

    client.loop_stop()
    client.disconnect()

    print()
    print("=" * 70)
    print("SMOKE OK: broker accepted gateway connect + telemetry + attributes.")
    print(f"Now open Linkstuffs admin and check whether device '{DEVICE}'")
    print("appears under Entities -> Devices with telemetry hello=world,")
    print("event_type=smoke, lot_id=SMOKE_LOT.")
    print()
    print("If YES -> gateway flag is correctly flipped; middleware will work.")
    print("If NO  -> token is for a non-gateway device; ask Linkstuffs admin")
    print("         to tick 'Is gateway' on the device that owns this token.")
    print()
    print(f"After verifying, delete device '{DEVICE}' from Linkstuffs admin.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
