"""NexGen Wafersystems MG Series equipment simulator.

Speaks HSMS in either equipment role and replays a realistic MG lot flow using
the *actual* CEIDs and report VID lists from
output/nexgen_mg_series/EventSubscription.json. No event DSL, no state machine
- a hardcoded script of events, same shape as the DaVinci simulator.

The MG profile is transcribed entirely from a PDF and has never met hardware,
so this simulator exists to exercise the paths that would otherwise be first
tried in a fab:

  * Two lots running CONCURRENTLY on two process modules fed from two
    different load ports. This is the case that breaks correlation-based
    attribution; the MG reports the originating load port inside every
    process-module event, and this proves the profile reads it.
  * Refusing a named subscription band (--refuse-band) so band isolation is
    demonstrated rather than assumed, including the enabled-event read-back
    reflecting the refusal.
  * Starting in HOST OFF-LINE (--start-offline) so the S1F17 ON-LINE request
    is a tested path. While OFF-LINE the equipment discards everything except
    S1F13/S1F17, exactly as the manual's section 3.2 describes.
  * Both HSMS roles (--hsms-mode active|passive), since the manual never says
    which one a real MG uses.

Run it standalone:

    python -m simulator.nexgen_mg_simulator --port 5051 --wafers 3
    python -m simulator.nexgen_mg_simulator --port 5051 --refuse-band gem300
    python -m simulator.nexgen_mg_simulator --port 5051 --start-offline
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set

import secsgem.hsms
import secsgem.secs.variables as secs_var
from secsgem.gem.communication_state_machine import CommunicationState

from eap_middleware.profiles import (
    NEXGEN_MG_CEID_BANDS,
    NEXGEN_MG_REPORTS,
    NEXGEN_MG_SVIDS,
    ProfileRegistry,
)
from gateway.identity import SecsS01F02Extended

from .equipment import EquipmentSimulator
from .secs_data_types import SecsDataTypes
from .secsgem_equipment import ALARM_CLASS_PARAMETER_CONTROL_WARNING

logger = logging.getLogger(__name__)


# On-line identity, exactly as the manual prints it. Section 9.1.1.1 captures
# the tool answering S1F14 with <A[4] 'MG22'> and <A[7] '3.7.0.0'>, and section
# 5.2 caps SOFTREV at "6 bytes maximum" (MDLN at 6). The simulator used to send
# an 18-character revision string, so the host was only ever exercised against
# an over-long value - the DaVinci case, which SecsS01F02Extended exists to
# tolerate - and never against a conformant one.
MDLN = "MG22"
SOFTREV = "3.7.0.0"

# ControlState (SVID 11) per the manual: 1=EquipmentOffline, 2=AttemptOnline,
# 3=HostOffline, 4=OnlineLocal, 5=OnlineRemote.
CONTROL_STATE_EQUIPMENT_OFFLINE = 1
CONTROL_STATE_HOST_OFFLINE = 3
CONTROL_STATE_ONLINE_LOCAL = 4

# ProcessState (SVID 15). The manual describes this as a one-byte unsigned
# integer in the state-model section and as ASCII in the status-variable
# table; --process-state-ascii switches the simulator between the two so the
# profile is proven to survive whichever the real tool turns out to send.
# The full set from the state-model section. The numbering has real gaps
# (there is no 6 or 11), so these are transcribed rather than enumerated.
PROCESS_STATE_OFF = 0
PROCESS_STATE_ERROR = 1
PROCESS_STATE_SERVICE = 2
PROCESS_STATE_INIT = 3
PROCESS_STATE_NOT_INITIALIZED = 4
PROCESS_STATE_IDLE = 5
PROCESS_STATE_SETUP = 7
PROCESS_STATE_READY = 8
PROCESS_STATE_PROCESSING = 9
PROCESS_STATE_STOP = 10
PROCESS_STATE_ABORT = 12
PROCESS_STATE_WAFER_REMOVE = 20

PROCESS_STATE_NAMES = {
    PROCESS_STATE_OFF: "OFF",
    PROCESS_STATE_ERROR: "ERROR",
    PROCESS_STATE_SERVICE: "SERVICE",
    PROCESS_STATE_INIT: "INIT",
    PROCESS_STATE_NOT_INITIALIZED: "NOTINITIALIZED",
    PROCESS_STATE_IDLE: "IDLE",
    PROCESS_STATE_SETUP: "SETUP",
    PROCESS_STATE_READY: "READY",
    PROCESS_STATE_PROCESSING: "PROCESSING",
    PROCESS_STATE_STOP: "STOP",
    PROCESS_STATE_ABORT: "ABORT",
    PROCESS_STATE_WAFER_REMOVE: "WAFERREMOVE",
}

# Bare notification events - the manual lists no valid variables for any of
# them, so they are emitted with an empty report.
CEID_PROCESSING_STATE_CHANGE = 7
CEID_INIT_COMPLETED = 100
CEID_PROCESS_STATE_SETUP = 101
CEID_SETUP_COMPLETED = 102
CEID_READY_FOR_PROCESS = 103

# How long the host's enabled-event count must hold steady before the
# simulator accepts the subscription as complete. Must exceed the gap
# between two of the host's subscription bands - this profile subscribes in
# 31 bands and the enabled set grows one burst per band, so a shorter window
# mistakes an inter-band pause for the end of the sequence.
SUBSCRIPTION_QUIET_SEC = 1.0


def _typed(value: Any) -> Any:
    """Wrap a Python value as the secsgem item an S6F11 V[] slot expects."""
    if isinstance(value, bool):
        return SecsDataTypes.boolean(value)
    if isinstance(value, int):
        return SecsDataTypes.u4(value)
    if isinstance(value, float):
        return SecsDataTypes.f4(value)
    if value is None:
        return SecsDataTypes.ascii("")
    if isinstance(value, (list, tuple)):
        if not value:
            return secs_var.Array(secs_var.String, [])
        sample = next((item for item in value if item is not None), value[0])
        if isinstance(sample, bool):
            return secs_var.Array(secs_var.Boolean, [bool(v) for v in value])
        if isinstance(sample, int):
            return secs_var.Array(secs_var.U4, [int(v) for v in value])
        if isinstance(sample, float):
            return secs_var.Array(secs_var.F4, [float(v) for v in value])
        return secs_var.Array(secs_var.String, [str(v) for v in value])
    return SecsDataTypes.ascii(str(value))



# CEID -> documented name, for the run log. Built once: the alias table is
# ~243 entries and this is called on every event.
_CEID_NAMES: Dict[int, str] = {}


def _ceid_name(ceid: int) -> str:
    """The manual's name for a CEID, or a marker when it has none."""
    if not _CEID_NAMES:
        try:
            _CEID_NAMES.update(
                ProfileRegistry().get("nexgen_mg_series").ceid_aliases
            )
        except Exception:  # pragma: no cover - defensive
            logger.debug("Could not load MG CEID names", exc_info=True)
            _CEID_NAMES[0] = ""
    return _CEID_NAMES.get(int(ceid), "unnamed")


