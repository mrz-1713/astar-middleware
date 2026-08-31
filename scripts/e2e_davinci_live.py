#!/usr/bin/env python3
"""End-to-end live test: DaVinci simulator -> middleware -> real Linkstuffs cloud.

  SecsGemEquipment (PASSIVE)                   Linkstuffs (cloud)
        |                                              ^
        | HSMS / SECS-II over TCP                      |
        | (real S6F11, real S5F1)                      | HTTPS POST
        v                                              |
   secs_runtime.SecsMachineSession                     |
        |                                              |
        v                                              |
   CanonicalMapper -> JobTracker                       |
        |                                              |
        v                                              |
   LinkstuffsHttpPublisher --(real cloud)-->-----------+

Drives one full DaVinci lot (carrier load, control job, per-wafer
processing with measurement payload, alarm, carrier depart) through every
layer of the production stack. The HTTPS publisher posts everything to the
real cloud Linkstuffs at astar-monitoring.linkstuffs.com.

Set ``LINKSTUFFS_HTTP_DAVINCI_TOKEN`` before running. After the script
completes, open Linkstuffs admin -> Entities -> Devices and find the test
device. Latest telemetry should show the full lot's events.
"""

from __future__ import annotations

import logging
import os
import socket
import sqlite3
import struct
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict

import secsgem.hsms

from eap_middleware.csv_store import PerLotCsvWriter
from eap_middleware.job_tracker import JobTracker
from eap_middleware.linkstuffs_http import LinkstuffsHttpPublisher
from eap_middleware.mapper import CanonicalMapper
from eap_middleware.models import LinkstuffsHttpConfig, MachineConfig
from eap_middleware.outbox import SQLiteOutbox
from eap_middleware.profiles import ProfileRegistry
from eap_middleware.secs_runtime import SecsMachineSession
from simulator.secsgem_equipment import SecsGemEquipment


TOKEN = os.environ.get("LINKSTUFFS_HTTP_DAVINCI_TOKEN", "").strip()
BASE_URL = "https://astar-monitoring.linkstuffs.com"
DEVICE = "DAV_E2E_LIVE"     # whatever name appears in Linkstuffs for this token
WAFERS = 2
STEP_INTERVAL_SEC = 0.15


def _free_port() -> int:
    for _ in range(20):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", p))
            probe.close()
            return p
        except OSError:
            probe.close()
    raise RuntimeError("no free port")


