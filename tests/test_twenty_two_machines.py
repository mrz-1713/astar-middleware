"""22 machines of mixed vendors on one middleware process, at the same time.

This is the headline deployment claim, so it gets a real check rather than an
argument: 22 equipment simulators (all four profiles interleaved) each on its
own port, one EapMiddlewareService connecting to all of them, and an assertion
that every endpoint delivered correctly-mapped canonical events for its own
vendor - not just that the process survived.

Slow by nature (44 live HSMS endpoints, ~150 threads), so it is marked `slow`
and excluded from the default run. Run it with:

    python -m pytest tests/test_twenty_two_machines.py -m slow
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Any, Dict, List

import pytest

pytest.importorskip("secsgem")

import secsgem.hsms

from eap_middleware.models import (
    LegacyApiConfig,
    LinkstuffsConfig,
    LinkstuffsHttpConfig,
    MachineConfig,
    MiddlewarePaths,
    ServiceConfig,
)
from eap_middleware.service import EapMiddlewareService
from simulator.profile_simulator import (
    ProfileSimulator,
)

pytestmark = pytest.mark.slow

# Interleaved on purpose: consecutive endpoints are different vendors, so a
# profile leaking from one machine into its neighbour shows up immediately.
PROFILE_CYCLE = (
    "davinci_200_mc4_hc1",
    "spts_fxp_omega",
    "nexgen_mg_series",
    "ptiq_secsgem",
)
MACHINE_COUNT = 22


def _free_ports(count: int) -> List[int]:
    """Bind all of them at once so none of the ports collide with each other."""
    holders = []
    ports = []
    for _ in range(count):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
        )
        sock.bind(("127.0.0.1", 0))
        ports.append(sock.getsockname()[1])
        holders.append(sock)
    for sock in holders:
        sock.close()
    return ports


def test_twenty_two_mixed_machines_run_concurrently(tmp_path):
    ports = _free_ports(MACHINE_COUNT)
    machines: List[MachineConfig] = []
    simulators: List[Any] = []

    for index, port in enumerate(ports):
        profile_id = PROFILE_CYCLE[index % len(PROFILE_CYCLE)]
        endpoint_id = f"TOOL_{index + 1:02d}"
        middleware_mode = "active" if index % 2 == 0 else "passive"
        machines.append(
            MachineConfig(
                endpoint_id=endpoint_id,
                display_name=f"MACHINE_{index + 1:02d}",
                machine_profile=profile_id,
                host="127.0.0.1",
                port=port,
                hsms_mode=middleware_mode,
                hsms_bind_address="127.0.0.1",
                local_csv_path=str(tmp_path / "csv" / endpoint_id),
                admin_config_path=str(tmp_path / "admin" / endpoint_id),
                # The simulator answers S1F3, but 22 polling threads add
                # nothing to what this test proves.
                svid_collection_enabled=False,
            )
        )
        factory = ProfileSimulator
        kwargs = {
            "settings": secsgem.hsms.HsmsSettings(
                address="127.0.0.1",
                port=port,
                    connect_mode=(
                        secsgem.hsms.HsmsConnectMode.PASSIVE
                        if middleware_mode == "active"
                        else secsgem.hsms.HsmsConnectMode.ACTIVE
                    ),
                session_id=0,
            ),
            "tool_id": f"SIM_{index + 1:02d}",
            "wafer_count": 1,
            "step_interval_sec": 0.02,
            "fire_alarm": False,
            "loop_lots": True,
        }
        if factory is ProfileSimulator:
            kwargs["profile_id"] = profile_id
        simulators.append(factory(**kwargs))

    config = ServiceConfig(
        machines=machines,
        linkstuffs=LinkstuffsConfig(enabled=False),
        linkstuffs_http=LinkstuffsHttpConfig(enabled=False),
        legacy_api=LegacyApiConfig(enabled=False),
        paths=MiddlewarePaths(
            install_dir=str(tmp_path / "install"),
            outbox_db=str(tmp_path / "o.sqlite3"),
            legacy_api_outbox_db=str(tmp_path / "l.sqlite3"),
            http_outbox_db=str(tmp_path / "h.sqlite3"),
        ),
        startup_stagger_sec=0.05,
    )
    service = EapMiddlewareService(config)

    # Record what each endpoint actually produced, through the service's own
    # mapper (i.e. with that endpoint's profile).
    seen: Dict[str, List[str]] = {m.endpoint_id: [] for m in machines}
    unmapped: Dict[str, set] = {m.endpoint_id: set() for m in machines}
    lock = threading.Lock()
    original = service._on_secs_event

    def spy(machine: MachineConfig, ceid: int, data: Dict[str, object]) -> None:
        original(machine, ceid, data)
        events = service._mapper(machine).from_secs_events(machine, ceid, data)
        with lock:
            seen[machine.endpoint_id].extend(e.event_type for e in events)
            if any(e.event_type == "unknown" for e in events):
                unmapped[machine.endpoint_id].add(ceid)

    service._on_secs_event = spy  # type: ignore[method-assign]

    connected: Dict[str, str] = {}
    try:
        for simulator in simulators:
            simulator.enable()
            simulator.start_events()
        time.sleep(1.0)  # let 22 listeners bind
        service.start()

        deadline = time.time() + 180.0
        while time.time() < deadline:
            with lock:
                done = sum(
                    1 for events in seen.values() if "lot_end" in events
                )
            if done == MACHINE_COUNT:
                break
            time.sleep(0.5)
        connected = service.machine_states()
    finally:
        service.stop()
        for simulator in simulators:
            try:
                simulator.disable()
            except Exception:
                pass

    assert len(connected) == MACHINE_COUNT
    assert set(connected.values()) == {"connected"}

    incomplete = sorted(
        endpoint for endpoint, events in seen.items() if "lot_end" not in events
    )
    assert not incomplete, f"machines that never completed a lot: {incomplete}"

    for endpoint, events in seen.items():
        assert not unmapped[endpoint], (
            f"{endpoint} ({dict((m.endpoint_id, m.machine_profile) for m in machines)[endpoint]}) "
            f"reported CEIDs its profile does not map: {sorted(unmapped[endpoint])}"
        )
        # The steps every vendor has. process_start/process_end are not
        # universal: the MG maps its per-port "processing started" CEIDs to
        # lot_start, because on that tool they are the lot boundary.
        for step in ("lot_start", "wafer_start", "wafer_end", "lot_end"):
            assert step in events, (
                f"{endpoint} never reported {step}; got {sorted(set(events))}"
            )
