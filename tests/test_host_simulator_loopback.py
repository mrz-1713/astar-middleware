"""Full-stack loopback with the simulator on the HOST side of the link.

ProfileSimulator (equipment, PASSIVE) <==HSMS==> HostSimulator (host, ACTIVE)

The other loopback tests prove the middleware can host a simulated tool.
This one proves the reverse setting is real and not just a label: with
connection.role: host the packaged simulator performs the opening
sequence itself (S1F17, S2F33/35/37, S5F3) and receives S6F11 reports
with no middleware installed anywhere.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

pytest.importorskip("secsgem")

from simulator.config import (
    ConnectionConfig,
    HostConfig,
    SimulationConfig,
    SimulatorConfig,
)
from simulator.equipment import create_equipment_settings
from simulator.host_simulator import HostSimulator
from simulator.profile_simulator import ProfileSimulator
from simulator.runner import SimulatorRunner

PROFILE = "davinci_200_mc4_hc1"


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(
        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
    )
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_host_role_receives_events_from_an_equipment_simulator():
    port = _free_port()

    equipment = ProfileSimulator(
        settings=create_equipment_settings(
            port=port, device_id=0, address="127.0.0.1"
        ),
        profile_id=PROFILE,
        tool_id="EQ_SIM_01",
        wafer_count=2,
        step_interval_sec=0.2,
        loop_lots=True,
        fire_alarm=True,
    )
    host = HostSimulator(
        settings=_active_settings(port),
        tool_id="HOST_SIM_01",
        profile_id=PROFILE,
    )

    equipment.enable()
    try:
        equipment.start_events()
        host.enable()
        try:
            deadline = time.time() + 30.0
            while time.time() < deadline and host.events_received == 0:
                time.sleep(0.2)
            assert host.events_received > 0, (
                "the host-role simulator received no S6F11 within 30s; the "
                "HSMS handshake or the S2F33/35/37 subscription failed"
            )
            # The subscription is what turns a connected link into a
            # reporting one, so assert it explicitly rather than inferring
            # it from the events above.
            assert host.subscription_ok is True
            assert host.last_event_ceid is not None
            # A CEID the profile does not know would arrive as "unmapped"
            # and prove only that bytes moved, not that they decoded.
            assert host.last_event_name
        finally:
            host.disable()
    finally:
        equipment.disable()


def test_runner_builds_the_host_side_when_role_is_host():
    """The role in the YAML - not the caller - picks which side runs."""
    config = SimulatorConfig(
        connection=ConnectionConfig(
            role="host", mode="active", address="127.0.0.1",
            port=_free_port(), device_id=0,
        ),
        simulation=SimulationConfig(profile=PROFILE, tool_id="HOST_SIM_01"),
        host=HostConfig(request_online=False, enable_alarms=False),
    )

    # Never enabled, so nothing to tear down: construction alone is what
    # proves the dispatch.
    simulator = SimulatorRunner(config)._create_simulator()

    assert isinstance(simulator, HostSimulator)


def test_runner_still_builds_the_equipment_side_by_default():
    config = SimulatorConfig(
        connection=ConnectionConfig(
            mode="passive", address="127.0.0.1",
            port=_free_port(), device_id=0,
        ),
        simulation=SimulationConfig(profile=PROFILE, tool_id="EQ_SIM_01"),
    )
    assert config.connection.role == "equipment"

    simulator = SimulatorRunner(config)._create_simulator()

    assert isinstance(simulator, ProfileSimulator)


def test_host_and_equipment_agree_on_who_listens():
    """The two describe_* sentences must be each other's mirror image."""
    equipment = ConnectionConfig(
        role="equipment", mode="passive", address="0.0.0.0",
        port=5051, device_id=0,
    )
    host = ConnectionConfig(
        role="host", mode="active", address="192.168.1.10",
        port=5051, device_id=0,
    )

    assert "EQUIPMENT" in equipment.describe_self()
    assert "listens" in equipment.describe_self()
    assert "HOST in HSMS ACTIVE mode" in equipment.describe_peer()
    assert "HOST" in host.describe_self()
    assert "dials out" in host.describe_self()
    assert "EQUIPMENT in HSMS PASSIVE mode" in host.describe_peer()


def _active_settings(port: int):
    import secsgem.hsms

    return secsgem.hsms.HsmsSettings(
        address="127.0.0.1",
        port=port,
        connect_mode=secsgem.hsms.HsmsConnectMode.ACTIVE,
        session_id=0,
        t3=10,
        t5=1,
        t6=5,
        t7=10,
        t8=5,
    )


def test_stop_event_stops_a_host_runner_cleanly():
    """Ctrl-C on a host must not hang: the runner owns the lifecycle."""
    stop = threading.Event()
    config = SimulatorConfig(
        connection=ConnectionConfig(
            role="host", mode="active", address="127.0.0.1",
            port=_free_port(), device_id=0,
        ),
        simulation=SimulationConfig(profile=PROFILE, tool_id="HOST_SIM_01"),
        host=HostConfig(request_online=False, enable_alarms=False),
    )
    runner = SimulatorRunner(config, stop_event=stop)

    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    time.sleep(1.0)
    runner.request_stop()
    thread.join(timeout=20.0)

    assert not thread.is_alive()
