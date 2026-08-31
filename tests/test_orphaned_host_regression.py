"""Regressions for the two-VM NexGen MG failure of 2026-08-19.

Symptom on the rig: `middleware.log` repeated

    [WARNING] Reconnect watchdog: TOOL_04 is disconnected, restarting session.

every 30-60 s for forty minutes, while S6F11 events and S5F1 alarms kept
arriving and being acknowledged the whole time. The equipment's own capture
showed the give-away: across every one of those "restarts" the tool's system
bytes advanced by exactly one (…ac8f -> …ac90) and no Select.req or S1F13 ever
appeared. The TCP connection was never broken, and the middleware's own
host - the one `SecsMachineSession.host` pointed at - had never established
anything. In five and a half minutes the only thing the middleware put on that
socket was a linktest every 27 s: no S2F33/35/37, no S1F3.

What produces that: a GatewayHost replaced by a reconnect but left connected.
It is unreachable through the session, so nothing subscribes or polls on it,
yet secsgem's threads keep it alive; it goes on acknowledging events into the
pipeline; and because HSMS equipment serves exactly one peer it holds the only
slot, so every replacement host connects to a closed door forever.

These tests pin the three things that make that state impossible to reach
silently: the host is really retired, a retired host is inert, and the
watchdog explains an outage instead of repeating one line.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from types import SimpleNamespace

import pytest
import secsgem.hsms

from eap_middleware.models import MachineConfig
from eap_middleware.secs_runtime import SecsMachineSession
from gateway.host import GatewayHost, create_host_settings


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _host(port: int) -> GatewayHost:
    return GatewayHost(
        settings=create_host_settings("127.0.0.1", port, device_id=0, mode="active"),
        tool_id="TOOL_04",
    )


# ----- a retired host cannot touch the pipeline -----

def test_retired_host_drops_messages_instead_of_acknowledging_them():
    """The orphan kept answering S6F12/S5F2 for a session that was gone.

    Acknowledging is the harmful half: it tells the tool the event was
    delivered, so the tool discards it, while nothing downstream ever stored
    it. Staying silent makes the tool retransmit to the connection the service
    actually owns.
    """
    delivered = []
    host = _host(_free_port())
    host._on_event = lambda *args: delivered.append(args)
    host._on_alarm = lambda *args: delivered.append(args)

    host.retire()

    message = SimpleNamespace(
        header=SimpleNamespace(session_id=0, stream=6, function=11, system=1),
        data=b"",
    )
    host._on_message_received({"message": message})

    assert delivered == []
    assert host._retired is True
    assert host.is_connected is False


def test_retire_detaches_every_callback():
    """Detached before disable(), because disable() can block: a message
    already sitting in secsgem's dispatcher queue would otherwise still be
    delivered into a torn-down session."""
    host = _host(_free_port())
    host._on_event = lambda *a: None
    host._on_alarm = lambda *a: None
    host._on_connect = lambda *a: None
    host._on_disconnect = lambda *a: None

    host.retire()

    assert host._on_event is None
    assert host._on_alarm is None
    assert host._on_connect is None
    assert host._on_disconnect is None


def test_retire_closes_a_socket_that_disable_left_open(monkeypatch, caplog):
    """secsgem 0.3.0's TcpConnection.disconnect() returns without closing
    anything when its receiver thread is not running, and
    TcpClientConnection.disable() is a no-op when `enabled` is already False.
    Either path leaves the tool's single peer slot occupied, so retire() must
    verify rather than trust it."""
    host = _host(_free_port())

    left_open, _peer = socket.socketpair()
    connection = SimpleNamespace(_sock=left_open, _connected=True)
    monkeypatch.setattr(host, "_protocol", SimpleNamespace(_connection=connection))
    monkeypatch.setattr(host, "disable", lambda: None)  # the no-op disable()

    with caplog.at_level(logging.WARNING, logger="gateway.host"):
        host.retire()

    assert left_open.fileno() == -1, "retire() left the HSMS socket open"
    assert connection._connected is False
    assert any("still held its HSMS socket" in r.message for r in caplog.records)


def test_retire_survives_a_disable_that_raises(monkeypatch, caplog):
    """A stop that throws used to abandon the host mid-teardown. retire() has
    to finish the job anyway - that is the whole point of it."""
    host = _host(_free_port())

    left_open, _peer = socket.socketpair()
    connection = SimpleNamespace(_sock=left_open, _connected=True)
    monkeypatch.setattr(host, "_protocol", SimpleNamespace(_connection=connection))

    def boom():
        raise OSError("secsgem disable blew up")

    monkeypatch.setattr(host, "disable", boom)

    with caplog.at_level(logging.WARNING, logger="gateway.host"):
        host.retire()  # must not raise

    assert host._retired is True
    assert left_open.fileno() == -1


# ----- the session hands its host to retire(), not disable() -----

def test_session_stop_retires_the_host():
    machine = MachineConfig(
        endpoint_id="TOOL_04",
        display_name="NEXGEN_MG_01",
        machine_profile="nexgen_mg_series",
        host="127.0.0.1",
        port=_free_port(),
    )
    session = SecsMachineSession(
        machine=machine,
        event_callback=lambda *a: None,
        alarm_callback=lambda *a: None,
        connect_callback=lambda *a: None,
        disconnect_callback=lambda *a: None,
    )
    retired = []
    session.host = SimpleNamespace(retire=lambda: retired.append(True))

    session.stop()

    assert retired == [True], "stop() must retire the host, not merely disable it"


# ----- a live connection is really released -----

@pytest.mark.parametrize("cycle", [1, 2])
def test_stopping_a_live_session_frees_the_equipment_connection(cycle):
    """End to end against a real passive peer.

    HSMS equipment accepts one peer at a time. If stop() leaves the socket
    open the listener never comes back, which is exactly why the rig could
    restart forever without reconnecting once.
    """
    from simulator.nexgen_mg_simulator import NexGenMgSimulator

    port = _free_port()
    equipment = NexGenMgSimulator(
        settings=secsgem.hsms.HsmsSettings(
            address="127.0.0.1",
            port=port,
            connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
            session_id=0,
        ),
        tool_id="MG_SIM",
        wafers_per_lot=1,
        step_interval_sec=0.05,
        loop_lots=False,
    )
    equipment.enable()
    # Threads already alive before this session exists. Six test files build a
    # machine called TOOL_04, so every one of them produces threads named
    # "Provision-TOOL_04" - a bare name scan at the end of this test would
    # flag a worker belonging to a completely different test (the MG loopback
    # rig subscribes in 31 bands, which can outlast its own 10s join under
    # suite load). Identity, not name, is what distinguishes ours.
    pre_existing = {thread.ident for thread in threading.enumerate()}
    machine = MachineConfig(
        endpoint_id="TOOL_04",
        display_name="NEXGEN_MG_01",
        machine_profile="nexgen_mg_series",
        host="127.0.0.1",
        port=port,
        event_subscription_enabled=False,
    )
    session = SecsMachineSession(
        machine=machine,
        event_callback=lambda *a: None,
        alarm_callback=lambda *a: None,
        connect_callback=lambda *a: None,
        disconnect_callback=lambda *a: None,
    )
    try:
        for _ in range(cycle):
            session.start()
            deadline = time.time() + 15
            while time.time() < deadline and not session.host.is_connected:
                time.sleep(0.05)
            assert session.host.is_connected, "host never established"
            orphan = session.host
            session.stop()
            assert orphan._retired is True
            connection = getattr(orphan.protocol, "_connection", None)
            sock = getattr(connection, "_sock", None)
            assert sock is None or sock.fileno() == -1, (
                "the stopped host still holds the equipment's only HSMS slot"
            )
    finally:
        session.stop()
        equipment.disable()
        # No provisioning or receiver thread may outlive the session: a leaked
        # one goes on issuing SECS round-trips against a torn-down connection.
        leaked = [
            thread.name for thread in threading.enumerate()
            if thread.name.startswith("Provision-TOOL_04")
            and thread.ident not in pre_existing
        ]
        assert leaked == [], (
            f"provisioning threads outlived the session: {leaked}"
        )
