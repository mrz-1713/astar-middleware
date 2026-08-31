"""GEM equipment peer: the SECS/GEM plumbing every simulator is built on.

`SecsGemEquipment` carries no vendor logic of its own. It owns the GEM message
handlers (S1F1/S1F3/S1F11/S2F13/S5F3), the S6F11 and S5F1 senders, packet
decoding, and the interruptible lot loop. `ProfileSimulator` subclasses it to
drive any profile in the registry, and `NexGenMgSimulator` builds on the same
plumbing. It was named `DavinciSimulator` until the numbers below were
recognised as defaults rather than as the class's purpose.

The DaVinci 200 MC4 HC1 lot script stays here as the default behaviour: a
realistic flow using the *actual* DaVinci CEIDs and V[] payload layouts from
output/davinci200_mc4_hc1/EventSubscription.json. No event-DSL, no state
machine - just a hardcoded script of (delay, ceid, v_list) tuples.

The `_handle_*_davinci` handler names are deliberate and must NOT be shortened
to bare `_handle_s1f1` / `_handle_s1f3` / `_handle_s2f13`: `EquipmentSimulator`
already defines those, and dropping the suffix would silently override the base
implementations instead of registering alongside them.

Lifecycle for one lot (default 3 wafers):

  1. MaterialReceived (PortID=1)             -> middleware activates LP1
  2. CarrierClamped                          -> mounted/clamped
  3. ControlJob:Selected-Executing           -> lot_start
  4. For each wafer:
       a. NeedsProcessing2InProcess          -> wafer_start (substrate list)
       b. PM1/ProcessingStarted              -> process_start (W, L, R)
       c. PM1/ProcessingFinished             -> process_end (W, L, R, +files +TestResults)
       d. InProcess2Processed                -> wafer_end
  5. ControlJob:Completed-NoState            -> lot_end
  6. LP1/CarrierDeparted                     -> unloaded (closes per-lot CSV)
  7. MaterialRemoved                         -> tracker deactivates LP1

Optionally also fires one S5F1 alarm partway through the run so the
middleware exercises the alarm path.

Drive it from a test or the demo script:

    python -m simulator.secsgem_equipment --port 5050 --wafers 3 --interval 0.5

Use ponytail mode: small file, clear flow, no abstractions.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Sequence

import secsgem.hsms
import secsgem.secs.variables as secs_var
from secsgem.gem.communication_state_machine import CommunicationState

from .equipment import EquipmentSimulator
from .secs_data_types import SecsDataTypes
from eap_middleware.mapper import RPTID_CEID_OFFSET
from gateway.identity import SecsS01F02Extended

logger = logging.getLogger(__name__)


# SEMI E5 ALCD alarm categories (bits 0-6). 1 and 2 are the safety categories
# the middleware's alarm limiter must never shed; 3 is the ordinary category
# for a process parameter drifting out of limits, which is what the shipped
# lot scripts fire.
ALARM_CLASS_PERSONAL_SAFETY = 1
ALARM_CLASS_EQUIPMENT_SAFETY = 2
ALARM_CLASS_PARAMETER_CONTROL_WARNING = 3
ALARM_CLASS_PARAMETER_CONTROL_ERROR = 4


# ----- DaVinci SVID mock values -----
# Maps the real DaVinci SVIDs (sourced from SECS-Items_MueTec workbook) to
# Python values that an S1F3 status request returns. Values are realistic but
# static; for state-dependent ones (Clock, ControlState, ProcessState) the
# simulator computes them at request time.

_DAVINCI_SVID_STATIC_VALUES: Dict[int, Any] = {
    # ControlState 1010001: U1 (1=Offline/Equip, 2=Attempt, 3=Host Offline,
    # 4=Online Local, 5=Online Remote). Simulator stays Online-Remote.
    1010001: 5,
    1010002: 1,             # PreviousControlState (Offline before going online)
    1010003: 1,             # EventsEnabled (1=enabled)
    1010004: 3050001,       # LastEventID (something recent)
    # 1010005 Clock - computed at request time
    1020001: 1,             # AlarmsEnabled
    1020002: 0,             # AlarmsSet (count of active alarms)
    1030001: 0,             # SpoolCountActual
    1030002: 0,             # SpoolCountTotal
    1030003: "",            # SpoolStartTime
    1030004: "",            # SpoolFullTime
    1040001: "",            # PPError
    1050001: 2,             # ProcessState (2 = Idle when not processing)
    1050002: 1,             # PreviousProcessState (1=Init)
    1060005: 1,             # PM1/OperationMode (1=Production)
    1060006: False,                # PM1/RecipeActive (BOOLEAN - overridden dynamically)
    1060007: "Recipe_Overlay_v3",  # PM1/RecipeName
    1060008: True,          # PM1/ReadyForProcess
    1070001: 1,             # TM1/OperationMode
    1070002: "",            # TM1/WaferID
    # Load port 1 state (E84 / E87 - boolean & U1 values)
    1080011: 1,             # LP1/ClampStatus (1=clamped)
    1080012: 0,             # LP1/DoorStatus (0=closed)
    1080013: 1,             # LP1/IsMapped
    1080014: 1,             # LP1/CarrierPresentStatus
    1080016: 2,             # LP1/OperationMode (2=Auto)
    1080017: 6,             # LP1/State (6=ReadyToUnload eg)
    # Load port 2
    1090011: 0,             # LP2/ClampStatus
    1090012: 1,             # LP2/DoorStatus
    1090013: 0,             # LP2/IsMapped
    1090014: 0,             # LP2/CarrierPresentStatus
    1090016: 2,
    1090017: 1,             # LP2 In Service / Available
    1100001: 16,            # QueueAvailableSpace
    1100002: 0,             # QueuedCJobs
    1120001: "",            # LP1/CarrierID (set during lot)
    1120002: 1,             # LP1/PortID
    1120010: "",            # LP1/MaterialID
    1130001: "",            # LP2/CarrierID
    1130002: 2,
    # FFU / Vacuum monitoring - typical metrology-tool values
    1170001: 980,           # FFUGaugePressurePM (in Pa, say)
    1170002: 985,           # FFUGaugePressureEFEM1
    1170003: 988,
    1170004: 1013,          # MainPressure (atm-ish)
    1170005: 50,            # MainVacuumEFEM
    1170006: 10,            # MainVacuumPM
    1170011: 1, 1170012: 1, 1170013: 1, 1170014: 1,   # FFU EFEM fans on
    1170015: 1, 1170016: 1, 1170017: 1, 1170018: 1,   # FFU PM fans on
}


# DaVinci Equipment Constants (Section EC of the workbook). The simulator
# returns sane defaults for the ones the middleware might query.
_DAVINCI_ECID_STATIC_VALUES: Dict[int, Any] = {
    # TimeFormat: 0 = 12-byte yymmddhhmmss, 1 = 16-byte YYYYMMDDhhmmsscc.
    # Default 1 comes from the vendor workbook (EC sheet, ECID 4010001,
    # "Default Value" = 1). The 0/1 meaning is stated verbatim by the other two
    # vendors' manuals - Omega Table 6 ECID 67 "0 = 12 byte format / 1 = 16 byte
    # format" and NexGen MG §8.4 ECID 5 "0 = 12-byte format / 1 = 16-byte
    # format, default=1" - and 16-byte is the Y2K-compliant form all three
    # default to. `_davinci_svid_value` reads this constant rather than
    # hardcoding a width, so flipping it here actually changes the clock the
    # simulator emits.
    4010001: 1,             # TimeFormat
    4010002: 30,            # HeartbeatInterval (seconds)
    4010003: 10,            # EstablishCommunicationsTimeout
    4010004: 0,             # EnableWBit
    4020001: 1,             # EnableSpooling
    4020002: 1,             # OverWriteSpool
    4020003: 1000,          # MaxSpoolMessages
    4020004: 100,           # MaxSpoolTransmit
    4030001: 1,             # AllowOverrideFlowRecipes
    4030002: 1,             # AllowOverrideModuleRecipes
    4030003: "DAV_SIM_01",  # MachineName - overridden to tool_id at runtime
    4030006: 0,             # WaferIDReadingMode
    4060001: 0,             # IDReader/ReadMode
    4070001: "Default",     # SetUpName
    4100001: True,          # PM1/Installed
}


# ----- helpers to type-stamp arbitrary V[] entries -----

def _typed(value: Any) -> Any:
    """Wrap a Python value as the right secsgem-typed item for an S6F11 V[].

    Real DaVinci SECS-II includes nested list types for E90 substrate events
    (SubstLotIDList = Array of String, TestResults = list of structures...).
    We use secsgem's runtime Array/List constructors so the wire encoding
    matches the real machine 1:1:

      list of strings   -> secs_var.Array(String, [...])
      list of ints      -> secs_var.Array(U4, [...])
      list of dicts     -> Array(String, [json_of_each])
      empty list        -> empty Array(String)
      dict              -> ASCII JSON
    """
    if isinstance(value, bool):
        return SecsDataTypes.boolean(value)
    if isinstance(value, int):
        return SecsDataTypes.u4(value)
    if isinstance(value, float):
        return SecsDataTypes.f4(value)
    if isinstance(value, str):
        return SecsDataTypes.ascii(value)
    if value is None:
        return SecsDataTypes.ascii("")
    if isinstance(value, dict):
        return SecsDataTypes.ascii(json.dumps(value, default=str))
    if isinstance(value, (list, tuple)):
        if not value:
            return secs_var.Array(secs_var.String, [])
        # Pick an item type based on the first non-empty element.
        sample = next((v for v in value if v is not None), value[0])
        if isinstance(sample, bool):
            return secs_var.Array(secs_var.Boolean, [bool(v) for v in value])
        if isinstance(sample, int):
            return secs_var.Array(secs_var.U4, [int(v) for v in value])
        if isinstance(sample, float):
            return secs_var.Array(secs_var.F4, [float(v) for v in value])
        if isinstance(sample, str):
            return secs_var.Array(secs_var.String, [str(v) for v in value])
        if isinstance(sample, dict):
            return secs_var.Array(
                secs_var.String,
                [json.dumps(v, default=str) for v in value],
            )
        # Fallback: stringify and treat as Array of String
        return secs_var.Array(secs_var.String, [str(v) for v in value])
    return value


def _typed_list(values: Sequence[Any]) -> List[Any]:
    return [_typed(v) for v in values]


# ----- The simulator -----

class SecsGemEquipment(EquipmentSimulator):
    """Realistic DaVinci lot replay."""

    def __init__(
        self,
        settings: secsgem.hsms.HsmsSettings,
        tool_id: str = "DAV_SIM_01",
        wafer_count: int = 3,
        step_interval_sec: float = 0.5,
        fire_alarm: bool = True,
        loop_lots: bool = False,
        lot_id_prefix: str = "LOT_SIM",
        alarm_class: int = ALARM_CLASS_PARAMETER_CONTROL_WARNING,
    ) -> None:
        super().__init__(settings=settings, tool_id=tool_id, event_interval=99999.0)
        self.wafer_count = max(1, int(wafer_count))
        self.step_interval = max(0.0, float(step_interval_sec))
        self.fire_alarm = bool(fire_alarm)
        # SEMI E5 ALCD bits 0-6. Set this to 1 or 2 to exercise the alarm
        # limiter's never-shed guarantee for safety alarms.
        self.alarm_class = int(alarm_class) & 0x7F
        self.loop_lots = bool(loop_lots)
        self.lot_id_prefix = lot_id_prefix
        self._lot_counter = 0
        # State that mutates during a lot, surfaced via S1F3 polling
        self._current_lot_id = ""
        self._current_recipe = "Recipe_Overlay_v3"
        self._current_carrier_id = ""
        # The last collection event this tool fired, for the LastEventID
        # status variable. Distinct from the lot script's `_last_ceid`, which
        # is a de-duplication guard and resets between lots.
        self._last_event_ceid: int = 0
        # name -> SVID for the S1F3/S1F11 handlers. An attribute (not a module
        # import) so ProfileSimulator can answer for a different vendor.
        from eap_middleware.profiles import DAVINCI_SVIDS

        self._svid_names: Dict[str, int] = dict(DAVINCI_SVIDS)
        # Override base handlers so DaVinci-specific SVID/EC values are
        # returned instead of the generic ones from the parent class.
        self.register_stream_function(1, 3, self._handle_s1f3_davinci)
        self.register_stream_function(1, 1, self._handle_s1f1_davinci)
        self.register_stream_function(2, 13, self._handle_s2f13_davinci)
        self.register_stream_function(1, 11, self._handle_s1f11_davinci)
        # Real DaVinci/FabLink accepts S5F3 (Enable/Disable Alarm Send); mirror
        # that so the host's enable_all_alarms() gets a proper S5F4 ACKC5=0.
        self.register_stream_function(5, 3, self._handle_s5f3_davinci)

    # ----- Status / EC handlers (1:1 with real DaVinci responses) -----

    def _handle_s1f1_davinci(self, handler: Any, packet: Any) -> Any:
        return SecsS01F02Extended([
            "DaVinci200",
            "DaVinci200 Version 4.9.3",
        ])

    def _davinci_svid_value(self, svid: int) -> Any:
        """Return a realistic value for a DaVinci SVID. State-dependent
        SVIDs (Clock, current lot/recipe/carrier) are computed live."""
        if svid == 1010005:  # Clock - width follows ECID 4010001 TimeFormat
            # The simulator used to advertise TimeFormat=1 and then send the
            # 12-byte form regardless, so it contradicted its own equipment
            # constant and the 16-byte branch the real default-configured tool
            # uses was never exercised end to end.
            now = datetime.now()
            if int(self._davinci_ecid_value(4010001) or 0) == 0:
                return now.strftime("%y%m%d%H%M%S")            # 12-byte
            return now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 10000:02d}"
        if svid == 1060006:  # PM1/RecipeActive is BOOLEAN (is a recipe loaded?)
            return bool(self._current_recipe)
        if svid == 1060007:
            return self._current_recipe
        if svid == 1070002:
            return ""  # TM1/WaferID
        if svid == 1120001:
            return self._current_carrier_id
        if svid == 1120010:
            return self._current_lot_id
        if svid == 1030001:
            return self.spool_count()
        if svid == 1030002:
            return self._spool_total
        if svid in _DAVINCI_SVID_STATIC_VALUES:
            return _DAVINCI_SVID_STATIC_VALUES[svid]
        return 0  # unknown SVID -> 0 (real DaVinci would return a typed empty)

    def _davinci_ecid_value(self, ecid: int) -> Any:
        if ecid == 4030003:
            return self.tool_id
        return _DAVINCI_ECID_STATIC_VALUES.get(ecid, 0)

    def _handle_s1f3_davinci(self, handler: Any, packet: Any) -> Any:
        """S1F3 SVID values request -> S1F4. Returns the requested SVIDs in
        the order asked, each wrapped in the right secsgem type."""
        try:
            data = self._decode_packet_data(1, 3, packet.data)
            svids = [self._unwrap(item) for item in (data or [])]
            if not svids:
                svids = sorted(set(self._svid_names.values()))
            response = [_typed(self._davinci_svid_value(int(s))) for s in svids]
            return self.stream_function(1, 4)(response)
        except Exception as exc:
            logger.error("[%s] S1F3 handler failed: %s", self.tool_id, exc)
            return self.stream_function(1, 4)([])

    def _handle_s2f13_davinci(self, handler: Any, packet: Any) -> Any:
        """S2F13 ECID values request -> S2F14. Same shape as S1F3."""
        try:
            data = self._decode_packet_data(2, 13, packet.data)
            ecids = [self._unwrap(item) for item in (data or [])]
            if not ecids:
                ecids = sorted(_DAVINCI_ECID_STATIC_VALUES)
            response = [_typed(self._davinci_ecid_value(int(e))) for e in ecids]
            return self.stream_function(2, 14)(response)
        except Exception as exc:
            logger.error("[%s] S2F13 handler failed: %s", self.tool_id, exc)
            return self.stream_function(2, 14)([])

    def _handle_s1f11_davinci(self, handler: Any, packet: Any) -> Any:
        """S1F11 SVID Namelist Request -> S1F12. Reports name + units for
        each requested SVID. Without this, the host can't auto-discover."""
        try:
            data = self._decode_packet_data(1, 11, packet.data)
            svids = [self._unwrap(item) for item in (data or [])]
            id_to_name = {v: k for k, v in self._svid_names.items()}
            if not svids:
                svids = sorted(id_to_name)
            entries = []
            for s in svids:
                svid = int(s)
                name = id_to_name.get(svid, f"SVID_{svid}")
                entries.append([svid, name, ""])
            return self.stream_function(1, 12)(entries)
        except Exception as exc:
            logger.error("[%s] S1F11 handler failed: %s", self.tool_id, exc)
            return self.stream_function(1, 12)([])

    def _handle_s5f3_davinci(self, handler: Any, packet: Any) -> Any:
        """S5F3 Enable/Disable Alarm Send -> S5F4 ACKC5=0 (accepted)."""
        return self.stream_function(5, 4)(0)

    @staticmethod
    def _unwrap(item: Any) -> Any:
        return item.get() if hasattr(item, "get") else item

    def _decode_packet_data(self, stream: int, function: int, data: Any) -> Any:
        """Decode raw S<stream>F<function> bytes using the stream-function
        class. The equipment-side base class doesn't expose this helper, so
        we mirror gateway/host.py's implementation here."""
        if not isinstance(data, bytes):
            return data
        try:
            message = self.stream_function(stream, function)()
            message.decode(data)
            return message.data
        except Exception as exc:
            logger.error(
                "[%s] Failed to decode S%sF%s bytes: %s",
                self.tool_id, stream, function, exc,
            )
            return data

    # Override the parent's auto-event loop so we run our scripted lot.
    def _event_loop(self) -> None:
        logger.info("[%s] DaVinci scripted lot loop ready", self.tool_id)
        while self._running:
            if self.communication_state.current != CommunicationState.COMMUNICATING:
                self._stop_event.wait(0.1)
                continue
            completed = self._run_one_lot()
            if completed and not self.loop_lots:
                logger.info("[%s] Lot finished, idling (loop_lots=False)", self.tool_id)
                while self._running and not self._stop_event.wait(0.2):
                    pass
                return
            if completed:
                # Brief pause between complete lots. A disconnect during this
                # pause naturally returns the loop to its communication wait.
                self._interruptible_wait(self.step_interval * 2)

    # ----- raw S6F11 with explicit V[] -----

    def _send_raw_s6f11(self, ceid: int, v_list: Sequence[Any]) -> bool:
        # Skip if host hasn't enabled this event (S2F37). The middleware
        # subscribes to all DaVinci events at startup so this is normally OK.
        # LastEventID advances when the tool *fires* the event, whether or not
        # the host enabled its report - that is exactly what makes it useful
        # for telling an idle tool from a silent subscription.
        self._last_event_ceid = int(ceid)
        if not self._is_event_enabled(ceid):
            logger.debug("[%s] CEID %s not host-enabled, skipping", self.tool_id, ceid)
            return True
        typed_values = _typed_list(v_list)
        if ceid in {3220013, 3220014, 3220016, 3220017}:
            # The DaVinci manual defines these E90 enum arrays as U1, not
            # strings. IDs/locations remain string arrays.
            for index in (0, 5, 8, 9, 11, 12):
                if index >= len(v_list):
                    continue
                values = v_list[index]
                if not isinstance(values, (list, tuple)):
                    continue
                try:
                    coerced = [int(value) for value in values]
                except (TypeError, ValueError):
                    # Not an enum array after all - leave _typed's own guess in
                    # place rather than killing the event thread over it.
                    logger.warning(
                        "[%s] CEID %s V[%s] is not a U1 enum array: %r",
                        self.tool_id, ceid, index, values,
                    )
                    continue
                typed_values[index] = secs_var.Array(secs_var.U1, coerced)
        s6f11_data = {
            "DATAID": SecsDataTypes.u4(0),
            "CEID": SecsDataTypes.u4(ceid),
            "RPT": [{
                # The one convention every profile and both decoders use:
                # gateway/host.py and eap_middleware/mapper.py each look the
                # report up as RPTID_CEID_OFFSET + CEID, and all four shipped
                # EventSubscription.json files number their reports that way.
                #
                # This used to be `1003000000 + ceid % 1000000`, which is the
                # same number only for DaVinci, whose CEIDs happen to sit in
                # the 3xxxxxx range (1003000000 + 3010004 % 1000000 =
                # 1003010004 = the profile's rptid). For the NexGen MG it gave
                # 1003000213 where the host had defined 1000000213, so the
                # host never matched the report it had asked for and silently
                # fell back to "the first report in the message". That worked
                # only because this simulator sends exactly one - it left the
                # RPTID-keyed decode path, the one real hardware uses,
                # unexercised.
                "RPTID": SecsDataTypes.u4(RPTID_CEID_OFFSET + ceid),
                "V": typed_values,
            }],
        }
        message = self.stream_function(6, 11)(s6f11_data)
        # _send_or_spool owns the log line: it is the only place that knows
        # whether the message went out or was retained.
        return self._send_or_spool(f"S6F11 CEID={ceid}", message)

    def _send_s5f1_alarm(self, alid: int, altx: str, is_set: bool = True) -> bool:
        # ALCD: SEMI E5 says one byte where bit 7 is the set/clear flag and
        # bits 0-6 carry the alarm category. The secsgem S5F1 stream-function
        # spec wants ALCD as a raw integer (not a wrapped Binary item) - the
        # function class handles the byte encoding internally.
        #
        # The category used to be left at 0, which is not a category SEMI E5
        # defines: every alarm reached the middleware as "Code=0". That also
        # meant the alarm rate limiter's guarantee - categories 1 and 2
        # (personal safety, equipment safety) are never shed, however heavy
        # the storm - was never exercised, because the simulator could not
        # produce an alarm of either category.
        alcd_int = (0x80 if is_set else 0x00) | (self.alarm_class & 0x7F)
        body = [alcd_int, alid, altx]
        message = self.stream_function(5, 1)(body)
        return self._send_or_spool(f"S5F1 ALID={alid}", message)

    # ----- the lot script -----

    def _interruptible_wait(self, duration: float) -> bool:
        """Wait for connected time, pausing a partial lot across reconnects."""
        if not self._running:
            return False
        remaining = max(0.0, duration)
        last = time.monotonic()
        while self._running:
            now = time.monotonic()
            if self.communication_state.current == CommunicationState.COMMUNICATING:
                remaining -= now - last
                if remaining <= 0:
                    return True
            last = now
            if self._stop_event.wait(min(0.1, max(remaining, 0.01))):
                return False
        return False

    def _emit_event(self, ceid: int, values: Sequence[Any]) -> bool:
        return self._send_raw_s6f11(ceid, values) and self._interruptible_wait(
            self.step_interval
        )

    def _emit_alarm(self, alid: int, text: str, is_set: bool) -> bool:
        return self._send_s5f1_alarm(alid, text, is_set) and self._interruptible_wait(
            self.step_interval
        )

    def _abandon_lot(self, lot_id: str) -> bool:
        self._current_lot_id = ""
        self._current_carrier_id = ""
        logger.warning(
            "[%s] Abandoning partial lot %s after communication loss; "
            "the next connection starts a fresh lot",
            self.tool_id,
            lot_id,
        )
        return False

    def _run_one_lot(self) -> bool:
        self._lot_counter += 1
        lot_id = f"{self.lot_id_prefix}_{self._lot_counter:04d}"
        ctrl_job_id = f"CJ_{self._lot_counter:04d}"
        carrier_id = f"CARRIER_{self._lot_counter:04d}"
        recipe = "Recipe_Overlay_v3"
        # Mirror state into the SVID values that S1F3 returns
        self._current_lot_id = lot_id
        self._current_recipe = recipe
        self._current_carrier_id = carrier_id

        # 1) Carrier physical arrival (LP1, PortID=1)
        if not self._emit_event(3050001, [1]):
            return self._abandon_lot(lot_id)
        if not self._emit_event(3210006, [carrier_id, 1]):
            return self._abandon_lot(lot_id)
        if not self._emit_event(3210002, [1, 1]):
            return self._abandon_lot(lot_id)

        # 2) ControlJob lifecycle
        if not self._emit_event(3200017, [ctrl_job_id]):
            return self._abandon_lot(lot_id)

        # 3) Per-wafer processing
        for slot in range(1, self.wafer_count + 1):
            wafer_id = f"W{self._lot_counter:04d}_{slot:02d}"

            # E90 NeedsProcessing -> InProcess (substrate list payload)
            if not self._emit_event(3220013, [
                [1],             # SubstIDStatusList (U1 enum)
                [f"LP1.{slot}"], # SubstSubstLocIDList
                ["PM1"],         # SubstDestinationList
                ["LP1"],         # SubstSourceList
                [],              # SubstHistoryList
                [1],             # SubstMtrlStatusList (U1 enum)
                [],              # AcquiredIDList
                [wafer_id],      # SubstIDList
                [3],             # SubstProcStateList (U1 enum)
                [1],             # SubstStateList (U1 enum)
                [lot_id],        # SubstLotIDList
                [0],             # SubstTypeList (U1 enum)
                [0],             # SubstUsageList (U1 enum)
            ]):
                return self._abandon_lot(lot_id)

            # PM1/ProcessingStarted: V = [WaferID, LotID, RecipeName]
            if not self._emit_event(3140002, [wafer_id, lot_id, recipe]):
                return self._abandon_lot(lot_id)

            # PM1/ProcessingFinished with realistic TestResults payload
            test_results = [
                {"die": f"{x},{y}", "v": round(1.0 + (x + y) * 0.01, 3),
                 "p": True}
                for x in range(5) for y in range(5)
            ]
            if not self._emit_event(3140003, [
                wafer_id, lot_id, recipe,
                f"result_{lot_id}_{slot:02d}.csv",
                f"D:/MachineData/EAP_{self.tool_id}/results/",
                f"D:/MachineData/EAP_{self.tool_id}/images/{wafer_id}/",
                test_results,
            ]):
                return self._abandon_lot(lot_id)

            # E90 InProcess -> Processed
            if not self._emit_event(3220016, [
                [1], [f"LP1.{slot}"], ["PM1"], ["LP1"], [],
                [2], [], [wafer_id], [4], [2],
                [lot_id], [0], [0],
            ]):
                return self._abandon_lot(lot_id)

            # Optionally fire an alarm mid-lot to exercise the alarm path
            if self.fire_alarm and slot == 1:
                if not self._emit_alarm(
                    alid=5010001,
                    text="Aligner: Analog Input Channels in Manual Mode",
                    is_set=True,
                ):
                    return self._abandon_lot(lot_id)
                if not self._emit_alarm(
                    alid=5010001,
                    text="Aligner: Analog Input Channels in Manual Mode",
                    is_set=False,
                ):
                    return self._abandon_lot(lot_id)

        # 4) Lot end + carrier depart (this closes the per-lot CSV)
        if not self._emit_event(3200002, [ctrl_job_id]):
            return self._abandon_lot(lot_id)
        if not self._emit_event(3160002, [carrier_id]):
            return self._abandon_lot(lot_id)
        if not self._send_raw_s6f11(3050002, [1]):
            return self._abandon_lot(lot_id)
        self._current_lot_id = ""
        self._current_carrier_id = ""
        logger.info("[%s] Lot %s done (%d wafers)", self.tool_id, lot_id, self.wafer_count)
        return True