class NexGenMgSimulator(EquipmentSimulator):
    """MG Series lot replay across two process modules and two load ports."""

    def __init__(
        self,
        settings: secsgem.hsms.HsmsSettings,
        tool_id: str = "MG_SIM_01",
        wafers_per_lot: int = 3,
        step_interval_sec: float = 0.5,
        fire_alarm: bool = True,
        loop_lots: bool = False,
        lot_id_prefix: str = "MGLOT",
        refuse_band: Optional[str] = None,
        start_offline: bool = False,
        process_state_ascii: bool = False,
        substrate_ids: bool = True,
        load_ports: Sequence[int] = (1, 2),
        replay_all: bool = False,
    ) -> None:
        super().__init__(settings=settings, tool_id=tool_id, event_interval=99999.0)
        self._spooling_enabled = False
        self.wafers_per_lot = max(1, int(wafers_per_lot))
        self.step_interval = max(0.0, float(step_interval_sec))
        self.fire_alarm = bool(fire_alarm)
        self.loop_lots = bool(loop_lots)
        self.lot_id_prefix = lot_id_prefix
        # Name of the subscription band to refuse (matches the 'band' field in
        # EventSubscription.json). Reports and links for that band are
        # rejected; every other band is accepted normally.
        self.refuse_band = refuse_band or None
        self.process_state_ascii = bool(process_state_ascii)
        # GEM300 tools report a real substrate ID; cassette tools do not, and
        # the profile must fall back to the load slot number.
        self.substrate_ids = bool(substrate_ids)
        self.load_ports = tuple(load_ports)
        # Sweep every CEID the profile documents instead of running the lot
        # script. The lot script fires 31 of 243; whole bands (gem300,
        # metrology_aux) otherwise never reach the middleware's decoder.
        self.replay_all = bool(replay_all)

        self._control_state = (
            CONTROL_STATE_HOST_OFFLINE if start_offline else CONTROL_STATE_ONLINE_LOCAL
        )
        # A tool that has just been powered on has not initialised yet. The
        # run loop walks NOTINITIALIZED -> INIT -> IDLE once, so the middleware
        # sees the startup path rather than a tool that is inexplicably idle
        # from its first message.
        self._process_state_value = PROCESS_STATE_NOT_INITIALIZED
        self._initialised = False
        self._lot_counter = 0
        self._last_ceid = 0
        self._refused_ceids: Set[int] = set()
        # Per-port live state, surfaced through S1F3 polling.
        self._port_lot_id: Dict[int, str] = {}
        self._port_recipe: Dict[int, str] = {}
        self._port_carrier: Dict[int, str] = {}
        self._emit_lock = threading.Lock()

        self.register_stream_function(1, 1, self._handle_s1f1_mg)
        self.register_stream_function(1, 3, self._handle_s1f3_mg)
        self.register_stream_function(1, 17, self._handle_s1f17_mg)
        self.register_stream_function(5, 3, self._handle_s5f3_mg)

    def _remote_command_profile(self) -> str:
        return "nexgen_mg_series"

    def _accept_remote_command(self, command: str, params: List[Any]) -> int:
        """NexGen command/state policy from the supplied MG command table."""
        if self.is_offline:
            return 2
        if params:
            return 3
        if command == "START":
            if self._process_state_value not in (PROCESS_STATE_IDLE, PROCESS_STATE_READY):
                return 2
            self._process_state_value = PROCESS_STATE_PROCESSING
            return 4
        if command in ("STOP", "ABORT"):
            if self._process_state_value != PROCESS_STATE_PROCESSING:
                return 2
            self._process_state_value = (
                PROCESS_STATE_STOP if command == "STOP" else PROCESS_STATE_ABORT
            )
            return 4
        return 1

    # ----- identity / status -------------------------------------------------

    @property
    def is_offline(self) -> bool:
        return self._control_state in (
            CONTROL_STATE_HOST_OFFLINE, CONTROL_STATE_EQUIPMENT_OFFLINE
        )

    def _handle_s1f1_mg(self, handler: Any, packet: Any) -> Any:
        return SecsS01F02Extended([MDLN, SOFTREV])

    def _handle_s1f17_mg(self, handler: Any, packet: Any) -> Any:
        """S1F17 Request ON-LINE -> S1F18 ONLACK.

        0 = accepted, 1 = refused, 2 = already online. Per the manual the
        request is honoured only out of HOST OFF-LINE: an operator-selected
        EQUIPMENT OFF-LINE stays authoritative and is refused, which is the
        one silent-failure mode the middleware cannot clear on its own.
        """
        if self._control_state == CONTROL_STATE_EQUIPMENT_OFFLINE:
            logger.info("[%s] S1F17 refused: EQUIPMENT OFF-LINE is operator-set",
                        self.tool_id)
            return self.stream_function(1, 18)(1)
        if self._control_state == CONTROL_STATE_HOST_OFFLINE:
            self._control_state = CONTROL_STATE_ONLINE_LOCAL
            logger.info("[%s] S1F17 accepted: HOST OFF-LINE -> ON-LINE", self.tool_id)
            return self.stream_function(1, 18)(0)
        return self.stream_function(1, 18)(2)

    def _handle_s5f3_mg(self, handler: Any, packet: Any) -> Any:
        """S5F3 Enable/Disable Alarm Send -> S5F4 ACKC5=0."""
        return self.stream_function(5, 4)(0)

    def _svid_value(self, svid: int) -> Any:
        if svid == 10:  # Clock
            return datetime.now().strftime("%Y%m%d%H%M%S%f")[:16]
        if svid == 11:
            return self._control_state
        if svid == 15:  # ProcessState - the documented int/ASCII contradiction
            return (
                str(self._process_state_value) if self.process_state_ascii
                else self._process_state_value
            )
        if svid == 12:  # EventsEnabled - what the tool believes is live
            return sorted(self.enabled_ceids())
        if svid == 16:  # LastEventID
            return self._last_ceid
        for port in range(1, 5):
            base = 3000 + port * 100
            if svid == base + 31:
                return self._port_lot_id.get(port, "")
            if svid == base + 32:
                return self._port_recipe.get(port, "")
            if svid == base + 33:
                return self._port_carrier.get(port, "")
            if svid == base + 10:  # portNMapResult - 1=FULLSLOT, 2=EMPTYSLOT
                return [1] * self.wafers_per_lot
        return 0

    def enabled_ceids(self) -> List[int]:
        """CEIDs actually enabled, honouring any refused band."""
        if self._all_events_enabled:
            candidates = set(NEXGEN_MG_CEID_BANDS)
        else:
            candidates = set(self._enabled_events)
        return sorted(candidates - self._refused_ceids)

    def _handle_s1f3_mg(self, handler: Any, packet: Any) -> Any:
        try:
            svids = self._decoded_body(1, 3, packet) or []
            if not svids:
                svids = sorted(set(NEXGEN_MG_SVIDS.values()))
            return self.stream_function(1, 4)(
                [_typed(self._svid_value(int(svid))) for svid in svids]
            )
        except Exception as exc:
            logger.error("[%s] S1F3 handler failed: %s", self.tool_id, exc)
            return self.stream_function(1, 4)([])

    # ----- subscription, with optional band refusal ---------------------------

    def _band_of_rptid(self, rptid: int) -> str:
        return NEXGEN_MG_CEID_BANDS.get(int(rptid) - 1000000000, "")

    def _refused_band_ceids(self) -> Set[int]:
        return {
            ceid for ceid, band in NEXGEN_MG_CEID_BANDS.items()
            if band == self.refuse_band
        }

    def _decoded_body(self, stream: int, function: int, packet: Any) -> Any:
        """Plain-Python view of an incoming message body.

        Handlers receive the raw SECS-II body as bytes, so the subscription
        messages have to go through the stream-function codec before their
        contents can be read. `List.get()` returns nested dicts/lists of
        Python values, which is all these handlers need.
        """
        data = packet.data
        if isinstance(data, bytes):
            message = self.stream_function(stream, function)()
            message.decode(data)
            data = message.data
        if hasattr(data, "get"):
            try:
                return data.get()
            except Exception:  # pragma: no cover - defensive
                pass
        return data

    # The S2F33/35/37 handlers keep their bare names because the base class
    # registers them by attribute lookup; the _mg-suffixed handlers above are
    # separate registrations and do not collide.

    def _handle_s2f33(self, handler: Any, packet: Any) -> Any:
        """S2F33 Define Report -> S2F34 DRACK.

        Refusing a band rejects the ENTIRE message, which is what the manual
        says real equipment does when it detects any error - and is the whole
        reason the middleware splits the subscription up in the first place.
        """
        body = self._decoded_body(2, 33, packet)
        definitions = body.get("DATA", []) if isinstance(body, dict) else []
        parsed = {
            int(report["RPTID"]): list(report.get("VID", []))
            for report in definitions
        }
        if self.refuse_band and any(
            self._band_of_rptid(rptid) == self.refuse_band for rptid in parsed
        ):
            logger.info("[%s] S2F33 refused: band %r (%d reports)",
                        self.tool_id, self.refuse_band, len(parsed))
            return self.stream_function(2, 34)(4)  # DRACK 4 = invalid VID
        if not definitions:
            # SEMI E5: a zero-length report list deletes EVERY report
            # definition, and with them every event link. This is step 2 of
            # the manual's own lot-start sequence (MG 9.1/9.2: "Host deletes
            # all existing report definitions", then "... all existing report
            # links"), so a host that opens with it must see the tool
            # actually cleared. Acknowledging DRACK=0 and keeping the old
            # definitions - which is what this did - would let a rig pass a
            # reset the real tool performs, and the base EquipmentSimulator
            # already gets this right.
            self._report_definitions.clear()
            self._event_links.clear()
            logger.info("[%s] S2F33 zero-length list: all reports and links "
                        "deleted", self.tool_id)
            return self.stream_function(2, 34)(0)
        for rptid, vids in parsed.items():
            if vids:
                self._report_definitions[rptid] = vids
            else:
                self._report_definitions.pop(rptid, None)
                # Deleting one report also drops any link that referenced it;
                # a CEID left pointing at a report that no longer exists
                # would report an undefined RPTID.
                for ceid, rptids in list(self._event_links.items()):
                    remaining = [r for r in rptids if r != rptid]
                    if remaining:
                        self._event_links[ceid] = remaining
                    else:
                        self._event_links.pop(ceid, None)
        return self.stream_function(2, 34)(0)

    def _handle_s2f35(self, handler: Any, packet: Any) -> Any:
        """S2F35 Link Event Report -> S2F36 LRACK."""
        body = self._decoded_body(2, 35, packet)
        links = body.get("DATA", []) if isinstance(body, dict) else []
        if not links:
            # Zero-length DATA = delete every report link (SEMI E5), exactly
            # as the base class does. Without this the "delete all" step of the
            # MG lot-start reset was acknowledged while every old link lived on.
            self._event_links.clear()
            logger.info(
                "[%s] S2F35: zero-length DATA; all report links deleted",
                self.tool_id,
            )
            return self.stream_function(2, 36)(0)
        parsed = {
            int(link["CEID"]): [int(rptid) for rptid in link.get("RPTID", [])]
            for link in links
        }
        if self.refuse_band and any(
            NEXGEN_MG_CEID_BANDS.get(ceid) == self.refuse_band for ceid in parsed
        ):
            logger.info("[%s] S2F35 refused: band %r (%d links)",
                        self.tool_id, self.refuse_band, len(parsed))
            return self.stream_function(2, 36)(4)  # LRACK 4 = CEID does not exist
        if any(
            rptid not in self._report_definitions
            for rptids in parsed.values()
            for rptid in rptids
        ):
            return self.stream_function(2, 36)(3)  # LRACK 3 = RPTID does not exist
        for ceid, rptids in parsed.items():
            # A CEID linked to an empty RPTID list means "delete that link",
            # not "link it to nothing" - storing an empty list left a stale
            # reference the base class correctly removes.
            if rptids:
                self._event_links[ceid] = rptids
            else:
                self._event_links.pop(ceid, None)
        return self.stream_function(2, 36)(0)

    def _handle_s2f37(self, handler: Any, packet: Any) -> Any:
        """S2F37 Enable/Disable Event -> S2F38 ERACK."""
        body = self._decoded_body(2, 37, packet)
        enable = bool(body.get("CEED", False)) if isinstance(body, dict) else False
        ceids = [
            int(ceid)
            for ceid in (body.get("CEID", []) if isinstance(body, dict) else [])
        ]
        # The base class sets this flag on every S2F37; this override must too,
        # or _is_event_enabled() keeps returning True for every CEID and the
        # subset-enable below filters nothing (the "fired but NOT enabled" INFO
        # then never fires for a real subset subscription).
        self._event_reporting_configured = True
        if enable:
            if ceids:
                self._enabled_events.update(ceids)
                self._disabled_events.difference_update(ceids)
            else:
                self._all_events_enabled = True
                self._enabled_events.clear()
                self._disabled_events.clear()
        elif ceids:
            if self._all_events_enabled:
                self._disabled_events.update(ceids)
            else:
                self._enabled_events.difference_update(ceids)
        else:
            self._all_events_enabled = False
            self._enabled_events.clear()
            self._disabled_events.clear()
        if self.refuse_band:
            # A band whose reports were refused must not start reporting, and
            # must be absent from the EventsEnabled read-back.
            self._refused_ceids = self._refused_band_ceids()
            self._enabled_events.difference_update(self._refused_ceids)
        return self.stream_function(2, 38)(0)

    # ----- plumbing ----------------------------------------------------------

    def _interruptible_wait(self, duration: float) -> bool:
        if not self._running:
            return False
        if duration <= 0:
            return self.communication_state.current == CommunicationState.COMMUNICATING
        deadline = time.monotonic() + duration
        while self._running:
            if self.communication_state.current != CommunicationState.COMMUNICATING:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            if self._stop_event.wait(min(0.05, remaining)):
                return False
        return False

    def _send_s6f11(self, ceid: int, values: Sequence[Any]) -> bool:
        """Send one S6F11 with the report VID list this CEID was defined with."""
        if self.communication_state.current != CommunicationState.COMMUNICATING:
            return False
        if self.is_offline:
            # Section 3.2: OFF-LINE equipment discards host primaries and
            # reports nothing. This is exactly the silent-failure mode the
            # ON-LINE request exists to prevent.
            logger.info(
                "[%s] OFF-LINE: dropping CEID %s (%s). Section 3.2 - an "
                "OFF-LINE tool reports nothing until the host requests "
                "ON-LINE (S1F17).", self.tool_id, ceid, _ceid_name(ceid),
            )
            return True
        self._last_ceid = ceid
        if ceid in self._refused_ceids or not self._is_event_enabled(ceid):
            # INFO, not DEBUG. "The tool fired an event the host never
            # subscribed to" is the single most useful line for diagnosing
            # an empty feed, and at DEBUG nobody ever saw it.
            logger.info(
                "[%s] CEID %s (%s) fired but NOT enabled by the host; "
                "not sent", self.tool_id, ceid, _ceid_name(ceid),
            )
            return True
        reports: List[Dict[str, Any]] = []
        if values:
            reports.append({
                "RPTID": SecsDataTypes.u4(1000000000 + ceid),
                "V": [_typed(value) for value in values],
            })
        body = {
            "DATAID": SecsDataTypes.u4(0),
            "CEID": SecsDataTypes.u4(ceid),
            "RPT": reports,
        }
        try:
            with self._emit_lock:
                response = self.send_and_waitfor_response(
                    self.stream_function(6, 11)(body)
                )
            if response is None:
                logger.warning("[%s] S6F11 CEID=%s unacknowledged", self.tool_id, ceid)
                return False
            logger.info(
                "[%s] -> S6F11 CEID=%s (%s) values=%d",
                self.tool_id, ceid, _ceid_name(ceid), len(values),
            )
            return True
        except Exception as exc:
            logger.error("[%s] S6F11 CEID=%s failed: %s", self.tool_id, ceid, exc)
            return False

    def _emit(self, ceid: int, values: Sequence[Any] = ()) -> bool:
        return self._send_s6f11(ceid, values) and self._interruptible_wait(
            self.step_interval
        )

    def _set_process_state(self, value: int, announce: bool = True) -> bool:
        """Move SVID 15 and tell the host it moved.

        The manual gives ProcessingStateChange (CEID 7) no valid variables, so
        the host learns the new value by polling SVID 15 after the event - the
        event says only that something changed. Keeping the two in that order
        is the point: the SV is updated first so a poll that races the event
        cannot read the old value.
        """
        self._process_state_value = value
        if not announce:
            return True
        return self._emit(CEID_PROCESSING_STATE_CHANGE)

    def _run_initialisation(self) -> bool:
        """NOTINITIALIZED -> INIT -> IDLE, once per run.

        Without this the tool is idle from its first message and the init path
        is never exercised, which is where a host that gates on process state
        would fail.
        """
        if self._initialised:
            return True
        if not self._set_process_state(PROCESS_STATE_INIT):
            return False
        if not self._emit(CEID_INIT_COMPLETED):
            return False
        if not self._set_process_state(PROCESS_STATE_IDLE):
            return False
        self._initialised = True
        return True

    def _run_process_setup(self) -> bool:
        """IDLE -> SETUP -> READY, the run-up the manual puts before every lot.

        SETUP is the equipment conditioning the modules for the selected
        recipe; READY is every precondition met. Both carry their own event.
        """
        if not self._set_process_state(PROCESS_STATE_SETUP):
            return False
        if not self._emit(CEID_PROCESS_STATE_SETUP):
            return False
        if not self._emit(CEID_SETUP_COMPLETED):
            return False
        if not self._set_process_state(PROCESS_STATE_READY):
            return False
        return self._emit(CEID_READY_FOR_PROCESS)

    def _send_alarm(self, alid: int, text: str, is_set: bool) -> bool:
        if self.communication_state.current != CommunicationState.COMMUNICATING:
            return False
        try:
            with self._emit_lock:
                # ALCD: bit 7 = set/clear, bits 0-6 = the SEMI E5 category. A
                # category of 0 is not one E5 defines and reached the host as
                # "Code=0" for every alarm.
                alcd = (0x80 if is_set else 0x00) | ALARM_CLASS_PARAMETER_CONTROL_WARNING
                response = self.send_and_waitfor_response(
                    self.stream_function(5, 1)([alcd, alid, text])
                )
            return response is not None
        except Exception as exc:
            logger.error("[%s] S5F1 ALID=%s failed: %s", self.tool_id, alid, exc)
            return False

    # ----- the lot script ----------------------------------------------------

    def _identity_values(
        self,
        pm: int,
        lot_id: str,
        recipe: str,
        port: int,
        slot: int,
        carrier: str,
        job: str,
        with_substrate: bool,
    ) -> List[Any]:
        """Build the V[] for a process-module event in report VID order."""
        values: List[Any] = []
        if with_substrate:
            values.append(f"{lot_id}.{slot:02d}" if self.substrate_ids else "")
        values += [
            lot_id,           # pmNCurrWaferLotId
            recipe,           # pmNCurrWaferPPId
            port,             # pmNCurrWaferLoadPort  <- the whole point
            slot,             # pmNCurrWaferLoadSlot
            carrier,          # pmNCurrWaferCId
            job,              # pmNCurrWaferJobId
            slot,             # pmNCurrWaferUnloadSlot
            port,             # pmNCurrWaferUnloadPort
        ]
        return values

    def _lot_chemistry_values(self) -> List[float]:
        """The per-lot chemistry tail of the CEID 5 report.

        Sized from the profile's own report definition so the simulator can
        never drift out of step with the VID list the middleware asked for;
        values ascend so a mis-ordered decode is obvious in the assertion.
        """
        count = len(NEXGEN_MG_REPORTS[5]) - 5  # minus the identity slots
        return [round(10.0 + index, 1) for index in range(count)]

    def _wafer_metric_values(self, ceid: int, identity_slots: int) -> List[float]:
        """The per-wafer metric tail of the pmNWaferFinished report (213/313).

        Same contract as `_lot_chemistry_values`, and it exists for the same
        reason: a report the simulator under-fills is a report whose decode is
        never actually exercised. 213/313 are the largest per-wafer reports in
        the subscription - the identity block plus every flow, temperature,
        chuck-speed and bevel-etch variable the chamber publishes - and they
        used to be emitted with the identity block alone, nine values against a
        seventy-four slot layout.

        That is what let PM2's medium temperatures (DVIDs 1210-1218) go missing
        from the subscription unnoticed: the simulator never sent those values,
        so no end-to-end test could tell a full report from a truncated one.
        Sizing from the profile means adding or removing a metric VID changes
        what the simulator sends automatically.
        """
        count = len(NEXGEN_MG_REPORTS[ceid]) - identity_slots
        return [round(100.0 + index, 1) for index in range(max(0, count))]

    def _run_port_lot(self, port: int, pm: int) -> bool:
        """One complete lot on `port`, processed in process module `pm`."""
        # Both load ports run a lot concurrently, so this read-modify-write
        # needs the lock: without it both ports can take the same index and
        # report the same LOTID/carrier - the exact confusion the two-port
        # test exists to rule out.
        with self._emit_lock:
            self._lot_counter += 1
            index = self._lot_counter
        lot_id = f"{self.lot_id_prefix}_{index:04d}"
        recipe = f"MG_CLEAN_{pm:02d}"
        carrier = f"CAR_{index:04d}"
        job = f"JOB_{index:04d}"
        self._port_lot_id[port] = lot_id
        self._port_recipe[port] = recipe
        self._port_carrier[port] = carrier

        pm_base = 200 if pm == 1 else 300

        # Cassette placed (no valid data variables - the port is in the CEID)
        if not self._emit(129 + port):
            return False
        # Cassette mapped: wafers in cassette, then the slot map itself.
        # 1=FULLSLOT, 2=EMPTYSLOT, 3=CROSSSLOTTED, 4=DOUBLESLOTTED - slot 2 is
        # reported cross-slotted so the abnormal cases are exercised too.
        if not self._emit(139 + port, [self.wafers_per_lot]):
            return False
        slot_map = [1] * self.wafers_per_lot
        if len(slot_map) > 1:
            slot_map[1] = 3
        if not self._emit(145, [port, slot_map]):
            return False
        # Recipe selected for this port's carrier (what the factory host's
        # PPSELECT triggers). V = [ppSelectedName, ppSelectedPortId].
        if not self._emit(13, [recipe, str(port)]):
            return False
        # Processing started on this port: wafers, output port, date, time.
        # The machine-level process state (SVID 15) is driven by the event
        # loop, not from here: two ports run concurrently in their own threads
        # and would race each other between PROCESSING and IDLE.
        now = datetime.now()
        if not self._emit(149 + port, [
            self.wafers_per_lot, port,
            now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"),
        ]):
            return False

        for slot in range(1, self.wafers_per_lot + 1):
            started = self._identity_values(
                pm, lot_id, recipe, port, slot, carrier, job, with_substrate=True
            )
            if not self._emit(pm_base + 12, started):      # pmNWaferStarted
                return False
            if not self._emit(pm_base + 20, [f"Medium{pm}", slot]):  # step started
                return False
            if not self._emit(pm_base + 21, self._identity_values(
                pm, lot_id, recipe, port, slot, carrier, job, with_substrate=False
            ) + [f"Medium{pm}", slot]):                    # pmNStepFinished
                return False
            # pmNWaferFinished carries the identity block AND the whole
            # per-wafer metric block, which is the report the fab actually
            # wants off this tool. Filling it is what proves the positional
            # decode of all 74 slots rather than assuming it.
            if not self._emit(
                pm_base + 13,
                started + self._wafer_metric_values(pm_base + 13, len(started)),
            ):
                return False
            if self.fire_alarm and slot == 1 and port == self.load_ports[0]:
                self._send_alarm(1001, "PM1: chuck N2 flow below limit", True)
                self._send_alarm(1001, "PM1: chuck N2 flow below limit", False)

        # Ready to unload: wafers finished, total lot time, total process time
        if not self._emit(123 + port, [self.wafers_per_lot, 120, 95]):
            return False
        # Equipment-level lot completion. This is the largest report in the
        # subscription - the last-finished-lot identity plus the whole per-lot
        # chemistry block - so emitting it is how the positional decode of 35
        # slots gets proven rather than assumed.
        if not self._emit(5, [job, lot_id, recipe, str(port), carrier]
                          + self._lot_chemistry_values()):
            return False
        # Cassette physically removed - this is what closes the per-lot CSV
        if not self._emit(133 + port):
            return False
        self._port_lot_id.pop(port, None)
        self._port_carrier.pop(port, None)
        logger.info("[%s] Lot %s finished on port %s / PM%s",
                    self.tool_id, lot_id, port, pm)
        return True

    def _wait_for_subscription(self, timeout: float = 15.0) -> None:
        """Hold the lot script until the host's subscription has settled.

        A collection event fired before the host's S2F37 is genuinely not
        reported - that is correct GEM behaviour, not a bug - and a real tool
        does not start a lot in the same instant HSMS selects. Waiting until
        the enabled-event set stops growing means the banded subscription (one
        define/link/enable round trip per band) has fully landed before the
        first cassette is placed. Bounded, so a host that never subscribes
        still gets a run.
        """
        deadline = time.monotonic() + timeout
        previous = -1
        quiet_since: Optional[float] = None
        while self._running and time.monotonic() < deadline:
            if self._all_events_enabled:
                return
            current = len(self._enabled_events)
            if current and current == previous:
                # The count has stopped growing. That is only meaningful once
                # it has held for longer than the gap between two of the
                # host's subscription bands: this profile subscribes in 31
                # bands of S2F33/S2F35/S2F37, and the enabled set grows in a
                # burst per band. Requiring just two consecutive equal polls
                # (100ms) read any inter-band pause as "finished", so the lot
                # started against a partial subscription - and every CEID the
                # host had not reached yet was correctly, silently dropped.
                # The symptom was a run that emitted initialisation and setup
                # and then appeared to stall with no lot events at all.
                if quiet_since is None:
                    quiet_since = time.monotonic()
                elif time.monotonic() - quiet_since >= SUBSCRIPTION_QUIET_SEC:
                    logger.info(
                        "[%s] Subscription settled: %d events enabled "
                        "(unchanged for %.1fs)",
                        self.tool_id, current, SUBSCRIPTION_QUIET_SEC,
                    )
                    return
            else:
                quiet_since = None
            previous = current
            if self._stop_event.wait(0.05):
                return
        enabled = len(self._enabled_events)
        if enabled:
            logger.warning(
                "[%s] Subscription still growing after %.0fs (%d events "
                "enabled); starting the lot anyway. Events the host has not "
                "enabled yet will not be reported.",
                self.tool_id, timeout, enabled,
            )
        else:
            logger.warning("[%s] Host never enabled any events; starting anyway",
                           self.tool_id)

    def _run_replay_sweep(self) -> None:
        """Emit every CEID the profile documents, once, in CEID order.

        Refused and disabled CEIDs are filtered by `_send_s6f11` exactly as
        they are for the lot script, so a `--refuse-band` run sweeps only the
        bands the host actually accepted.
        """
        from eap_middleware.profiles import (
            ProfileRegistry,
            profile_with_subscription_file,
        )

        from .event_replay import replay

        base = ProfileRegistry().get("nexgen_mg_series")
        profile = profile_with_subscription_file(
            base, base.event_subscription_path
        )
        sent = replay(profile, self._emit)
        logger.info(
            "[%s] Replay sweep sent %s of %s documented CEIDs",
            self.tool_id, sent, len(profile.ceid_aliases),
        )

    def _event_loop(self) -> None:
        logger.info("[%s] MG scripted lot loop ready", self.tool_id)
        settled = False
        while self._running:
            if self.communication_state.current != CommunicationState.COMMUNICATING:
                self._stop_event.wait(0.1)
                settled = False
                continue
            if not settled:
                self._wait_for_subscription()
                settled = True
                if self.communication_state.current != CommunicationState.COMMUNICATING:
                    continue
            # Walk the startup path once the host is actually listening, so
            # NOTINITIALIZED -> INIT -> IDLE is on the wire rather than having
            # happened before anyone could see it. Outside the `settled` block
            # so a run that loses the link mid-initialisation retries it on
            # reconnect instead of processing lots that never initialised.
            if not self._initialised and not self._run_initialisation():
                continue
            if self.replay_all:
                self._run_replay_sweep()
                if not self.loop_lots:
                    logger.info("[%s] Replay sweep finished, idling", self.tool_id)
                    while self._running and not self._stop_event.wait(0.2):
                        pass
                    return
                self._interruptible_wait(self.step_interval * 2)
                continue
            # Run one lot per load port CONCURRENTLY, each in its own process
            # module. Interleaved S6F11s from two ports are precisely the
            # condition under which port attribution by inference goes wrong.
            # SVID 15 is a machine-level variable, so the run-up and the
            # wind-down bracket the whole concurrent batch rather than running
            # once per port.
            if not self._run_process_setup():
                continue
            if not self._set_process_state(PROCESS_STATE_PROCESSING):
                continue
            threads = [
                threading.Thread(
                    target=self._run_port_lot,
                    args=(port, pm),
                    name=f"MGLot-P{port}",
                    daemon=True,
                )
                for pm, port in enumerate(self.load_ports, start=1)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            # WAFERREMOVE is the documented state for "wafers are still in the
            # system and are being handed back", which is this moment exactly.
            self._set_process_state(PROCESS_STATE_WAFER_REMOVE)
            self._set_process_state(PROCESS_STATE_IDLE)
            if not self.loop_lots:
                logger.info("[%s] Lots finished, idling", self.tool_id)
                while self._running and not self._stop_event.wait(0.2):
                    pass
                return
            self._interruptible_wait(self.step_interval * 2)


def main() -> None:
    parser = argparse.ArgumentParser(description="NexGen MG Series simulator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5051)
    parser.add_argument("--device-id", type=int, default=0)
    parser.add_argument("--hsms-mode", choices=("passive", "active"), default="passive",
                        help="equipment HSMS role; the MG manual states neither")
    parser.add_argument("--wafers", type=int, default=3)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--no-alarm", action="store_true")
    parser.add_argument("--refuse-band", default=None,
                        help="reject one subscription band, e.g. gem300")
    parser.add_argument("--start-offline", action="store_true",
                        help="start in HOST OFF-LINE so S1F17 is required")
    parser.add_argument("--process-state-ascii", action="store_true",
                        help="report ProcessState as ASCII instead of an integer")
    parser.add_argument("--no-substrate-ids", action="store_true",
                        help="behave as a cassette tool with no GEM300 substrate IDs")
    parser.add_argument("--tool-id", default="MG_SIM_01")
    parser.add_argument("--replay-all", action="store_true",
                        help="sweep all 243 documented CEIDs instead of "
                             "running the lot script (decode coverage, not "
                             "a physically coherent lot)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    mode = (
        secsgem.hsms.HsmsConnectMode.PASSIVE if args.hsms_mode == "passive"
        else secsgem.hsms.HsmsConnectMode.ACTIVE
    )
    # The config-driven runner sets all five timers via resolved_hsms_timers();
    # this standalone main must too, or the simulator runs secsgem's library
    # defaults (T7=8s) against a host that states T7=10s and the link can drop
    # for a timer mismatch neither side logs. Lazy import: gateway.host defines
    # this constant near the end of the module, so a top-level import here
    # would risk a circular-import window.
    from gateway.host import DEFAULT_HSMS_TIMERS

    settings = secsgem.hsms.HsmsSettings(
        address=args.host,
        port=args.port,
        connect_mode=mode,
        session_id=args.device_id,
        **DEFAULT_HSMS_TIMERS,
    )
    simulator = NexGenMgSimulator(
        settings=settings,
        tool_id=args.tool_id,
        wafers_per_lot=args.wafers,
        step_interval_sec=args.interval,
        fire_alarm=not args.no_alarm,
        loop_lots=args.loop,
        refuse_band=args.refuse_band,
        start_offline=args.start_offline,
        process_state_ascii=args.process_state_ascii,
        substrate_ids=not args.no_substrate_ids,
        replay_all=args.replay_all,
    )
    simulator.enable()
    simulator.start_events()
    peer = "connect the middleware with hsms_mode='active'"
    if args.hsms_mode == "active":
        peer = "the middleware must listen with hsms_mode='passive'"
    print(f"[{args.tool_id}] MG simulator on {args.host}:{args.port} "
          f"({args.hsms_mode.upper()}); {peer}. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        simulator.disable()


if __name__ == "__main__":  # pragma: no cover
    main()
