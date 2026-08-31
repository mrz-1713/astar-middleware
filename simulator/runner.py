"""Supervised runtime for the DaVinci equipment simulator."""

from __future__ import annotations

import logging
import signal
import socket
import threading
import time
from types import FrameType
from typing import Any, Callable, Dict, Optional

import secsgem.hsms
from secsgem.gem.communication_state_machine import CommunicationState

from .config import SimulatorConfig
from .profile_simulator import ProfileSimulator

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_CONFIG = 2
EXIT_STARTUP = 3
EXIT_RETRIES_EXHAUSTED = 4


class SimulatorStartupError(RuntimeError):
    """Raised when the simulator cannot safely start."""


# Either role: the host simulator deliberately exposes the same surface
# (enable/disable/start_events/communication_state/protocol/events).
SimulatorFactory = Callable[..., Any]


class SimulatorRunner:
    """Own one simulator instance and its process-level lifecycle.

    Supervises either SECS/GEM role. `connection.role` decides which side
    is built; everything below it - preflight, retry, backoff, signal
    handling - is transport-level and identical for both.
    """

    def __init__(
        self,
        config: SimulatorConfig,
        simulator_factory: Optional[SimulatorFactory] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        self.config = config
        # Kept unresolved: the default depends on connection.role, and an
        # injected factory must still win over both defaults.
        self._simulator_factory = simulator_factory
        self._stop_event = stop_event or threading.Event()
        self._simulator: Optional[Any] = None
        self._tcp_connected = threading.Event()
        self._connection_attempt_windows = 0
        self._previous_signal_handlers: dict[int, Any] = {}

    def request_stop(self) -> None:
        self._stop_event.set()

    @property
    def simulator(self) -> Optional[Any]:
        """The live instance, or None while stopped or between restarts.

        The control panel polls this for link state and, on the host side,
        for the received-event counters.
        """
        return self._simulator

    def build_settings(self) -> secsgem.hsms.HsmsSettings:
        connect_mode = (
            secsgem.hsms.HsmsConnectMode.ACTIVE
            if self.config.connection.mode == "active"
            else secsgem.hsms.HsmsConnectMode.PASSIVE
        )
        # All five HSMS timers are set explicitly, from the same table the
        # middleware host uses. Two reasons this is not left to the library:
        #
        # 1. The simulator stands in for the tool, and the tool is the side
        #    the host's timers have to match. secsgem's defaults differ from
        #    every shipped profile (T7 is 8 s against the profiles' 10 or 5),
        #    so the rig ran permanently mismatched and a real timer mismatch
        #    - which shows up only as unexplained link drops - was the one
        #    fault it could not reproduce.
        # 2. T5 used to be wired to recovery.initial_retry_sec, conflating
        #    the HSMS connect-separation timer with the runner's own restart
        #    backoff. Tuning how fast the process restarts silently retuned a
        #    protocol timer, and its default of 1 s is below anything any
        #    vendor manual states.
        return secsgem.hsms.HsmsSettings(
            address=self.config.connection.address,
            port=self.config.connection.port,
            connect_mode=connect_mode,
            session_id=self.config.connection.device_id,
            **self.resolved_hsms_timers(),
        )

    def resolved_hsms_timers(self) -> dict[str, int]:
        """The timers this simulator will actually run, defaults filled in."""
        from gateway.host import DEFAULT_HSMS_TIMERS

        timers = dict(DEFAULT_HSMS_TIMERS)
        timers.update(self.config.connection.hsms_timers or {})
        return timers

    def _preflight_passive_listener(self) -> None:
        if self.config.connection.mode != "passive":
            return
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(
                (self.config.connection.address, self.config.connection.port)
            )
        except OSError as exc:
            endpoint = f"{self.config.connection.address}:{self.config.connection.port}"
            raise SimulatorStartupError(
                f"cannot listen on {endpoint}; the address is unavailable or the port is in use"
            ) from exc
        finally:
            probe.close()

    def _resolve_factory(self) -> SimulatorFactory:
        if self._simulator_factory is not None:
            return self._simulator_factory
        if self.config.connection.is_host:
            # Imported here so the equipment path never pulls in the host
            # stack (gateway.host and its E40/subscription machinery).
            from .host_simulator import HostSimulator

            return HostSimulator
        return ProfileSimulator

    def _host_kwargs(self) -> dict[str, Any]:
        simulation = self.config.simulation
        host = self.config.host
        return {
            "settings": self.build_settings(),
            "tool_id": simulation.tool_id,
            "profile_id": simulation.profile,
            "subscription_path": simulation.subscription_path,
            "request_online": host.request_online,
            "enable_alarms": host.enable_alarms,
            "drain_spool": host.drain_spool,
            "read_identity": host.read_identity,
        }

    def _create_simulator(self) -> Any:
        factory = self._resolve_factory()
        simulation = self.config.simulation
        if self.config.connection.is_host:
            kwargs = self._host_kwargs()
        else:
            kwargs: Dict[str, Any] = {
                "settings": self.build_settings(),
                "tool_id": simulation.tool_id,
                "wafer_count": simulation.wafer_count,
                "step_interval_sec": simulation.event_interval_sec,
                "fire_alarm": simulation.emit_alarm,
                "loop_lots": simulation.repeat_lots,
            }
            if factory is ProfileSimulator:
                # Only the profile-driven simulator understands these; an
                # injected factory (tests, a vendor-specific sim) keeps the
                # old signature.
                kwargs["profile_id"] = simulation.profile
                kwargs["ceid_overrides"] = simulation.ceid_overrides
                kwargs["svid_values"] = simulation.svid_values
                kwargs["svid_types"] = simulation.svid_types
                kwargs["dvid_values"] = simulation.dvid_values
                kwargs["dvid_types"] = simulation.dvid_types
                kwargs["mdln"] = simulation.mdln
                kwargs["softrev"] = simulation.softrev
                kwargs["alarm_id"] = simulation.alarm_id
                kwargs["alarm_text"] = simulation.alarm_text
                kwargs["subscription_path"] = simulation.subscription_path
        simulator = factory(**kwargs)
        protocol_events: Any = simulator.protocol.events
        handler_events: Any = simulator.events
        protocol_events.connected += self._on_tcp_connected
        protocol_events.disconnected += self._on_tcp_disconnected
        handler_events.handler_communicating += self._on_gem_communicating
        return simulator

    def _on_tcp_connected(self, _data: dict[str, Any]) -> None:
        self._tcp_connected.set()
        self._connection_attempt_windows = 0
        logger.info("TCP connected")

    def _on_tcp_disconnected(self, _data: dict[str, Any]) -> None:
        self._tcp_connected.clear()
        if not self._stop_event.is_set():
            logger.warning(
                "Connection lost; event emission paused until GEM reconnects"
            )

    def _on_gem_communicating(self, _data: dict[str, Any]) -> None:
        self._tcp_connected.set()
        self._connection_attempt_windows = 0
        logger.info("HSMS selected; GEM communication established")

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        supported = [signal.SIGINT, signal.SIGTERM]
        sigbreak = getattr(signal, "SIGBREAK", None)
        if sigbreak is not None:
            supported.append(sigbreak)
        for signum in supported:
            self._previous_signal_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handle_signal)

    def _restore_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for signum, previous in self._previous_signal_handlers.items():
            signal.signal(signum, previous)
        self._previous_signal_handlers.clear()

    def _handle_signal(self, signum: int, _frame: Optional[FrameType]) -> None:
        logger.info("Stop requested by signal %s", signum)
        self.request_stop()

    def _serve_once(self) -> int:
        self._preflight_passive_listener()
        simulator = self._create_simulator()
        self._simulator = simulator
        connection = self.config.connection
        endpoint = connection.endpoint
        # Both lines are logged verbatim on every start: the role/mode pair
        # is the setting operators get wrong, and the log is what they send
        # when the link stays down.
        logger.info("Starting simulator. %s", connection.describe_self())
        logger.info("%s", connection.describe_peer())
        try:
            self._tcp_connected.clear()
            self._connection_attempt_windows = 0
            simulator.enable()
            simulator.start_events()
            wait_log_delay = self.config.recovery.initial_retry_sec
            next_wait_log = time.monotonic() + wait_log_delay
            while not self._stop_event.wait(0.25):
                now = time.monotonic()
                communicating = (
                    simulator.communication_state.current
                    == CommunicationState.COMMUNICATING
                )
                if communicating:
                    wait_log_delay = self.config.recovery.initial_retry_sec
                    next_wait_log = now + wait_log_delay
                    continue
                if now >= next_wait_log:
                    if (
                        self.config.connection.mode == "active"
                        and not self._tcp_connected.is_set()
                    ):
                        self._connection_attempt_windows += 1
                        limit = self.config.recovery.maximum_restart_attempts
                        if limit and self._connection_attempt_windows >= limit:
                            logger.error(
                                "Connection retry limit exhausted after %s attempt(s)",
                                self._connection_attempt_windows,
                            )
                            return EXIT_RETRIES_EXHAUSTED
                    logger.warning(
                        "Waiting for GEM communication as %s at %s (HSMS %s)",
                        connection.role.upper(),
                        endpoint,
                        connection.mode.upper(),
                    )
                    wait_log_delay = min(
                        wait_log_delay * 2,
                        self.config.recovery.maximum_retry_sec,
                    )
                    # wait_log_delay only paces the wait-log above. The actual
                    # HSMS T5 connect-separation timer is set once, in
                    # build_settings(), from resolved_hsms_timers(); retuning
                    # it here with the runner's own restart backoff was exactly
                    # the timer/backoff conflation build_settings() removed.
                    next_wait_log = time.monotonic() + wait_log_delay
            return EXIT_OK
        finally:
            logger.info(
                "Stopping %s simulator", connection.role.upper()
            )
            try:
                simulator.disable()
            finally:
                self._simulator = None

    def run(self) -> int:
        self._install_signal_handlers()
        restart_attempt = 0
        retry_delay = self.config.recovery.initial_retry_sec
        try:
            while not self._stop_event.is_set():
                try:
                    return self._serve_once()
                except SimulatorStartupError as exc:
                    logger.error("%s", exc)
                    return EXIT_STARTUP
                except Exception:
                    logger.exception("Unexpected simulator failure")
                    restart_attempt += 1
                    limit = self.config.recovery.maximum_restart_attempts
                    if limit and restart_attempt >= limit:
                        logger.error(
                            "Restart limit exhausted after %s attempt(s)",
                            restart_attempt,
                        )
                        return EXIT_RETRIES_EXHAUSTED
                    logger.warning(
                        "Restarting simulator in %s second(s)", retry_delay
                    )
                    if self._stop_event.wait(retry_delay):
                        return EXIT_OK
                    retry_delay = min(
                        retry_delay * 2, self.config.recovery.maximum_retry_sec
                    )
            return EXIT_OK
        finally:
            self._restore_signal_handlers()
