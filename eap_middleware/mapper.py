"""Map vendor-specific SECS/GEM payloads into canonical middleware events."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .job_tracker import JobTracker
from .models import CanonicalEvent, MachineConfig, utc_now
from .profiles import MachineProfile

logger = logging.getLogger(__name__)

# Profile convention, mirrored from gateway.host: each CEID's own report is
# numbered CEID + this offset.
RPTID_CEID_OFFSET = 1_000_000_000

# Module-level dedup for unknown-CEID warnings: a single (profile, machine,
# ceid) only warns once. The machine is part of the key so a CEID drift on one
# tool cannot silence the warning for a second tool on the same profile.
_UNKNOWN_CEID_WARNED: Set[Tuple[str, str, int]] = set()


def _scalar(value: Any) -> str:
    """Coerce a SECS value (possibly a list from an E90 substrate report)
    into a single scalar string. Lists collapse to their first non-empty
    element so SubstLotIDList=['LOT_M42'] becomes 'LOT_M42'."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                return text
        return ""
    text = str(value).strip()
    return text


def _get_first(data: Mapping[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        if key in data and data[key] is not None:
            text = _scalar(data[key])
            if text:
                return text
    return default


def _get_int(data: Mapping[str, Any], *keys: str, default: int = 0) -> int:
    raw = _get_first(data, *keys)
    if raw == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _aware(value: datetime) -> datetime:
    """Attach the middleware host's zone to an equipment clock reading.

    Every SECS clock format in this function - the SPTS 16-byte Clock, the
    DaVinci 12/16-byte forms, the NexGen CLOCK DVVAL - is bare wall time with
    no zone, and `received_at` was the same until it started carrying an
    offset. `CanonicalEvent.timestamp` must be uniformly aware, because a
    field that is naive for one event and aware for the next cannot be
    compared, formatted or rendered consistently:

      * `csv_store._filename` took the lot file's timestamp from whichever
        path filled `LotBuffer.start_timestamp` - the event itself (naive
        equipment local) or a re-parsed pending row (aware, and stamped UTC),
        so two lots off one tool could be named on two different clocks;
      * `CanonicalEvent.to_dict()` emitted an ISO string with no offset,
        which is unresolvable once it leaves the process.

    `datetime.astimezone()` on a naive value interprets it as host-local,
    which is what `timestamp_ms()` has always done implicitly (a naive
    `.timestamp()` uses the local zone) - so the epoch this yields is
    unchanged. It makes the existing assumption explicit rather than adopting
    a new one: the tool and the middleware are on the same site clock. A tool
    deliberately kept on a different zone must be corrected at the tool,
    which is also where the operator reads the same wall time back.
    """
    return value if value.tzinfo is not None else value.astimezone()


# A tool's own RTC can be wrong by a lot more than any real HSMS/network
# delay - a resumed VM snapshot on the FabNet test rig has come back hours
# or a full day off, and that clock reaching Linkstuffs unchecked is what put
# a "last update" a day in the future on the dashboard. Past this bound the
# tool clock and the middleware's own receipt time disagree enough that it is
# worth a log line even though the tool's reading is never the one used below.
_CLOCK_DISAGREEMENT_LOG_THRESHOLD = timedelta(hours=1)


def _parse_received_at(data: Mapping[str, Any]) -> Optional[datetime]:
    """The middleware's own receipt time, if this event carries one.

    Distinct from `_parse_clock`, which prefers the tool's DATETIME/CLOCK
    over this same field - this looks at `timestamp`/`received_at` alone,
    which `gateway.host` stamps with `datetime.now().astimezone()` at the
    moment the message actually arrived, independent of the equipment's own
    RTC.
    """
    raw = _get_first(data, "timestamp", "received_at")
    if not raw:
        return None
    try:
        return _aware(datetime.fromisoformat(raw))
    except ValueError:
        return None


def _parse_timestamp(data: Mapping[str, Any]) -> datetime:
    """The event's timestamp: the middleware PC's own receipt clock
    (`timestamp`/`received_at`) whenever the event carries one, never the
    tool's self-reported CLOCK/DATETIME - see `_parse_received_at`. Only
    events with no receipt stamp at all (direct test fixtures, mainly) fall
    back to parsing the tool's own clock via `_parse_clock`.
    """
    received = _parse_received_at(data)
    parsed = _aware(_parse_clock(data))
    if received is None:
        return parsed
    if abs(parsed - received) > _CLOCK_DISAGREEMENT_LOG_THRESHOLD:
        logger.warning(
            "Tool clock %s is %s from the middleware's received_at %s - "
            "using received_at; check the tool/VM system clock.",
            parsed.isoformat(), abs(parsed - received), received.isoformat(),
        )
    return received


def _parse_clock(data: Mapping[str, Any]) -> datetime:
    raw = _get_first(data, "DATETIME", "Datetime", "CLOCK", "Clock", "timestamp", "received_at")
    if not raw:
        return utc_now()
    # SPTS 16-byte Clock format yyyymmddhhmmsscc (Section 12.4) and DaVinci
    # 12/16-byte formats per ECID 4010001 / TimeFormat. Parse by length when
    # the value is all digits.
    digits = raw if raw.isdigit() else ""
    if len(digits) == 16:
        # YYYYMMDDhhmmsscc - last 2 chars are centiseconds -> *10_000 -> usec.
        try:
            base = datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
            return base.replace(microsecond=int(digits[14:16]) * 10_000)
        except ValueError:
            pass
    if len(digits) == 12:
        # YYMMDDhhmmss legacy short form (SPTS TimeFormat=0)
        try:
            return datetime.strptime(digits, "%y%m%d%H%M%S")
        except ValueError:
            pass
    if len(digits) == 14:
        try:
            return datetime.strptime(digits, "%Y%m%d%H%M%S")
        except ValueError:
            pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y%m%d%H%M%S%f",
        "%Y%m%d%H%M%S",
        "%Y%m%d_%H%M%S_%f",
    ):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return utc_now()


def _as_rptid(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _report_layout(
    profile: MachineProfile, rptid: Optional[int], ceid: int
) -> Optional[Sequence[str]]:
    """DV names for one report, by RPTID."""
    if rptid is None:
        return None
    layout = profile.rptid_dv_layout.get(rptid)
    if layout:
        return layout
    # Profile convention: a CEID's own report is numbered CEID + this offset,
    # so a profile that only documents per-CEID layouts still decodes its
    # primary report.
    if ceid and rptid == RPTID_CEID_OFFSET + int(ceid):
        return profile.ceid_dv_layout.get(int(ceid))
    return None


def _merge_v_array(
    data: Mapping[str, Any],
    profile: MachineProfile,
    ceid: int,
) -> Dict[str, Any]:
    """Decode the S6F11 reports into named keys using the profile's layouts.

    Every report in the message is decoded, not only the one the host handler
    selected: a CEID can carry several reports (DaVinci LP1/CarrierTag sends
    the carrier report and the tag report together), and decoding just one
    threw the rest away. The selected report is applied last so it stays
    authoritative wherever two reports use the same DV name.

    The host parser's hardcoded EAP-plan keys (DATETIME, LOAD_PORT, LOT_ID...)
    still survive when they don't collide with the profile layout. The
    mapper's downstream `_get_first` calls list the profile-decoded names
    FIRST so they shadow the EAP-plan positional contaminants."""
    merged: Dict[str, Any] = dict(data)
    selected = _as_rptid(data.get("_rptid"))
    for report in data.get("_reports_raw") or []:
        if not isinstance(report, Mapping):
            continue
        rptid = _as_rptid(report.get("rptid"))
        values = report.get("values") or []
        if rptid is None or not values or rptid == selected:
            continue
        layout = _report_layout(profile, rptid, ceid)
        if not layout:
            continue
        for index, dv_name in enumerate(layout):
            if index >= len(values):
                break
            if dv_name:
                merged.setdefault(dv_name, values[index])

    raw = data.get("_v_raw")
    layout = profile.ceid_dv_layout.get(ceid) if ceid else None
    if not layout:
        layout = _report_layout(profile, selected, ceid)
    if not raw or not layout:
        return merged
    for index, dv_name in enumerate(layout):
        if index >= len(raw):
            break
        merged[dv_name] = raw[index]
    return merged


class CanonicalMapper:
    """Turns one machine's raw SECS events into canonical events and CSV rows.

    Every vendor specific comes from the machine's profile; the job tracker
    supplies the lot and load-port context the raw event does not carry.
    """

    def __init__(
        self,
        profile: MachineProfile,
        tracker: Optional[JobTracker] = None,
    ):
        self.profile = profile
        # Optional - when None, chamber-event LP fallback is a no-op so
        # standalone mapper usage (existing unit tests) keeps working.
        self.tracker = tracker

    def from_secs_event(
        self,
        machine: MachineConfig,
        ceid: int,
        data: Mapping[str, Any],
    ) -> CanonicalEvent:
        """Compatibility wrapper for callers that expect one event."""
        events = self.from_secs_events(machine, ceid, data)
        if not events:
            raise ValueError(f"CEID {ceid} produced no canonical events")
        return events[0]

    def from_secs_events(
        self,
        machine: MachineConfig,
        ceid: int,
        data: Mapping[str, Any],
    ) -> List[CanonicalEvent]:
        """Map one SECS report, expanding aligned E90 substrate arrays."""
        merged = _merge_v_array(data, self.profile, ceid)
        layout = self.profile.ceid_dv_layout.get(ceid, ())
        list_keys = [name for name in layout if name.endswith("List")]
        lengths = [
            len(merged[name])
            for name in list_keys
            if isinstance(merged.get(name), (list, tuple))
        ]
        count = max(lengths, default=0)
        if count == 0:
            return [self._from_merged_event(machine, ceid, merged)]
        source_lists = {
            name: merged.get(name) for name in list_keys if name in merged
        }
        events: List[CanonicalEvent] = []
        for index in range(count):
            row = dict(merged)
            row.pop("_v_raw", None)
            row["_e90_index"] = index
            row["_e90_count"] = count
            row["_e90_source_lists"] = source_lists
            for name in list_keys:
                value = merged.get(name)
                if isinstance(value, (list, tuple)):
                    row[name] = value[index] if index < len(value) else None
            events.append(self._from_merged_event(machine, ceid, row))
        return events

    def _from_merged_event(
        self,
        machine: MachineConfig,
        ceid: int,
        data: Mapping[str, Any],
    ) -> CanonicalEvent:

        raw_event = _get_first(
            data,
            "SECSGEM_RAW_EVENT",
            "SECSGEM Raw Event",
            "raw_event_name",
            "RawEvent",
            "TOOL_EVENT",
            "ToolEvent",
        )
        mapping = self.profile.resolve_event(raw_event=raw_event, ceid=ceid)

        # Fix #1: log unknown CEIDs once per (profile, ceid) so silent CSV
        # drops become visible without flooding the log.
        if mapping.event_type == "unknown":
            key = (self.profile.profile_id, machine.endpoint_id, ceid)
            if key not in _UNKNOWN_CEID_WARNED:
                _UNKNOWN_CEID_WARNED.add(key)
                logger.warning(
                    "Unknown CEID %s on %s/%s (raw_event=%r) - event will be "
                    "captured as 'unknown' but won't be aliased; update the "
                    "profile if this CEID is expected.",
                    ceid, self.profile.profile_id, machine.endpoint_id, raw_event,
                )

        # Distinguish two callers of this method:
        #   1. Host parser (real S6F11): puts raw V[] in `_v_raw` and ALSO
        #      labels each V[i] with a hardcoded EAP-plan key (LOAD_PORT,
        #      LOT_ID, ...). For real equipment those labels are wrong - V[3]
        #      is whatever the vendor's DV layout says, not LOAD_PORT. We must
        #      ignore them when a profile layout exists.
        #   2. Direct test fixture / legacy caller: no `_v_raw`, but may pass
        #      EAP-plan keys (LOT_ID="...") on purpose. Those are legitimate.
        # The presence of `_v_raw` is the unambiguous signal.
        positional_labels_are_safe = "_v_raw" not in data
        lot_id_keys = ["LotID", "LOTID", "Lot", "SubstLotIDList"]
        load_port_keys = ["PortID", "LoadPort", "Load Port"]
        wafer_id_keys = ["WaferID", "WAFERID", "SubstID", "SubstIDList"]
        recipe_keys = ["RecipeName", "RecipeID", "RCPID", "Recipe"]
        if positional_labels_are_safe:
            # LOT_START_TIME/LotStartTime are deliberately NOT here: a
            # timestamp is not an identifier, and _get_first would make
            # it the lot id (and the CSV filename) for any report that
            # carries a start time but no LOT_ID.
            lot_id_keys += ["LOT_ID"]
            load_port_keys += ["LOAD_PORT"]
            wafer_id_keys += ["WAFER_ID"]
            recipe_keys += ["RECIPE"]
        lot_id = _get_first(data, *lot_id_keys)
        load_port = _get_first(data, *load_port_keys)
        if not load_port:
            # Fix #4: many vendor CEIDs encode the load port in the CEID name
            # (DaVinci LP1/CarrierArrived = port 1, SPTS *2 family = VCE B).
            # Fall back to the profile's implicit map so concurrent lots on
            # different ports never share a per-lot CSV bucket.
            load_port = self.profile.ceid_load_port.get(ceid, "")

        # v2 Track A: keep the job tracker up to date on lifecycle CEIDs
        # (carrier arrive/depart, ControlJob start/end). Done BEFORE the
        # chamber-event fallback so an arriving carrier's own event has the
        # tracker state ready when the next chamber event fires.
        if self.tracker is not None:
            tracker_data = dict(data)
            if load_port:
                tracker_data["_resolved_load_port"] = load_port
            self.tracker.note_event(
                machine.endpoint_id, self.profile, ceid, tracker_data
            )

        chamber = _get_first(
            data, "CHAMBER", "Chamber", "Cham",
            # Same shape as the load_port fallback above: some vendors
            # encode the process module in the CEID rather than the
            # payload (NexGen MG pm1*/pm2*). Empty map -> "NA" as before.
            default=self.profile.ceid_chamber.get(ceid, "NA"),
        )

        # v2 Track A: chamber-event LP attribution. PM/recipe CEIDs in the
        # profile's chamber_event_ceids set don't name their own LP - ask
        # the tracker which carrier is currently being processed. The chamber
        # goes with the question: on a tool running two lots in two chambers
        # there is no single "currently processing" carrier, and only the
        # chamber distinguishes them.
        if not load_port and self.tracker is not None and ceid in self.profile.chamber_event_ceids:
            inferred = self.tracker.lookup_lp(
                machine.endpoint_id, ceid, data, chamber=chamber
            )
            if inferred:
                load_port = inferred
        if load_port and self.tracker is not None:
            self.tracker.note_resolution(machine.endpoint_id, data, load_port)
        return CanonicalEvent(
            timestamp=_parse_timestamp(data),
            endpoint_id=machine.endpoint_id,
            display_name=machine.display_name,
            machine_profile=machine.machine_profile,
            vendor=self.profile.vendor,
            model=self.profile.model,
            event_type=mapping.event_type,
            raw_event_name=raw_event or mapping.secs_raw_event,
            ceid=ceid,
            load_port=load_port,
            chamber=chamber,
            lot_id=lot_id,
            wafer_id=_get_first(data, *wafer_id_keys),
            recipe=_get_first(data, *recipe_keys),
            secs_raw_event=mapping.secs_raw_event,
            raw_payload=dict(data),
        )

    def alarm_event(
        self,
        machine: MachineConfig,
        alarm_data: Mapping[str, Any],
    ) -> CanonicalEvent:
        alid = _get_int(alarm_data, "alid", "ALID")
        altx = _get_first(alarm_data, "altx", "ALTX", "alarm_text")
        is_set = bool(alarm_data.get("is_set", True))
        return CanonicalEvent(
            timestamp=_parse_timestamp(alarm_data),
            endpoint_id=machine.endpoint_id,
            display_name=machine.display_name,
            machine_profile=machine.machine_profile,
            vendor=self.profile.vendor,
            model=self.profile.model,
            event_type="alarm",
            raw_event_name="AlarmSet" if is_set else "AlarmCleared",
            ceid=alid,
            secs_raw_event=altx,
            raw_payload=dict(alarm_data),
        )

    def svid_event(
        self,
        machine: MachineConfig,
        svid_values: Mapping[int, Any],
    ) -> CanonicalEvent:
        # First occurrence wins so an SVID that legitimately carries two names
        # (4306: `mapResultLastMap`, the manual's status-variable name, and
        # `SlotMapGem`, the CEID-145 report alias that disambiguates the slot
        # encoding) keeps its canonical label in S1F3 samples rather than the
        # last-alias-wins label a comprehension would pick.
        name_by_id: Dict[int, str] = {}
        for name, svid in self.profile.svids_by_name.items():
            name_by_id.setdefault(svid, name)
        payload: Dict[str, Any] = {}
        for svid, value in svid_values.items():
            name = name_by_id.get(int(svid), f"SVID_{svid}")
            payload[f"svid_{name}"] = value
        return CanonicalEvent(
            timestamp=utc_now(),
            endpoint_id=machine.endpoint_id,
            display_name=machine.display_name,
            machine_profile=machine.machine_profile,
            vendor=self.profile.vendor,
            model=self.profile.model,
            event_type="svid_sample",
            raw_event_name="SVID_SAMPLE",
            raw_payload=payload,
        )

    def connection_event(
        self,
        machine: MachineConfig,
        state: str,
        details: str = "",
    ) -> CanonicalEvent:
        return CanonicalEvent(
            timestamp=utc_now(),
            endpoint_id=machine.endpoint_id,
            display_name=machine.display_name,
            machine_profile=machine.machine_profile,
            vendor=self.profile.vendor,
            model=self.profile.model,
            event_type="connection_state",
            raw_event_name=state,
            secs_raw_event=details,
            raw_payload={"connection_state": state, "details": details},
        )
