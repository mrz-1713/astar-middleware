"""Acceptance tests for the same runner used by the packaged executable."""

from __future__ import annotations

import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("secsgem")

from gateway.host import GatewayHost, create_host_settings
from simulator.config import (
    ConnectionConfig,
    RecoveryConfig,
    SimulationConfig,
    SimulatorConfig,
)
from simulator.runner import EXIT_OK, SimulatorRunner


def _free_port() -> int:
    for _ in range(20):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
        )
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
            probe.close()
            return port
        except OSError:
            probe.close()
    raise RuntimeError("could not reserve a free port")


def _simulator_config(
    mode: str, port: int, hsms_timers: dict[str, int] | None = None
) -> SimulatorConfig:
    return SimulatorConfig(
        connection=ConnectionConfig(
            mode=mode,
            address="127.0.0.1",
            port=port,
            device_id=0,
            hsms_timers=hsms_timers or {},
        ),
        simulation=SimulationConfig(
            tool_id=f"DAV_RUNNER_{mode.upper()}",
            wafer_count=1,
            event_interval_sec=0.02,
            repeat_lots=False,
            emit_alarm=True,
        ),
        recovery=RecoveryConfig(
            initial_retry_sec=1,
            maximum_retry_sec=2,
            maximum_restart_attempts=2,
        ),
        source_path=Path(f"davinci-{mode}.yaml"),
    )


def _simulator_config_with_device_id(
    mode: str, port: int, device_id: int
) -> SimulatorConfig:
    config = _simulator_config(mode, port)
    return SimulatorConfig(
        connection=ConnectionConfig(
            mode=config.connection.mode,
            address=config.connection.address,
            port=config.connection.port,
            device_id=device_id,
        ),
        simulation=config.simulation,
        recovery=config.recovery,
        logging=config.logging,
        source_path=config.source_path,
    )


def _new_host(
    mode: str,
    port: int,
    events: list[tuple[int, dict[str, Any]]],
    connected: list[int],
    alarms: list[dict[str, Any]] | None = None,
):
    settings = create_host_settings(
        host="127.0.0.1",
        port=port,
        device_id=0,
        mode=mode,
        bind_address="127.0.0.1",
    )
    settings.timeouts.t3 = 5
    settings.timeouts.t5 = 1
    settings.timeouts.t6 = 3
    host = GatewayHost(
        settings=settings,
        tool_id=f"HOST_{mode.upper()}",
        on_event=lambda _tool, ceid, data: events.append((ceid, data)),
        on_connect=lambda _tool: connected.append(1),
    )
    if alarms is not None:
        host.set_alarm_callback(lambda _tool, alarm: alarms.append(alarm))
    return host


def _wait(predicate, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize(
    ("simulator_mode", "host_mode"),
    [("active", "passive"), ("passive", "active")],
)
def test_packaged_runner_communicates_in_both_hsms_directions(
    simulator_mode: str,
    host_mode: str,
):
    port = _free_port()
    events: list[tuple[int, dict[str, Any]]] = []
    alarms: list[dict[str, Any]] = []
    connected: list[int] = []
    host = _new_host(host_mode, port, events, connected, alarms)
    runner = SimulatorRunner(_simulator_config(simulator_mode, port))
    result: list[int] = []
    runner_thread = threading.Thread(
        target=lambda: result.append(runner.run())
    )

    try:
        if host_mode == "passive":
            host.enable()
            time.sleep(0.25)
            runner_thread.start()
        else:
            runner_thread.start()
            time.sleep(0.25)
            host.enable()

        assert _wait(
            lambda: bool(connected)
        ), "GEM communication was not established"
        response = host.are_you_there()
        assert response is not None
        assert host.settings.streams_functions.decode(response).get() == [
            "DaVinci200",
            "DaVinci200 Version 4.9.3",
        ]

        required = {3050001, 3140002, 3140003, 3050002}
        assert _wait(
            lambda: required.issubset({ceid for ceid, _ in events})
        ), (
            f"missing DaVinci lifecycle events in {simulator_mode} mode: "
            f"{[ceid for ceid, _ in events]}"
        )
        assert _wait(
            lambda: bool(alarms)
        ), "DaVinci S5F1 alarm was not delivered"
        assert any(alarm.get("alid") == 5010001 for alarm in alarms)
    finally:
        try:
            host.disable()
        except Exception:
            pass
        runner.request_stop()
        if runner_thread.ident is not None:
            runner_thread.join(10)

    assert not runner_thread.is_alive()
    assert result == [EXIT_OK]


def test_active_runner_drains_spool_and_resumes_partial_lot_after_disconnect():
    port = _free_port()
    first_events: list[tuple[int, dict[str, Any]]] = []
    first_connected: list[int] = []
    first_host = _new_host("passive", port, first_events, first_connected)
    # T5 is the HSMS connect-separation timer: secsgem's active loop waits it
    # out between connection attempts, so the shipped default of 10s puts the
    # reconnect below at the edge of any sane test budget. This test is about
    # the spool preserving a partial lot across a reconnect, not about how
    # long connect separation is, so shorten it - which also exercises the
    # per-simulator hsms_timers override.
    runner = SimulatorRunner(_simulator_config("active", port, {"t5": 1}))
    result: list[int] = []
    runner_thread = threading.Thread(
        target=lambda: result.append(runner.run())
    )
    second_host = None

    try:
        first_host.enable()
        time.sleep(0.25)
        runner_thread.start()
        assert _wait(lambda: bool(first_connected))
        assert _wait(lambda: any(ceid == 3050001 for ceid, _ in first_events))

        # Break the link during the first scripted lot, then provide a fresh
        # passive host endpoint for secsgem's active reconnect loop. The tool
        # spool must preserve the partial lot rather than restarting it.
        first_host.disable()
        time.sleep(0.25)

        second_events: list[tuple[int, dict[str, Any]]] = []
        second_connected: list[int] = []
        second_host = _new_host(
            "passive", port, second_events, second_connected
        )
        second_host.enable()

        assert _wait(lambda: bool(second_connected), timeout=20)
        assert second_host.drain_spool(), "reconnected host must request S6F23 recovery"
        assert _wait(lambda: bool(second_events), timeout=15)
        assert second_events[0][0] != 3050001, (
            "spool recovery restarted and duplicated MaterialReceived: "
            f"{[ceid for ceid, _ in second_events]}"
        )
        assert _wait(lambda: any(ceid == 3050002 for ceid, _ in second_events))
        assert sum(
            ceid == 3050001 for ceid, _ in first_events + second_events
        ) == 1
    finally:
        if second_host is not None:
            try:
                second_host.disable()
            except Exception:
                pass
        try:
            first_host.disable()
        except Exception:
            pass
        runner.request_stop()
        runner_thread.join(10)

    assert not runner_thread.is_alive()
    assert result == [EXIT_OK]


def test_mismatched_device_id_never_reaches_gem_communicating():
    port = _free_port()
    events: list[tuple[int, dict[str, Any]]] = []
    connected: list[int] = []
    host = _new_host("passive", port, events, connected)
    runner = SimulatorRunner(
        _simulator_config_with_device_id("active", port, 1)
    )
    result: list[int] = []
    runner_thread = threading.Thread(
        target=lambda: result.append(runner.run())
    )

    try:
        host.enable()
        time.sleep(0.25)
        runner_thread.start()
        time.sleep(2.0)
        assert not connected
        assert not events
    finally:
        try:
            host.disable()
        except Exception:
            pass
        runner.request_stop()
        runner_thread.join(10)

    assert not runner_thread.is_alive()
    assert result == [EXIT_OK]
