from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import secsgem.hsms
import yaml
from secsgem.gem.communication_state_machine import CommunicationState

from gateway.host import DEFAULT_HSMS_TIMERS

import simulator.runner as runner_module
from simulator.config import (
    ConnectionConfig,
    RecoveryConfig,
    SimulationConfig,
    SimulatorConfig,
)
from simulator.runner import (
    EXIT_OK,
    EXIT_RETRIES_EXHAUSTED,
    EXIT_STARTUP,
    SimulatorRunner,
)


class _Event:
    def __iadd__(self, callback):
        del callback
        return self


class _Events:
    connected = _Event()
    disconnected = _Event()
    handler_communicating = _Event()


class _Protocol:
    events = _Events()


class _FakeSimulator:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.settings = kwargs["settings"]
        self.communication_state = SimpleNamespace(
            current=CommunicationState.NOT_COMMUNICATING
        )
        self.protocol = _Protocol()
        self.events = _Events()
        self.enabled = threading.Event()
        self.events_started = False
        self.disabled = False

    def enable(self):
        self.enabled.set()

    def start_events(self):
        self.events_started = True

    def disable(self):
        self.disabled = True


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class _AdvancingStopEvent(threading.Event):
    def __init__(self, clock: _Clock):
        super().__init__()
        self.clock = clock

    def wait(self, timeout=None):
        if not self.is_set() and timeout is not None:
            self.clock.now += timeout
        return self.is_set()


def _config(
    mode: str,
    address: str,
    port: int,
    maximum_restart_attempts: int = 1,
) -> SimulatorConfig:
    return SimulatorConfig(
        connection=ConnectionConfig(
            mode=mode, address=address, port=port, device_id=7
        ),
        simulation=SimulationConfig(event_interval_sec=0.01),
        recovery=RecoveryConfig(
            initial_retry_sec=1,
            maximum_retry_sec=2,
            maximum_restart_attempts=maximum_restart_attempts,
        ),
        source_path=Path("davinci.yaml"),
    )


def test_build_settings_maps_active_mode_and_device_id():
    settings = SimulatorRunner(
        _config("active", "127.0.0.1", 5050)
    ).build_settings()
    assert settings.connect_mode == secsgem.hsms.HsmsConnectMode.ACTIVE
    assert settings.address == "127.0.0.1"
    assert settings.port == 5050
    assert settings.session_id == 7


def test_build_settings_uses_the_shipped_hsms_timers_not_the_retry_backoff():
    """T5 is a protocol timer, not the runner's restart interval.

    They used to be the same value: build_settings passed
    recovery.initial_retry_sec as t5, so tuning how fast a crashed simulator
    came back silently retuned HSMS connect-separation - and its default of
    1 s is below anything any vendor manual states.
    """
    config = _config("active", "127.0.0.1", 5050)
    assert config.recovery.initial_retry_sec == 1
    settings = SimulatorRunner(config).build_settings()
    for name, expected in DEFAULT_HSMS_TIMERS.items():
        assert getattr(settings.timeouts, name) == expected


def test_build_settings_honours_configured_hsms_timers():
    """The simulator stands in for the tool, so it must be able to run the
    tool's own timers. secsgem's defaults differ from every shipped profile
    (T7 is 8 s against the profiles' 10 or 5), so a rig left on the library
    defaults was permanently mismatched and could never reproduce the fault
    a real timer mismatch causes."""
    spts = {"t3": 30, "t5": 5, "t6": 10, "t7": 5, "t8": 6}
    config = SimulatorConfig(
        connection=ConnectionConfig(
            mode="active", address="127.0.0.1", port=5050, device_id=7,
            hsms_timers=spts,
        ),
        simulation=SimulationConfig(event_interval_sec=0.01),
        recovery=RecoveryConfig(initial_retry_sec=1, maximum_retry_sec=2),
        source_path=Path("davinci.yaml"),
    )
    settings = SimulatorRunner(config).build_settings()
    for name, expected in spts.items():
        assert getattr(settings.timeouts, name) == expected


