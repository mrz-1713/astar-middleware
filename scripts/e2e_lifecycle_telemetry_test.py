#!/usr/bin/env python3
"""End-to-end lifecycle test against the REAL Linkstuffs HTTPS endpoint.

Drives a full DaVinci lot lifecycle (carrier in -> control job -> per-wafer
process -> alarm -> control job complete -> carrier out) through the REAL
secsgem stack (host <-> simulator over a loopback HSMS socket, same library
used against the physical tool), runs every captured event through the real
CanonicalMapper, and POSTs the resulting telemetry to the real production
Linkstuffs device (DAVINCI200_MC4_HC1_01) using the token from
config/production.yaml.

This is a one-shot manual verification script (not part of pytest) because it
talks to a live third-party cloud service. Run it deliberately:

    python3 scripts/e2e_lifecycle_telemetry_test.py

For every event it prints:
  - the exact JSON payload sent (captured before the HTTP call, so this is
    ground truth of what left the process)
  - the HTTP status Linkstuffs returned for that POST

Then it analyzes the captured payloads for correctness: required fields
present, no leaked internal markers, lot/wafer/recipe continuity across the
lot, JSON-serializable nested fields round-trip cleanly.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

# Quiet the very verbose secsgem wire-trace logging (every SECS message body)
# so the interesting output (per-event POST results + analysis) isn't buried.
logging.basicConfig(level=logging.WARNING)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import secsgem.hsms  # noqa: E402

from gateway.host import GatewayHost, create_host_settings  # noqa: E402
from simulator.secsgem_equipment import SecsGemEquipment  # noqa: E402
from eap_middleware.job_tracker import JobTracker  # noqa: E402
from eap_middleware.mapper import CanonicalMapper  # noqa: E402
from eap_middleware.models import MachineConfig  # noqa: E402
from eap_middleware.profiles import ProfileRegistry  # noqa: E402
from eap_middleware.linkstuffs_http import LinkstuffsHttpPublisher  # noqa: E402
from eap_middleware.config import load_service_config  # noqa: E402

CONFIG_PATH = REPO_ROOT / "config" / "production.yaml"
DISPLAY_NAME = "DAVINCI200_MC4_HC1_01"


def _free_port() -> int:
    for _ in range(20):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
            probe.close()
            return port
        except OSError:
            probe.close()
            continue
    raise RuntimeError("no free port")


def _wait(pred, timeout=20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


def post(url: str, body) -> int:
    data = json.dumps(body, separators=(",", ":"), default=str).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "astar-eap-middleware/1.0"},
    )
    try:
        # Live-test URL is an explicit operator-provided HTTP(S) endpoint.
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def main() -> int:
    print("=" * 78)
    print("E2E LIFECYCLE TEST: real secsgem stack -> real Linkstuffs HTTPS endpoint")
    print("=" * 78)

    cfg = load_service_config(CONFIG_PATH)
    token = cfg.linkstuffs_http.device_tokens.get(DISPLAY_NAME, "")
    base_url = cfg.linkstuffs_http.base_url
    if not token or not base_url:
        print("FAIL: no device token / base_url configured for", DISPLAY_NAME)
        return 2
    telemetry_url = f"{base_url}/api/v1/{token}/telemetry"
    attrs_url = f"{base_url}/api/v1/{token}/attributes"
    print(f"Target device : {DISPLAY_NAME}")
    print(f"Endpoint      : {base_url}/api/v1/{token[:4]}***/telemetry")
    print()

    # --- real secsgem host <-> real DaVinci simulator over loopback HSMS ---
    port = _free_port()
    sim_settings = secsgem.hsms.HsmsSettings(
        address="127.0.0.1", port=port,
        connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE, session_id=0,
    )
    simulator = SecsGemEquipment(
        settings=sim_settings, tool_id="DAV_SIM_E2E",
        wafer_count=3, step_interval_sec=0.05,
        fire_alarm=True, loop_lots=False,
    )

    events: list[tuple[int, dict]] = []
    connected: list[int] = []
    host_settings = create_host_settings(host="127.0.0.1", port=port, device_id=0, mode="active")
    host_settings.timeouts.t3 = 5
    host_settings.timeouts.t6 = 3
    host = GatewayHost(
        settings=host_settings, tool_id="DAV_E2E",
        on_event=lambda _t, ceid, data: events.append((ceid, data)),
        on_connect=lambda _t: connected.append(1),
    )

    machine = MachineConfig(
        endpoint_id="TOOL_E2E", display_name=DISPLAY_NAME,
        machine_profile="davinci_200_mc4_hc1", host="127.0.0.1", port=port,
    )
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile, tracker=JobTracker())

    sent_payloads: list[dict] = []
    sent_statuses: list[int] = []

    try:
        print("-- starting simulator + real secsgem host (loopback HSMS) --")
        simulator.enable()
        simulator.start_events()
        time.sleep(0.3)
        host.enable()
        if not _wait(lambda: bool(connected)):
            print("FAIL: host never reached COMMUNICATING")
            return 3
        print("   connected (COMMUNICATING)")

        ok = host.subscribe_to_events("output/davinci200_mc4_hc1/EventSubscription.json")
        print(f"   subscribe_to_events -> {ok}")
        if not ok:
            print("FAIL: subscription rejected")
            return 4

        ok = host.enable_all_alarms()
        print(f"   enable_all_alarms -> {ok}")
        print()

        # Push device attributes once, like the real publisher does on connect.
        attrs = LinkstuffsHttpPublisher.attributes_payload(machine, profile)
        status = post(attrs_url, attrs)
        print(f"-- attributes POST -> HTTP {status} --")
        print(json.dumps(attrs, indent=2))
        print()

        print("-- waiting for full lot lifecycle (carrier in -> ... -> carrier out) --")
        ok = _wait(lambda: any(c == 3160002 for c, _ in events), timeout=30.0)
        if not ok:
            print("FAIL: lot never completed (no CarrierDeparted 3160002 observed)")
            return 5
        # small settle window for any trailing events
        time.sleep(0.5)
        print(f"   captured {len(events)} raw SECS events")
        print()

        # --- transform every captured event exactly like the live service
        # does, and send while the loopback session is still up (teardown of
        # the secsgem dispatcher threads happens AFTER, off the critical path
        # below, since it has been observed to hang joining its threads). ---
        print("=" * 78)
        print("SENDING TELEMETRY TO LINKSTUFFS")
        print("=" * 78)
        for ceid, raw in events:
            ev = mapper.from_secs_event(machine, ceid, raw)
            payload = LinkstuffsHttpPublisher.telemetry_payload(ev)
            status = post(telemetry_url, payload)
            sent_payloads.append(payload[0])
            sent_statuses.append(status)
            vals = payload[0]["values"]
            print(f"[CEID {ceid:>7}] {vals.get('event_type'):<16} "
                  f"lot={vals.get('lot_id') or '-':<14} "
                  f"wafer={vals.get('wafer_id') or '-':<12} "
                  f"-> HTTP {status}", flush=True)
    finally:
        # disable() has been observed to hang joining the secsgem dispatcher
        # thread after a full lifecycle run. Do it on a daemon thread with a
        # bounded wait so a stuck teardown can never block the report below;
        # the process exits at the end of main() regardless of thread state.
        def _teardown():
            try:
                host.disable()
            except Exception:
                pass
            try:
                simulator.disable()
            except Exception:
                pass

        t = threading.Thread(target=_teardown, daemon=True)
        t.start()
        t.join(timeout=5.0)
        if t.is_alive():
            print("(teardown still running in background, continuing anyway)")

    print()
    print("=" * 78)
    print("ANALYSIS OF SENT TELEMETRY")
    print("=" * 78)

    n = len(sent_payloads)
    n_ok = sum(1 for s in sent_statuses if 200 <= s < 300)
    print(f"Events sent       : {n}")
    print(f"Accepted (2xx)    : {n_ok}")
    print(f"Rejected/non-2xx  : {n - n_ok}")
    if n_ok < n:
        for p, s in zip(sent_payloads, sent_statuses):
            if not (200 <= s < 300):
                print(f"   REJECTED status={s}: {json.dumps(p)}")

    required_keys = {
        "endpoint_id", "display_name", "machine_profile", "vendor", "model",
        "event_type", "raw_event_name", "ceid", "load_port", "chamber",
        "lot_id", "wafer_id", "recipe", "secs_raw_event",
    }
    missing_keys_events = []
    leaked_internal = []
    bad_json_nested = []
    for p in sent_payloads:
        vals = p["values"]
        missing = required_keys - set(vals.keys())
        if missing:
            missing_keys_events.append((vals.get("ceid"), missing))
        for k, v in vals.items():
            if k.startswith("_") or k == "raw__v_raw":
                leaked_internal.append((vals.get("ceid"), k))
            if k.startswith("raw_") and isinstance(v, str) and (v.startswith("[") or v.startswith("{")):
                try:
                    json.loads(v)
                except (TypeError, ValueError):
                    bad_json_nested.append((vals.get("ceid"), k, v[:80]))

    print()
    print(f"Schema completeness     : {'OK' if not missing_keys_events else 'FAIL'} "
          f"({len(missing_keys_events)} events missing required keys)")
    for ceid, missing in missing_keys_events:
        print(f"   CEID {ceid}: missing {missing}")

    print(f"Internal marker leakage : {'OK (none leaked)' if not leaked_internal else 'FAIL'}")
    for ceid, k in leaked_internal:
        print(f"   CEID {ceid}: leaked {k}")

    print(f"Nested JSON round-trip  : {'OK' if not bad_json_nested else 'FAIL'}")
    for ceid, k, v in bad_json_nested:
        print(f"   CEID {ceid}: {k} did not parse as JSON: {v}")

    # Lot/wafer/recipe continuity across the lifecycle
    lot_ids = {p["values"]["lot_id"] for p in sent_payloads if p["values"]["lot_id"]}
    recipes = {p["values"]["recipe"] for p in sent_payloads if p["values"]["recipe"]}
    wafer_ids = sorted({p["values"]["wafer_id"] for p in sent_payloads if p["values"]["wafer_id"]})
    print()
    print(f"Distinct lot_id(s) seen : {sorted(lot_ids)} ({'OK single lot' if len(lot_ids) == 1 else 'WARN multiple lots'})")
    print(f"Distinct recipe(s) seen : {sorted(recipes)} ({'OK single recipe' if len(recipes) <= 1 else 'WARN'})")
    print(f"Distinct wafer_id(s)    : {wafer_ids} (expect 3 for this run)")

    event_types = [p["values"]["event_type"] for p in sent_payloads]
    print(f"event_type sequence    : {event_types}")
    unknown = [e for e in event_types if e in ("unknown", "", None)]
    print(f"Unknown event_type     : {'OK (none)' if not unknown else f'FAIL ({len(unknown)})'}")

    # Example full payload for a process_end event (richest payload)
    sample = next((p for p in sent_payloads if p["values"]["event_type"] == "process_end"), None)
    if sample:
        print()
        print("-- sample process_end payload (as sent) --")
        print(json.dumps(sample, indent=2, default=str))

    print()
    overall_ok = (
        n_ok == n
        and not missing_keys_events
        and not leaked_internal
        and not bad_json_nested
        and len(lot_ids) == 1
        and not unknown
    )
    print("=" * 78)
    print("OVERALL:", "PASS" if overall_ok else "FAIL")
    print("=" * 78)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    # A lingering secsgem dispatcher/server thread (non-daemon in some teardown
    # paths) can otherwise keep the interpreter alive after results are printed.
    os._exit(code)
