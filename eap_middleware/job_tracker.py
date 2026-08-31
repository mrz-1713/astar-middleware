"""Per-machine in-memory tracking of which load port owns the active job.

When a PM-chamber event fires (S6F11 from PM1/ProcessingStarted, RecipeEnd,
etc.) the report payload typically does NOT name a load port - the chamber
just says "I started processing a wafer." The mapper still needs to know
which carrier on which LP that wafer came from so the per-lot CSV doesn't
mix LP1 and LP2 wafers into one file.

This module watches the carrier/job lifecycle events (LP1/CarrierArrived,
ControlJob:Selected-Executing, etc.) and remembers the association so
mapper.from_secs_event() can ask "what's the active LP on machine X right
now?" and get the right answer.

State is in-memory only - on middleware restart we lose all associations and
re-build them as the next CarrierArrival flows through. Spec accepts a brief
post-restart "NA" routing window in exchange for not owning a persistence
layer.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from .profiles import (
    TRANSITION_CTRL_JOB_END,
    TRANSITION_CTRL_JOB_START,
    TRANSITION_LP_ACTIVATE_1,
    TRANSITION_LP_ACTIVATE_2,
    TRANSITION_LP_ACTIVATE_FROM_PAYLOAD,
    TRANSITION_LP_DEACTIVATE_1,
    TRANSITION_LP_DEACTIVATE_2,
    TRANSITION_LP_DEACTIVATE_FROM_PAYLOAD,
    MachineProfile,
)

logger = logging.getLogger(__name__)


# Per-machine ceilings on the identifier maps. They are pruned when a load port
# deactivates - once per carrier on a healthy tool - but that depends on the
# deactivate CEID actually arriving. `ptiq_secsgem` declares no state
# transitions at all, and on the other profiles the deactivate CEIDs sit in the
# per-load-port subscription bands, so a refused band also stops the pruning.
# Unbounded, either case grows for the life of the process.
#
# The caps sit far above one carrier's worth of identifiers (a 25-slot cassette
# contributes 25 wafers and one lot), so a tool that deactivates normally never
# reaches them.
MAX_WAFER_IDS = 5000
MAX_LOT_IDS = 500
MAX_CTRL_JOBS = 500


def _bounded_put(
    mapping: "OrderedDict[str, str]", key: str, value: str, cap: int
) -> None:
    """Insert most-recently-used-last, discarding the oldest past `cap`."""
    if key in mapping:
        mapping.move_to_end(key)
    mapping[key] = value
    while len(mapping) > cap:
        mapping.popitem(last=False)


@dataclass
class _MachineState:
    """Per-machine load-port and control-job state.

    Lets a chamber event be attributed to the load port that owns it without
    guessing between concurrently active ports.
    """

    # Activation order is retained for diagnostics, but it is never used to
    # guess between multiple concurrently active ports.
    lp_history: List[str] = field(default_factory=list)
    # CtrlJobID -> LP. When a chamber event carries a CtrlJobID in its V[]
    # payload, this map disambiguates which LP it belongs to even when
    # multiple jobs are active concurrently.
    #
    # Ordered so the oldest entry is the one evicted at the cap.
    ctrl_jobs: "OrderedDict[str, str]" = field(default_factory=OrderedDict)
    wafer_ports: "OrderedDict[str, str]" = field(default_factory=OrderedDict)
    lot_ports: "OrderedDict[str, str]" = field(default_factory=OrderedDict)
    # Chamber -> LP for the wafer that chamber is working on right now. A
    # multi-chamber tool interleaves two lots from two load ports, so "which
    # port is active on this machine" is the wrong question for a PM event;
    # "which port did THIS chamber take its wafer from" is the right one, and
    # the tool states it on every wafer-level PM report.
    chamber_ports: Dict[str, str] = field(default_factory=dict)

    @property
    def active_lp(self) -> Optional[str]:
        active = list(dict.fromkeys(self.lp_history))
        return active[0] if len(active) == 1 else None

    def activate(self, lp: str) -> None:
        if lp not in self.lp_history:
            self.lp_history.append(lp)

    def deactivate(self, lp: str) -> None:
        self.lp_history[:] = [item for item in self.lp_history if item != lp]
        self.ctrl_jobs = OrderedDict(
            (job, port) for job, port in self.ctrl_jobs.items() if port != lp
        )
        self.wafer_ports = OrderedDict(
            (wafer, port) for wafer, port in self.wafer_ports.items() if port != lp
        )
        self.lot_ports = OrderedDict(
            (lot, port) for lot, port in self.lot_ports.items() if port != lp
        )
        # A carrier that has left cannot still own a chamber. Without this a
        # stray PM event after unload would be attributed to a port that no
        # longer holds a cassette.
        self.chamber_ports = {
            chamber: port
            for chamber, port in self.chamber_ports.items()
            if port != lp
        }


class JobTracker:
    """Thread-safe per-machine job/carrier state. One instance shared across
    all machines in the service."""

    def __init__(self) -> None:
        self._states: Dict[str, _MachineState] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._registry_lock = threading.Lock()

    def _state(self, machine_id: str) -> _MachineState:
        with self._registry_lock:
            state = self._states.get(machine_id)
            if state is None:
                state = _MachineState()
                self._states[machine_id] = state
                self._locks[machine_id] = threading.Lock()
            return state

    def _lock(self, machine_id: str) -> threading.Lock:
        # _state() ensures the lock exists; safe to read after.
        self._state(machine_id)
        return self._locks[machine_id]

    def note_event(
        self,
        machine_id: str,
        profile: MachineProfile,
        ceid: int,
        data: Mapping[str, Any],
    ) -> None:
        """Update tracker state based on the event's profile transition tag.
        Always safe to call - unknown CEIDs are a no-op."""
        transition = None  # the handler below logs it; must always be bound
        try:
            transition = profile.ceid_state_transitions.get(ceid)
            if transition:
                self._apply_transition(machine_id, transition, data)
            resolved_port = self._extract_port_id(data) or profile.ceid_load_port.get(
                ceid, ""
            )
            if resolved_port:
                self.note_resolution(machine_id, data, resolved_port)
                # The wafer-level PM reports (pmNWaferStarted/Finished,
                # pmNStepFinished) state the chamber in the CEID and the load
                # port in the payload. That pairing is the only place the two
                # are stated together, so it is what later step events - which
                # name the chamber and nothing else - are resolved against.
                #
                # Learn from the payload first, falling back to the profile's
                # CEID->chamber map, so the tracker records the same chamber
                # name the mapper later passes to lookup_lp(). Before this, a
                # CEID whose payload named a chamber but whose profile map had
                # no entry left that chamber unlearned - lookup_lp() then saw
                # a "named" chamber it had never seen and returned None instead
                # of the active port.
                chamber = self._extract_chamber(data) or profile.ceid_chamber.get(
                    ceid, ""
                )
                if chamber:
                    self.note_chamber(machine_id, chamber, resolved_port)
        except Exception:
            # A tracker bug must NEVER block event publishing. Log + carry on.
            logger.exception(
                "JobTracker transition %r failed for machine=%s ceid=%s",
                transition, machine_id, ceid,
            )

    def _apply_transition(
        self,
        machine_id: str,
        transition: str,
        data: Mapping[str, Any],
    ) -> None:
        state = self._state(machine_id)
        with self._lock(machine_id):
            if transition == TRANSITION_LP_ACTIVATE_1:
                state.activate("1")
            elif transition == TRANSITION_LP_ACTIVATE_2:
                state.activate("2")
            elif transition == TRANSITION_LP_DEACTIVATE_1:
                state.deactivate("1")
            elif transition == TRANSITION_LP_DEACTIVATE_2:
                state.deactivate("2")
            elif transition == TRANSITION_LP_ACTIVATE_FROM_PAYLOAD:
                lp = self._extract_port_id(data)
                if lp:
                    state.activate(lp)
            elif transition == TRANSITION_LP_DEACTIVATE_FROM_PAYLOAD:
                lp = self._extract_port_id(data)
                if lp:
                    state.deactivate(lp)
            elif transition == TRANSITION_CTRL_JOB_START:
                ctrl_id = self._extract_ctrl_job_id(data)
                lp = self._extract_port_id(data) or state.active_lp
                if ctrl_id and lp is not None:
                    _bounded_put(state.ctrl_jobs, ctrl_id, lp, MAX_CTRL_JOBS)
            elif transition == TRANSITION_CTRL_JOB_END:
                ctrl_id = self._extract_ctrl_job_id(data)
                if ctrl_id:
                    state.ctrl_jobs.pop(ctrl_id, None)
            # What the middleware CONCLUDED from the event, which is not the
            # same thing as the event itself and was the one step of the chain
            # the log never showed: an operator could see the S6F11 arrive and
            # the CSV row appear, with no way to tell which load port the
            # middleware thought was active in between.
            logger.info(
                "[%s] state %s -> active LP=%s, control jobs=%s",
                machine_id,
                transition,
                state.active_lp,
                sorted(state.ctrl_jobs) or "none",
            )

    @staticmethod
    def _extract_ctrl_job_id(data: Mapping[str, Any]) -> str:
        for key in ("CtrlJobID", "ControlJobID", "CJobID"):
            value = data.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    @staticmethod
    def _extract_port_id(data: Mapping[str, Any]) -> str:
        """Read the load port from a payload that carries PortID as a DV.
        DaVinci uses DV 2150002 -> 'PortID' (after profile decoding)."""
        for key in (
            "_resolved_load_port", "PortID", "PORT_ID", "LoadPort", "LOAD_PORT"
        ):
            value = data.get(key)
            if value is None:
                continue
            # PortID may arrive as int (1), str ("1"), or short list (["1"]).
            if isinstance(value, (list, tuple)):
                value = value[0] if value else ""
            text = str(value).strip()
            if text:
                return text
        return ""

    @staticmethod
    def _extract_chamber(data: Mapping[str, Any]) -> str:
        """Read a chamber/process-module name from a payload that carries it as
        a DV. Kept in step with the mapper's chamber key list so the tracker
        learns from exactly the fields the mapper later looks up."""
        for key in ("CHAMBER", "Chamber", "Cham"):
            value = data.get(key)
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                value = value[0] if value else ""
            text = str(value).strip()
            if text:
                return text
        return ""

    @staticmethod
    def _identifiers(data: Mapping[str, Any], keys: tuple[str, ...]) -> List[str]:
        result: List[str] = []
        for key in keys:
            value = data.get(key)
            if value is None:
                continue
            values = value if isinstance(value, (list, tuple)) else [value]
            for item in values:
                text = str(item).strip()
                if text and text not in result:
                    result.append(text)
        return result

    def note_resolution(
        self,
        machine_id: str,
        data: Mapping[str, Any],
        load_port: str,
    ) -> None:
        """Bind identifiers from a real payload to an evidenced load port."""
        state = self._state(machine_id)
        with self._lock(machine_id):
            for wafer in self._identifiers(
                data, ("WaferID", "WAFERID", "SubstID", "SubstIDList", "WAFER_ID")
            ):
                _bounded_put(state.wafer_ports, wafer, load_port, MAX_WAFER_IDS)
            for lot in self._identifiers(
                data, ("LotID", "LOTID", "SubstLotIDList", "LOT_ID")
            ):
                _bounded_put(state.lot_ports, lot, load_port, MAX_LOT_IDS)

    def note_chamber(
        self, machine_id: str, chamber: str, load_port: str
    ) -> None:
        """Record which load port the wafer now in `chamber` came from."""
        name = str(chamber).strip()
        port = str(load_port).strip()
        if not name or not port:
            return
        state = self._state(machine_id)
        with self._lock(machine_id):
            state.chamber_ports[name] = port

    def lookup_lp(
        self,
        machine_id: str,
        ceid: int,
        data: Mapping[str, Any],
        chamber: str = "",
    ) -> Optional[str]:
        """Return the best-guess load port for a chamber event on this
        machine. Returns None if we have no idea (caller falls back to NA)."""
        state = self._state(machine_id)
        with self._lock(machine_id):
            explicit = self._extract_port_id(data)
            if explicit:
                return explicit
            ctrl_id = self._extract_ctrl_job_id(data)
            if ctrl_id and ctrl_id in state.ctrl_jobs:
                return state.ctrl_jobs[ctrl_id]
            # Before any machine-wide guess: this chamber's own wafer. On a
            # two-chamber tool running two lots, active_lp is deliberately
            # None, so without this the event gets no port at all.
            #
            # "NA" is the mapper's placeholder for a profile that does no
            # chamber attribution; only a real chamber name counts.
            name = str(chamber).strip()
            named_chamber = bool(name) and name.upper() != "NA"
            if named_chamber and name in state.chamber_ports:
                return state.chamber_ports[name]
            for wafer in self._identifiers(
                data, ("WaferID", "WAFERID", "SubstID", "SubstIDList", "WAFER_ID")
            ):
                if wafer in state.wafer_ports:
                    return state.wafer_ports[wafer]
            for lot in self._identifiers(
                data, ("LotID", "LOTID", "SubstLotIDList", "LOT_ID")
            ):
                if lot in state.lot_ports:
                    return state.lot_ports[lot]
            if named_chamber:
                # The event named a chamber and that chamber holds no wafer we
                # know of. Falling through to "whichever port is active" would
                # hand a PM1 event to the only remaining cassette on LP3 - a
                # confidently wrong load port is worse than an empty one,
                # because nothing downstream can tell it was a guess.
                return None
            return state.active_lp

    def snapshot(self, machine_id: str) -> Dict[str, Any]:
        """For diagnostics / tests."""
        state = self._state(machine_id)
        with self._lock(machine_id):
            return {
                "active_lp": state.active_lp,
                "lp_history": list(state.lp_history),
                "ctrl_jobs": dict(state.ctrl_jobs),
                "wafer_ports": dict(state.wafer_ports),
                "lot_ports": dict(state.lot_ports),
                "chamber_ports": dict(state.chamber_ports),
            }
