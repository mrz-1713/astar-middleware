"""Profile primitives: the :class:`MachineProfile` record, the vendor-neutral
transition tags the job tracker keys off, and the helpers that overlay a
site's ``EventSubscription.json`` onto a built-in profile.

This module is vendor-agnostic on purpose - it must stay importable by every
vendor table module without creating a cycle.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from ..models import EventMapping

# Where repo-relative default paths (config/..., output/...) are anchored.
# NOTE: this file sits one level deeper than the old eap_middleware/profiles.py,
# so it needs a third .parent to land on the install root.
_INSTALL_ROOT = Path(__file__).resolve().parent.parent.parent

# Hard-coded rather than __name__: the package split must not rename the
# logger that operators already filter production logs on.
logger = logging.getLogger("eap_middleware.profiles")


# Vendor-neutral transition tags used by JobTracker. Profiles tag CEIDs with
# one of these so the tracker can update per-machine state without knowing
# any vendor specifics.
TRANSITION_LP_ACTIVATE_1 = "lp_activate_1"   # Carrier arrived on LP1 / VCE A
TRANSITION_LP_ACTIVATE_2 = "lp_activate_2"   # Carrier arrived on LP2 / VCE B
TRANSITION_LP_DEACTIVATE_1 = "lp_deactivate_1"
TRANSITION_LP_DEACTIVATE_2 = "lp_deactivate_2"
TRANSITION_CTRL_JOB_START = "ctrl_job_start"  # ControlJob:Selected-Executing
TRANSITION_CTRL_JOB_END = "ctrl_job_end"      # ControlJob:Completed-NoState etc.
# v2 audit fix: DaVinci doesn't subscribe to LP1/2 CarrierArrived (those CEIDs
# have empty Valid Variables, so subscribing would delete the link). Instead
# we drive LP activation from events that DO arrive with a PortID DV payload
# (MaterialReceived, CarrierIDRead, etc.). The tracker reads "PortID" from
# the merged data dict to know which LP to activate.
TRANSITION_LP_ACTIVATE_FROM_PAYLOAD = "lp_activate_from_payload"
TRANSITION_LP_DEACTIVATE_FROM_PAYLOAD = "lp_deactivate_from_payload"


@dataclass(frozen=True)
class MachineProfile:
    """Everything the middleware knows about one machine model.

    A profile is pure data: the SVID, DVID and CEID tables taken from the
    vendor manual, plus the canonical names they are published under.
    """

    profile_id: str
    vendor: str
    model: str
    default_port: int = 5000
    default_secs_device_id: int = 0
    event_aliases: Mapping[str, EventMapping] = field(default_factory=dict)
    ceid_aliases: Mapping[int, str] = field(default_factory=dict)
    svids_by_name: Mapping[str, int] = field(default_factory=dict)
    # Data variable name -> id. Used to label the VID/V pairs in E40 Process Job
    # notifications (S16F9) back to readable DV names. Empty for profiles that
    # don't document their DVs.
    dvs_by_name: Mapping[str, int] = field(default_factory=dict)
    identity_svid_names: List[str] = field(default_factory=list)
    event_subscription_path: Optional[str] = None
    notes: str = ""
    # Per-CEID ordered list of DV names. The mapper uses this to decode the
    # positional V[] array delivered in S6F11 reports. Sourced from the
    # vendor manual's "Valid DVs for Reports" / "Valid Variables" columns.
    ceid_dv_layout: Mapping[int, Sequence[str]] = field(default_factory=dict)
    # Per-RPTID ordered list of DV names. A CEID may carry several reports
    # (DaVinci LP1/CarrierTag sends both the carrier report and the tag report),
    # and ceid_dv_layout only describes the one the host selects. Without this,
    # every other report in the message arrives as unnamed values and is
    # dropped. Populated from the subscription file, which is the only place
    # the RPTID -> DVID mapping actually exists.
    rptid_dv_layout: Mapping[int, Sequence[str]] = field(default_factory=dict)
    # Per-CEID implicit load_port for events whose name encodes the side
    # (DaVinci LP1/*, SPTS *1 = VCE A, *2 = VCE B). Used when the V[] payload
    # doesn't carry a PortID/LoadPort field of its own.
    ceid_load_port: Mapping[int, str] = field(default_factory=dict)
    # Per-CEID implicit chamber for events whose CEID identifies the process
    # module (NexGen MG pm1*/pm2*). Sibling of ceid_load_port: used only when
    # the payload carries no Chamber field of its own. Profiles that leave it
    # empty keep the previous "NA" chamber behaviour.
    ceid_chamber: Mapping[int, str] = field(default_factory=dict)
    # CEIDs for events that happen inside a process chamber (PM1/* etc.) and
    # do NOT themselves identify a load port. The mapper consults JobTracker
    # for these so chamber events get attributed to the originating LP.
    chamber_event_ceids: FrozenSet[int] = field(default_factory=frozenset)
    # CEID -> transition tag (see TRANSITION_* constants). JobTracker uses
    # this to drive its state machine without vendor-specific knowledge.
    ceid_state_transitions: Mapping[int, str] = field(default_factory=dict)
    # Event-liveness health detection. `health_last_event_svid` is a status
    # variable that the equipment increments on EVERY collection event it fires
    # internally, whether or not the linked S6F11 report is delivered to the
    # host (GEM LastCEID/LastEventID semantics). The service polls it while a
    # tool is connected-but-silent: if it advances while zero S6F11 reports have
    # arrived, the subscription is acked-but-ineffective (e.g. the DaVinci
    # HostInterface is in E40 event-report style, sending events on Stream 16
    # instead of S6F11, or reports are spooled) and a health alarm is raised.
    # Left None for profiles where no such SV is known -> feature disabled.
    health_last_event_svid: Optional[int] = None
    health_events_enabled_svid: Optional[int] = None
    # SVID reporting the number of messages the equipment has currently spooled
    # (E30 SpoolCountActual). Polled while a tool is connected-but-silent: if it
    # is > 0 the tool buffered messages during a host outage that the middleware
    # does not auto-drain (no S6F23 Request Spooled Data), so those events may
    # not reach the dashboard. Left None disables the check.
    health_spool_count_svid: Optional[int] = None
    # HSMS-SS protocol timers (SEMI E37 T3/T5/T6/T7/T8, seconds) as documented
    # by THIS vendor. Both manuals that state them allow 1..120 but disagree on
    # the defaults, and a host whose timers do not match the tool's produces
    # intermittent, hard-to-diagnose link drops: the side with the shorter
    # timer declares a failure while the other still considers the transaction
    # open. Empty means "use the shipped default" (the DaVinci values, which
    # were the hardcoded behaviour before this became per-profile).
    hsms_timers: Mapping[str, int] = field(default_factory=dict)

    def resolve_event(self, raw_event: str = "", ceid: int = 0) -> EventMapping:
        if ceid and ceid in self.ceid_aliases:
            alias = self.ceid_aliases[ceid]
            mapped = self.event_aliases.get(alias) or self.event_aliases.get(alias.lower())
            if mapped:
                return mapped
        if raw_event:
            direct = self.event_aliases.get(raw_event)
            if direct:
                return direct
            direct = self.event_aliases.get(raw_event.lower())
            if direct:
                return direct
        fallback = raw_event or (f"CEID_{ceid}" if ceid else "UNKNOWN")
        return EventMapping(
            event_type="unknown",
            csv_tool_event=fallback,
            secs_raw_event=fallback,
        )

    def resolve_svid_name(self, name: str) -> Optional[int]:
        normalized = name.strip().lower()
        for known_name, svid in self.svids_by_name.items():
            if known_name.lower() == normalized:
                return svid
        return None


def profile_with_subscription_file(
    profile: MachineProfile, path: Optional[str]
) -> MachineProfile:
    """Layer a machine's own EventSubscription.json onto its profile.

    That file already decides what the middleware subscribes to (S2F33/35/37),
    so reading `events[].name` and `reports[].dvids` back out gives every
    machine a per-tool CEID map and V[] layout with no code change. This is the
    only way profiles whose CEID numbers are per-installation can work at all:
    ptiq_secsgem deliberately ships zero ceid_aliases because the numbers come
    from each tool's EIB model export.

    Strictly additive - a CEID the profile already documents keeps the vendor
    manual's meaning. A tool that reuses a documented CEID for a *different*
    event still needs a profile change; that is a manual contradiction, not
    configuration.
    """
    file = _locate_subscription_file(path)
    if file is None:
        return profile
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        logger.warning(
            "Ignoring unreadable event subscription file %s: %s", file, exc
        )
        return profile
    if not isinstance(data, dict):
        return profile

    try:
        aliases, layout, report_layout = _overlay_from_subscription(profile, data)
    except (TypeError, ValueError, AttributeError) as exc:
        # Valid JSON can still be the wrong shape - a hand-edited file with
        # "dvid_names": {"LotID": ...} parses fine and then fails on int().
        # One bad file must not stop the machine, let alone the process.
        logger.warning(
            "Ignoring malformed event subscription file %s: %s", file, exc
        )
        return profile

    if (
        aliases == profile.ceid_aliases
        and layout == profile.ceid_dv_layout
        and report_layout == profile.rptid_dv_layout
    ):
        return profile
    # Whatever generic names the file used have to be resolvable afterwards.
    events = dict(profile.event_aliases)
    for alias in aliases.values():
        if alias not in events:
            generic = _known_event_name(profile, alias)
            if generic is not None:
                events[alias] = generic
    return replace(
        profile,
        ceid_aliases=aliases,
        ceid_dv_layout=layout,
        rptid_dv_layout=report_layout,
        event_aliases=events,
    )


def profile_with_ceid_overrides(
    profile: MachineProfile, overrides: Mapping[str, int]
) -> MachineProfile:
    """Return a simulator-scoped profile that also decodes overridden CEIDs."""
    if not overrides:
        return profile
    aliases = dict(profile.ceid_aliases)
    layouts = dict(profile.ceid_dv_layout)
    load_ports = dict(profile.ceid_load_port)
    chambers = dict(profile.ceid_chamber)
    transitions = dict(profile.ceid_state_transitions)
    chamber_events = set(profile.chamber_event_ceids)
    events = dict(profile.event_aliases)

    for event_type, raw_ceid in overrides.items():
        ceid = int(raw_ceid)
        source_ceids = sorted(
            known_ceid
            for known_ceid in profile.ceid_aliases
            if profile.resolve_event(ceid=known_ceid).event_type == event_type
        )
        source = source_ceids[0] if source_ceids else None
        alias = profile.ceid_aliases.get(source) if source is not None else None
        if alias is None:
            alias = next(
                (
                    name
                    for name, mapping in profile.event_aliases.items()
                    if mapping.event_type == event_type
                ),
                event_type,
            )
        mapping = profile.event_aliases.get(alias)
        if mapping is None:
            mapping = next(
                (
                    candidate
                    for candidate in GENERIC_EVENT_ALIASES.values()
                    if candidate.event_type == event_type
                ),
                None,
            )
        if mapping is None:
            continue
        events[alias] = mapping
        aliases[ceid] = alias
        if source is None:
            continue
        if source in layouts:
            layouts[ceid] = layouts[source]
        if source in load_ports:
            load_ports[ceid] = load_ports[source]
        if source in chambers:
            chambers[ceid] = chambers[source]
        if source in transitions:
            transitions[ceid] = transitions[source]
        if source in chamber_events:
            chamber_events.add(ceid)

    return replace(
        profile,
        event_aliases=events,
        ceid_aliases=aliases,
        ceid_dv_layout=layouts,
        ceid_load_port=load_ports,
        ceid_chamber=chambers,
        ceid_state_transitions=transitions,
        chamber_event_ceids=frozenset(chamber_events),
    )


def event_mapping(event_type: str, csv: str, raw: str, closes: bool = False) -> EventMapping:
    return EventMapping(
        event_type=event_type,
        csv_tool_event=csv,
        secs_raw_event=raw,
        closes_lot_file=closes,
    )


def alias_table(entries: Mapping[str, EventMapping]) -> Dict[str, EventMapping]:
    result: Dict[str, EventMapping] = {}
    for alias, mapping in entries.items():
        result[alias] = mapping
        result[alias.lower()] = mapping
    return result


# Vendor-neutral event names. Any profile can be driven by a subscription file
# that uses these, which is what makes a tool with no published CEID table
# (ptiq_secsgem) - or a plain GEM tool of any make - work out of the box. The
# EAP-plan spellings (LOT_START, FOUP_POD_LOAD...) are the ones in the shipped
# config/EventSubscription.json.
GENERIC_EVENT_ALIASES: Dict[str, EventMapping] = alias_table(
    {
        "FOUP_POD_LOAD": event_mapping("loaded", "Loaded", "FOUP_POD_LOAD"),
        "FOUP_POD_UNLOAD": event_mapping(
            "unloaded", "Unloaded", "FOUP_POD_UNLOAD", closes=True
        ),
        "POD_ARRIVED": event_mapping("loaded", "Loaded", "POD_ARRIVED"),
        "POD_REMOVED": event_mapping("unloaded", "Unloaded", "POD_REMOVED", closes=True),
        "MATERIAL_RECEIVED": event_mapping("mounted", "Mounted", "MATERIAL_RECEIVED"),
        "MATERIAL_REMOVED": event_mapping("unmounted", "UnMounted", "MATERIAL_REMOVED"),
        "LOT_START": event_mapping("lot_start", "Lot_Start", "LOT_START"),
        "LOT_END": event_mapping("lot_end", "Lot_End", "LOT_END"),
        "WAFER_ENTER_CHAMBER": event_mapping(
            "wafer_start", "Wfr_Start", "WAFER_ENTER_CHAMBER"
        ),
        "WAFER_LEAVE_CHAMBER": event_mapping(
            "wafer_end", "Wfr_End", "WAFER_LEAVE_CHAMBER"
        ),
        "WAFER_START": event_mapping("wafer_start", "Wfr_Start", "WAFER_START"),
        "WAFER_END": event_mapping("wafer_end", "Wfr_End", "WAFER_END"),
        "PROCESS_START": event_mapping("process_start", "Proc_Start", "PROCESS_START"),
        "PROCESS_END": event_mapping("process_end", "Proc_End", "PROCESS_END"),
        "CLAMPED": event_mapping("clamped", "Clamped", "CLAMPED"),
        "UNCLAMPED": event_mapping("unclamped", "UnClamped", "UNCLAMPED"),
        # Canonical GEM alarm spellings. SPTS alarm CEIDs are computed per
        # tool layout (AlarmID = station*1e7 + station_type*1e5 + offset; ON
        # CEID = ALID + 10000 + offset; OFF CEID = ALID + 1000010000 + offset),
        # so no static SPTS table can name them - a commissioning engineer
        # adds the layout's CEIDs to the machine's EventSubscription.json with
        # these names and the overlay aliases them into the alarm pipeline
        # (rate limiting, dedup, AlarmStormSummary).
        "AlarmNDetected": event_mapping("alarm", "AlarmSet", "AlarmDetected"),
        "AlarmNCleared": event_mapping("alarm", "AlarmCleared", "AlarmCleared"),
        # Canonical GEM spellings, for files written from the SEMI standards
        # rather than from the EAP plan.
        "MaterialReceived": event_mapping("mounted", "Mounted", "MaterialReceived"),
        "MaterialRemoved": event_mapping("unmounted", "UnMounted", "MaterialRemoved"),
        "CarrierArrived": event_mapping("loaded", "Loaded", "CarrierArrived"),
        "CarrierRemoved": event_mapping(
            "unloaded", "Unloaded", "CarrierRemoved", closes=True
        ),
        "LotStarted": event_mapping("lot_start", "Lot_Start", "LotStarted"),
        "LotEnded": event_mapping("lot_end", "Lot_End", "LotEnded"),
        "WaferStarted": event_mapping("wafer_start", "Wfr_Start", "WaferStarted"),
        "WaferComplete": event_mapping("wafer_end", "Wfr_End", "WaferComplete"),
        "ProcessingStarted": event_mapping(
            "process_start", "Proc_Start", "ProcessingStarted"
        ),
        "ProcessingFinished": event_mapping(
            "process_end", "Proc_End", "ProcessingFinished"
        ),
        "ProcessingCompleted": event_mapping(
            "process_end", "Proc_End", "ProcessingCompleted"
        ),
    }
)


def _locate_subscription_file(path: Optional[str]) -> Optional[Path]:
    """The subscription file, found from any working directory.

    Profile defaults are repo-relative (`config/EventSubscription.json`). A
    packaged GUI or simulator is launched from a Start Menu shortcut whose
    working directory is not the install root, so a plain relative path would
    silently miss and the tool's CEIDs would never be applied.
    """
    if not path:
        return None
    candidate = Path(path)
    if candidate.is_file():
        return candidate
    if not candidate.is_absolute():
        beside = _INSTALL_ROOT / candidate
        if beside.is_file():
            return beside
    return None


def _overlay_from_subscription(
    profile: MachineProfile, data: Mapping[str, Any]
) -> Tuple[Dict[int, str], Dict[int, Tuple[str, ...]], Dict[int, Tuple[str, ...]]]:
    """(ceid_aliases, ceid_dv_layout, rptid_dv_layout) for `profile` + `data`."""
    dv_names = {
        int(key): str(value)
        for key, value in (data.get("dvid_names") or {}).items()
    }
    dvids_by_rptid = {
        int(report["rptid"]): [int(dvid) for dvid in report.get("dvids", [])]
        for report in (data.get("reports") or [])
        if isinstance(report, dict) and "rptid" in report
    }

    aliases = dict(profile.ceid_aliases)
    layout: Dict[int, Tuple[str, ...]] = {
        ceid: tuple(names) for ceid, names in profile.ceid_dv_layout.items()
    }
    # Every report gets a layout, not just the one the host selects, so the
    # other reports of a multi-report CEID decode into named values instead of
    # arriving as anonymous numbers. Partially-named reports are kept: an
    # unnamed DVID becomes a positional DV_<n> label rather than sinking the
    # whole report.
    report_layout: Dict[int, Tuple[str, ...]] = {
        rptid: tuple(names)
        for rptid, names in profile.rptid_dv_layout.items()
    }
    for rptid, dvids in dvids_by_rptid.items():
        if not dvids:
            continue
        report_layout[rptid] = tuple(
            dv_names.get(dvid) or f"DV_{dvid}" for dvid in dvids
        )
    for event in data.get("events") or []:
        if not isinstance(event, dict) or "ceid" not in event:
            continue
        try:
            ceid = int(event["ceid"])
        except (TypeError, ValueError):
            continue
        name = str(event.get("name", "")).strip()
        if name and ceid not in aliases and _known_event_name(profile, name):
            aliases[ceid] = name
        if ceid in layout:
            continue
        rptids = [int(rptid) for rptid in event.get("rptids", [])]
        if not rptids:
            continue
        # Same report the host parser picks out of a multi-report S6F11.
        preferred = 1_000_000_000 + ceid
        rptid = preferred if preferred in rptids else rptids[0]
        names = [dv_names.get(dvid, "") for dvid in dvids_by_rptid.get(rptid, [])]
        if names and all(names):
            layout[ceid] = tuple(names)
    return aliases, layout, report_layout


def _known_event_name(
    profile: MachineProfile, name: str
) -> Optional[EventMapping]:
    """Resolve an event name from the profile first, then the generic table."""
    for table in (profile.event_aliases, GENERIC_EVENT_ALIASES):
        mapping = table.get(name) or table.get(name.lower())
        if mapping is not None:
            return mapping
    return None