def test_service_simulator_mirrors_the_machines_own_timers():
    """A loopback pair whose two ends disagree is not a rehearsal of the
    field wiring. The in-process simulator the service starts must run the
    same timers the host side of that machine runs."""
    from eap_middleware.config import service_config_from_dict

    raw = yaml.safe_load(Path("config/production.yaml").read_text())
    for index, machine in enumerate(raw["machines"]):
        machine.update(
            enabled=True, runtime_mode="simulated", host="127.0.0.1",
            port=5100 + index, offline_test_mode=True,
        )
    config = service_config_from_dict(raw)
    by_profile = {m.machine_profile: m.hsms_timers for m in config.machines}
    # The SPTS timers come from its own manual (Table 3) and differ from the
    # other three, so this pins that they are carried through per machine
    # rather than collapsed to one shipped default.
    assert by_profile["spts_fxp_omega"] == {
        "t3": 30, "t5": 5, "t6": 10, "t7": 5, "t8": 6
    }
    for machine in config.machines:
        runner = SimulatorRunner(
            SimulatorConfig(
                connection=ConnectionConfig(
                    mode="passive", address="0.0.0.0", port=machine.port,
                    device_id=machine.secs_device_id,
                    hsms_timers=dict(machine.hsms_timers),
                ),
                simulation=SimulationConfig(profile=machine.machine_profile),
                recovery=RecoveryConfig(),
                source_path=Path("sim.yaml"),
            )
        )
        assert runner.resolved_hsms_timers() == dict(machine.hsms_timers)


def test_build_settings_maps_passive_mode():
    settings = SimulatorRunner(
        _config("passive", "0.0.0.0", 5050)
    ).build_settings()
    assert settings.connect_mode == secsgem.hsms.HsmsConnectMode.PASSIVE


def test_passive_port_conflict_returns_startup_error():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    try:
        runner = SimulatorRunner(_config("passive", "127.0.0.1", port))
        assert runner.run() == EXIT_STARTUP
    finally:
        listener.close()


def test_runner_starts_and_stops_exactly_one_simulator():
    created = []
    stop = threading.Event()

    def factory(**kwargs):
        simulator = _FakeSimulator(**kwargs)
        created.append(simulator)
        return simulator

    runner = SimulatorRunner(
        _config("active", "127.0.0.1", 5050),
        simulator_factory=factory,
        stop_event=stop,
    )
    result = []
    thread = threading.Thread(target=lambda: result.append(runner.run()))
    thread.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not created:
            time.sleep(0.01)
        assert created and created[0].enabled.wait(1)
        assert created[0].events_started is True
    finally:
        runner.request_stop()
        thread.join(2)

    assert not thread.is_alive()
    assert result == [EXIT_OK]
    assert len(created) == 1
    assert created[0].disabled is True


def test_active_connection_retry_limit_stops_after_retry(monkeypatch):
    created = []
    clock = _Clock()
    stop = _AdvancingStopEvent(clock)
    monkeypatch.setattr(runner_module.time, "monotonic", clock.monotonic)

    def factory(**kwargs):
        simulator = _FakeSimulator(**kwargs)
        created.append(simulator)
        return simulator

    runner = SimulatorRunner(
        _config(
            "active",
            "127.0.0.1",
            5050,
            maximum_restart_attempts=2,
        ),
        simulator_factory=factory,
        stop_event=stop,
    )
    result = runner.run()

    assert result == EXIT_RETRIES_EXHAUSTED
    assert clock.now == 3.0
    assert len(created) == 1
    # T5 is the HSMS connect-separation timer and is set once, in
    # build_settings(), from the profile timer table. It used to be rewritten
    # here with the runner's own escalating restart backoff (1s, then 2s),
    # so retuning how fast the process restarts silently retuned a protocol
    # timer. Retrying must leave it exactly where build_settings() put it.
    assert created[0].settings.timeouts.t5 == runner.resolved_hsms_timers()["t5"]
    assert created[0].disabled is True


def test_stop_request_is_idempotent():
    runner = SimulatorRunner(_config("active", "127.0.0.1", 5050))
    runner.request_stop()
    runner.request_stop()
    assert runner.run() == EXIT_OK
