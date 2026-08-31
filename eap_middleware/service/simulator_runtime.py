"""In-process simulator runners for `runtime_mode: simulated` machines."""


from __future__ import annotations


import threading

import time


from dataclasses import replace


from typing import (
    TYPE_CHECKING, Optional,
)


from ..models import (
    MachineConfig,
)


# The simulator is a SEPARATE deliverable, shipped as its own standalone exe,
# and the middleware package does not contain it. So `simulator` must never be
# imported at module scope: doing so makes the whole service unimportable on a
# production install, where only the middleware is present.
#
# `from __future__ import annotations` above makes the annotations below strings,
# so TYPE_CHECKING is enough for type checkers and costs nothing at runtime.
# The real imports live in `_start_simulator`, which only runs for a machine
# with `runtime_mode: simulated`.
if TYPE_CHECKING:  # pragma: no cover - imports for type checking only
    from simulator.runner import SimulatorRunner

from .constants import (
    SIMULATOR_MISSING_HINT,
)
from .errors import SimulatorUnavailableError
from .state import ServiceState


class SimulatorMixin(ServiceState):
    """In-process simulator runners for `runtime_mode: simulated` machines."""


    def _stop_simulator(
        self, endpoint_id: str, deadline: Optional[float] = None
    ) -> None:
        simulator = self._simulators.pop(endpoint_id, None)
        if simulator is not None:
            runner, thread = simulator
            runner.request_stop()
            self._join_within(
                thread, deadline, 10.0, f"Simulator for {endpoint_id}"
            )


    @staticmethod
    def _runtime_machine(machine: MachineConfig) -> MachineConfig:
        if not machine.is_simulated:
            return machine
        if machine.is_passive:
            return replace(machine, host="127.0.0.1", hsms_bind_address="127.0.0.1")
        return replace(machine, host="127.0.0.1")


    def _start_simulator(
        self, machine: MachineConfig, subscription_path: Optional[str] = None
    ) -> None:
        # Imported here, not at module scope: the simulator is a separate
        # deliverable and is absent from a middleware-only install. A missing
        # simulator must degrade to one clear message on one machine, not make
        # the whole service unimportable.
        try:
            from simulator.config import (
                ConnectionConfig,
                RecoveryConfig,
                SimulationConfig,
                SimulatorConfig,
                SimulatorLoggingConfig,
            )
            from simulator.runner import SimulatorRunner
        except ImportError as exc:
            raise SimulatorUnavailableError(
                f"{SIMULATOR_MISSING_HINT} (import failed: {exc})"
            ) from exc

        mode = "active" if machine.is_passive else "passive"
        # An embedded simulator only talks to this process. Exposing its
        # passive listener on every NIC adds no capability and widens the lab
        # attack surface, so both directions stay on loopback.
        address = "127.0.0.1"
        simulator = machine.simulator
        definitions = simulator.event_definitions
        dvid_names = definitions.get("dvid_names", {})
        dvid_values = definitions.get("dvid_values", {})
        dvid_types = definitions.get("dvid_types", {})
        svid_types = {
            int(item["svid"]): str(item["type"])
            for item in definitions.get("svids", [])
        }
        config = SimulatorConfig(
            connection=ConnectionConfig(
                mode=mode,
                address=address,
                port=machine.port,
                device_id=machine.secs_device_id,
                # The same timers this machine's host side will run. A
                # loopback pair whose two ends disagree is not a rehearsal of
                # the field wiring - and mismatched timers fail as
                # unexplained link drops, which is exactly what the rig
                # exists to catch before a tool does.
                hsms_timers=dict(machine.hsms_timers),
            ),
            simulation=SimulationConfig(
                profile=machine.machine_profile,
                tool_id=machine.display_name,
                wafer_count=simulator.wafer_count,
                event_interval_sec=simulator.event_interval_sec,
                repeat_lots=simulator.repeat_lots,
                emit_alarm=simulator.emit_alarm,
                alarm_id=simulator.alarm_id,
                alarm_text=simulator.alarm_text,
                mdln=simulator.mdln,
                softrev=simulator.softrev,
                subscription_path=(
                    subscription_path or self._subscription_path_for(machine)
                ),
                ceid_overrides=simulator.ceid_overrides,
                svid_values={int(key): value for key, value in simulator.svid_values.items()},
                svid_types=svid_types,
                dvid_values={
                    str(dvid_names.get(str(key), key)): value
                    for key, value in dvid_values.items()
                },
                dvid_types={
                    str(dvid_names.get(str(key), key)): value
                    for key, value in dvid_types.items()
                },
            ),
            recovery=RecoveryConfig(),
            logging=SimulatorLoggingConfig(directory=str(machine.simulator_log_dir)),
            source_path=machine.simulator_log_dir / "simulator.yaml",
        )
        if simulator.implementation == "davinci_advanced":
            from simulator.secsgem_equipment import SecsGemEquipment

            runner = SimulatorRunner(config, simulator_factory=SecsGemEquipment)
        elif simulator.implementation == "nexgen_advanced":
            from simulator.profile_simulator import nexgen_advanced_factory

            runner = SimulatorRunner(
                config, simulator_factory=nexgen_advanced_factory
            )
        else:
            runner = SimulatorRunner(config)
        thread = threading.Thread(
            target=self._run_simulator,
            args=(machine.endpoint_id, runner),
            name=f"Simulator-{machine.endpoint_id}",
            daemon=True,
        )
        self._simulators[machine.endpoint_id] = (runner, thread)
        self._simulator_exit_codes.pop(machine.endpoint_id, None)
        thread.start()


    def _run_simulator(self, endpoint_id: str, runner: SimulatorRunner) -> None:
        code = runner.run()
        self._simulator_exit_codes[endpoint_id] = code
        if self._running and code:
            self._set_runtime_state(
                endpoint_id, "Error", f"simulator exited with code {code}"
            )


    def _set_runtime_state(
        self, endpoint_id: str, state: str, error: str = ""
    ) -> None:
        current = self._runtime_states.setdefault(endpoint_id, {})
        current.update({"state": state, "last_transition": time.time()})
        if error:
            current["last_error"] = error


    def _simulator_status(self, endpoint_id: str) -> str:
        """Simulator state for the status snapshot.

        Read via .get() so a machine restart that pops _simulators between
        the membership test and the index can never raise KeyError here.
        """
        entry = self._simulators.get(endpoint_id)
        if entry is None:
            return "Stopped"
        _runner, thread = entry
        if thread.is_alive():
            return "Running"
        code = self._simulator_exit_codes.get(endpoint_id)
        return f"Error ({code})" if code else "Stopped"