def main() -> int:
    if not TOKEN:
        print(
            "ERROR: set LINKSTUFFS_HTTP_DAVINCI_TOKEN to the test device token "
            "before running this live test.",
            file=sys.stderr,
        )
        return 2

    logging.basicConfig(
        level=logging.WARNING,  # quiet down secsgem; we'll print our own progress
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    for noisy in ("secsgem", "simulator", "gateway"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("e2e")
    log.setLevel(logging.INFO)

    port = _free_port()
    tmp = Path(tempfile.mkdtemp(prefix="e2e_davinci_"))

    print("=" * 70)
    print(f"E2E live test: DaVinci simulator -> middleware -> {BASE_URL}")
    print(f"Device token : {TOKEN[:4]}***")
    print(f"Display name : {DEVICE}")
    print(f"HSMS port    : {port} (localhost)")
    print(f"Wafers       : {WAFERS}")
    print(f"Outbox dir   : {tmp}")
    print("=" * 70)

    # ---------- DaVinci simulator (PASSIVE) ----------
    sim_settings = secsgem.hsms.HsmsSettings(
        address="127.0.0.1",
        port=port,
        connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
        session_id=0,
    )
    simulator = SecsGemEquipment(
        settings=sim_settings,
        tool_id="DAV_SIM_LIVE",
        wafer_count=WAFERS,
        step_interval_sec=STEP_INTERVAL_SEC,
        fire_alarm=True,
        loop_lots=False,
    )

    # ---------- Middleware (ACTIVE) ----------
    machine = MachineConfig(
        endpoint_id="TOOL_E2E",
        display_name=DEVICE,
        machine_profile="davinci_200_mc4_hc1",
        host="127.0.0.1",
        port=port,
        secs_device_id=0,
        local_csv_path=str(tmp / "csv_local"),
        admin_config_path=str(tmp / "admin"),
        hsms_mode="active",
    )
    profile = ProfileRegistry().get(machine.machine_profile)
    tracker = JobTracker()
    mapper = CanonicalMapper(profile, tracker=tracker)
    csv_writer = PerLotCsvWriter()

    # ---------- HTTPS upstream to the real cloud ----------
    http_outbox = SQLiteOutbox(tmp / "http_outbox.sqlite3")
    http_pub = LinkstuffsHttpPublisher(
        config=LinkstuffsHttpConfig(
            enabled=True,
            base_url=BASE_URL,
            device_tokens={DEVICE: TOKEN},
        ),
        outbox=http_outbox,
    )

    sent_per_type: Dict[str, int] = {}
    alarms_sent = []

    def on_event(_machine, ceid, data):
        ev = mapper.from_secs_event(_machine, ceid, data)
        csv_writer.append(_machine, profile, ev)
        http_pub.queue_event(ev)
        sent_per_type[ev.event_type] = sent_per_type.get(ev.event_type, 0) + 1

    def on_alarm(_machine, alarm):
        ev = mapper.alarm_event(_machine, alarm)
        http_pub.queue_event(ev)
        alarms_sent.append(alarm.get("alid"))

    session = SecsMachineSession(
        machine=machine,
        event_callback=on_event,
        alarm_callback=on_alarm,
        connect_callback=lambda *a, **k: print("--- HSMS connected"),
        disconnect_callback=lambda *a, **k: print("--- HSMS disconnected"),
    )

    try:
        http_pub.queue_machine_attributes(machine, profile)
        http_pub.start()
        print("\n[1/4] Starting simulator (PASSIVE listener)...")
        simulator.enable()
        simulator.start_events()
        time.sleep(0.3)

        print("[2/4] Starting middleware session (ACTIVE dial)...")
        session.start()

        print("[3/4] Lot is running - waiting 8s for events + drain...")
        time.sleep(8)
        # Drain any remaining outbox rows (give publisher 3s more)
        for _ in range(30):
            if not http_outbox.pending(limit=200):
                break
            time.sleep(0.1)
        csv_writer.flush_all(reason="e2e_done")

    finally:
        print("\n[4/4] Tearing down...")
        try: session.stop()
        except Exception: pass
        try: simulator.disable()
        except Exception: pass
        # Give the publisher a moment to drain remaining outbox rows
        time.sleep(1.5)
        http_pub.stop()

    # ---------- Final report ----------
    with sqlite3.connect(tmp / "http_outbox.sqlite3") as conn:
        rows = dict(conn.execute(
            "SELECT status, COUNT(*) FROM outbox GROUP BY status"
        ).fetchall())

    csv_files = list((tmp / "csv_local").glob("*.csv"))

    print()
    print("=" * 70)
    print("RESULTS")
    print("=" * 70)
    print("Events received from simulator (by canonical event_type):")
    for et, n in sorted(sent_per_type.items()):
        print(f"  {et:20s}  {n}")
    print(f"Alarms received: {len(alarms_sent)} (ALIDs: {alarms_sent})")
    print()
    print(f"HTTPS outbox final state: {rows}")
    print(f"Per-lot CSV files written: {len(csv_files)}")
    for f in csv_files:
        print(f"  {f.name}")
    print()
    sent = rows.get("sent", 0)
    failed = rows.get("failed", 0)
    pending = rows.get("pending", 0)
    success = sent > 0 and failed == 0 and pending == 0
    if success:
        print(f"OK: {sent} HTTPS POSTs accepted by {BASE_URL}")
        print()
        print("Open Linkstuffs admin -> Entities -> Devices -> find the")
        print(f"device whose token is {TOKEN}. Latest telemetry should show:")
        print(f"  - event_type values: {sorted(sent_per_type.keys())}")
        print("  - lot_id starting with LOT_SIM_")
        print("  - wafer_id values W*_01, W*_02, ...")
        print("  - recipe Recipe_Overlay_v3")
        print("  - raw_TestResults JSON for each process_end event")
        if alarms_sent:
            print(f"  - one alarm event with raw_alid in {set(alarms_sent)}")
        print()
        return 0
    else:
        print(f"FAIL: sent={sent} failed={failed} pending={pending}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
