"""E40 (Process Job) event ingestion for a DaVinci running in E40 report style.

The DaVinci HostInterface has an INI entry that switches collection-event
delivery between two styles (manual §4.2.1):

  * E30 style - rich per-CEID reports via **S6F11** (the path this middleware
    is built around: 39 curated CEIDs covering E87 carrier, E90 substrate, E94
    job, alarms, ...).
  * E40 style - collection events are delivered as Process Job notifications on
    **Stream 16** (S16F9 Process Job Event Notify / S16F7 Process Job Alert
    Notify) instead of S6F11.

secsgem 0.3.0 ships no classes for the S16 process-job functions, so this
module defines the minimal ones and decodes them, mapping each notification
onto the SAME canonical pipeline used for S6F11 by reusing the profile's
existing ``PRJobMS_*`` / ``PRJobStateChange`` event aliases.

IMPORTANT: E40 data is intrinsically coarser than E30 - it only carries
process-job lifecycle, not the per-CEID carrier/substrate/alarm granularity. So
the *recommended* fix for an E40 tool remains switching it back to E30. This
module is the safety net: a tool stuck in E40 still delivers lot/job lifecycle
to the dashboard (tagged ``_e40``) instead of silent dead air.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from secsgem.secs.data_items import (
    ACKA,
    ERRCODE,
    ERRTEXT,
    TIMESTAMP,
    V,
    VID,
    DataItemBase,
)
from secsgem.secs.functions.base import SecsStreamFunction
from secsgem.secs.variables import String, U4


# ── E40-specific data items absent from secsgem 0.3.0 ────────────────────────
class PRJOBID(DataItemBase):
    """Process job identifier (ASCII)."""

    __type__ = String


class PREVENTID(DataItemBase):
    """Processing-related event identifier (U4). 1=Waiting for material,
    2=Job state change (manual Data Item Dictionary, p.248)."""

    __type__ = U4


class PRJOBMILESTONE(DataItemBase):
    """Process-job milestone (U4). 1=Setup, 2=Processing, 3=ProcessingComplete,
    4=Complete, 5=WaitingForStart (manual Data Item Dictionary, p.248)."""

    __type__ = U4


# ── S16 process-job functions (manual §4.4.3.10 tables 143/141) ──────────────
class SecsS16F09(SecsStreamFunction):
    """Process Job Event Notify (PRJE): L,4[PREVENTID, TIMESTAMP, PRJOBID,
    L,n[L,2[VID, V]]]. Equipment -> Host, reply required (S16F10)."""

    _stream = 16
    _function = 9
    _data_format = [PREVENTID, TIMESTAMP, PRJOBID, [[VID, V]]]
    _to_host = True
    _to_equipment = False
    _has_reply = True
    _is_reply_required = True
    _is_multi_block = True


class SecsS16F07(SecsStreamFunction):
    """Process Job Alert Notify (PRJA): L,4[TIMESTAMP, PRJOBID, PRJOBMILESTONE,
    L,2[ACKA, L,n[L,2[ERRCODE, ERRTEXT]]]]. Equipment -> Host, reply (S16F8)."""

    _stream = 16
    _function = 7
    _data_format = [TIMESTAMP, PRJOBID, PRJOBMILESTONE, [ACKA, [[ERRCODE, ERRTEXT]]]]
    _to_host = True
    _to_equipment = False
    _has_reply = True
    _is_reply_required = True
    _is_multi_block = True


class SecsS16F10(SecsStreamFunction):
    """Process Job Event Confirm (PRJEC): header only."""

    _stream = 16
    _function = 10
    _data_format = None
    _to_host = False
    _to_equipment = True
    _has_reply = False
    _is_reply_required = False
    _is_multi_block = False


class SecsS16F08(SecsStreamFunction):
    """Process Job Alert Confirm (PRJAC): header only."""

    _stream = 16
    _function = 8
    _data_format = None
    _to_host = False
    _to_equipment = True
    _has_reply = False
    _is_reply_required = False
    _is_multi_block = False


# PRJOBMILESTONE -> existing davinci event alias (see profiles.davinci_events).
_MILESTONE_TO_RAW_EVENT = {
    1: "PRJobMS_Setup",
    2: "PRJobMS_Processing",
    3: "PRJobMS_ProcessingComplete",
    4: "PRJobMS_Complete",
    5: "PRJobMS_WaitingForStart",
}

# PRJobStateEnum (manual p.159) -> existing davinci event alias. Used when an
# S16F9 carries the PRJobState DV (2130002) so we can derive lifecycle.
_PRJOBSTATE_TO_RAW_EVENT = {
    1: "PRJobMS_Setup",            # SETTING UP
    2: "PRJobMS_WaitingForStart",  # WAITING FOR START
    3: "PRJobMS_Processing",       # PROCESSING
    4: "PRJobMS_ProcessingComplete",  # PROCESS COMPLETE
    10: "PRJobMS_Complete",        # STOPPED  -> lot_end
    11: "PRJobMS_Complete",        # ABORTED  -> lot_end
}

# PREVENTID -> raw event when no more specific state is available.
_PREVENTID_TO_RAW_EVENT = {
    1: "PRJobMS_WaitingForStart",  # Waiting for material
    2: "PRJobStateChange",         # Job state change
}


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_s16f9(
    decoded: Mapping[str, Any],
    dv_name_by_id: Mapping[int, str],
) -> Tuple[str, Dict[str, Any]]:
    """Map a decoded S16F9 (PRJE) dict to (raw_event_name, canonical data dict).

    The returned data dict feeds straight into CanonicalMapper.from_secs_event
    with ceid=0: SECSGEM_RAW_EVENT drives alias resolution and the named DV
    values let the mapper pull out lot/wafer/recipe just like an S6F11 report.
    """
    preventid = _coerce_int(decoded.get("PREVENTID"))
    prjobid = decoded.get("PRJOBID")
    data: Dict[str, Any] = {
        "_e40": True,
        "PRJobID": prjobid,
        "PREVENTID": preventid,
    }
    # Name each VID/V pair against the profile's DV map.
    for pair in decoded.get("DATA", []) or []:
        if not isinstance(pair, Mapping):
            continue
        vid = _coerce_int(pair.get("VID"))
        if vid is None:
            continue
        name = dv_name_by_id.get(vid, f"DV_{vid}")
        data[name] = pair.get("V")
    # Prefer the explicit PRJobState DV to choose the lifecycle event; otherwise
    # fall back to the PREVENTID class.
    state = _coerce_int(data.get("PRJobState"))
    raw_event = (
        _PRJOBSTATE_TO_RAW_EVENT.get(state)
        if state is not None
        else None
    ) or (
        _PREVENTID_TO_RAW_EVENT.get(preventid, "PRJobStateChange")
        if preventid is not None
        else "PRJobStateChange"
    )
    data["SECSGEM_RAW_EVENT"] = raw_event
    return raw_event, data


def parse_s16f7(
    decoded: Mapping[str, Any],
    dv_name_by_id: Mapping[int, str],
) -> Tuple[str, Dict[str, Any]]:
    """Map a decoded S16F7 (PRJA) dict to (raw_event_name, canonical data dict).

    S16F7 carries the PRJOBMILESTONE which is the cleanest lot/job lifecycle
    signal (Setup/Processing/Complete)."""
    milestone = _coerce_int(decoded.get("PRJOBMILESTONE"))
    prjobid = decoded.get("PRJOBID")
    raw_event = (
        _MILESTONE_TO_RAW_EVENT.get(milestone, "PRJobStateChange")
        if milestone is not None
        else "PRJobStateChange"
    )
    data: Dict[str, Any] = {
        "_e40": True,
        "PRJobID": prjobid,
        "PRJobMilestone": milestone,
        "SECSGEM_RAW_EVENT": raw_event,
    }
    ack_block = decoded.get("ACK", decoded.get("DATA", {}))
    if not isinstance(ack_block, Mapping):
        ack_block = {}
    outcome = ack_block.get("ACKA", decoded.get("ACKA"))
    errors = ack_block.get(
        "ERRORS",
        ack_block.get("ERRS", ack_block.get("DATA", decoded.get("ERRORS", []))),
    ) or []
    data["PRJobAlertAccepted"] = (
        bool(outcome) if outcome is not None else None
    )
    data["PRJobErrors"] = (
        list(errors) if isinstance(errors, (list, tuple)) else [errors]
    )
    return raw_event, data