# ----- standalone demo entry point -----

def main() -> None:
    parser = argparse.ArgumentParser(description="DaVinci simulator")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (PASSIVE mode)")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--device-id", type=int, default=0)
    parser.add_argument("--wafers", type=int, default=3)
    parser.add_argument("--interval", type=float, default=0.5,
                        help="seconds between events")
    parser.add_argument("--loop", action="store_true",
                        help="keep producing new lots forever")
    parser.add_argument("--no-alarm", action="store_true")
    parser.add_argument("--tool-id", default="DAV_SIM_01")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Match the middleware host's HSMS timer defaults so this standalone main
    # does not run secsgem's library T7=8s against a host stating T7=10s.
    from gateway.host import DEFAULT_HSMS_TIMERS

    settings = secsgem.hsms.HsmsSettings(
        address=args.host,
        port=args.port,
        connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
        session_id=args.device_id,
        **DEFAULT_HSMS_TIMERS,
    )
    sim = SecsGemEquipment(
        settings=settings,
        tool_id=args.tool_id,
        wafer_count=args.wafers,
        step_interval_sec=args.interval,
        fire_alarm=not args.no_alarm,
        loop_lots=args.loop,
    )
    sim.enable()
    sim.start_events()
    print(f"[{args.tool_id}] DaVinci simulator listening on {args.host}:{args.port} "
          f"(PASSIVE). Connect the middleware with hsms_mode='active'. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sim.disable()


if __name__ == "__main__":  # pragma: no cover
    main()
