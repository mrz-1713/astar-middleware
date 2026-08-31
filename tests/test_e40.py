"""E40 (Process Job) event ingestion: a DaVinci in E40 report style delivers
collection events as S16F9/S16F7 instead of S6F11. These tests pin the custom
S16 function wire format, the milestone/state -> canonical-event mapping, and
that the result flows through CanonicalMapper to the same event types as the
equivalent E30 events."""

from __future__ import annotations

from gateway import e40
from eap_middleware.job_tracker import JobTracker
from eap_middleware.mapper import CanonicalMapper
from eap_middleware.models import MachineConfig
from eap_middleware.profiles import DAVINCI_DVS, ProfileRegistry


def _machine() -> MachineConfig:
    return MachineConfig(
        endpoint_id="TOOL_02", display_name="DAVINCI200_MC4_HC1_01",
        machine_profile="davinci_200_mc4_hc1", host="10.0.0.2", port=5000,
    )


def _dv_name_by_id():
    return {v: k for k, v in DAVINCI_DVS.items()}


# ── wire format: custom S16 functions round-trip ─────────────────────────────

def test_s16f9_round_trips():
    msg = e40.SecsS16F09({
        "PREVENTID": 2, "TIMESTAMP": "20260630120000", "PRJOBID": "PRJOB_42",
        "DATA": [{"VID": 2130001, "V": "PRJOB_42"}, {"VID": 2130002, "V": 3}],
    })
    clone = e40.SecsS16F09()
    clone.decode(msg.encode())
    got = clone.get()
    assert got["PRJOBID"] == "PRJOB_42"
    assert got["PREVENTID"] == 2
    assert {p["VID"] for p in got["DATA"]} == {2130001, 2130002}


def test_s16f7_round_trips():
    msg = e40.SecsS16F07({
        "TIMESTAMP": "20260630120000", "PRJOBID": "PRJOB_42",
        "PRJOBMILESTONE": 4, "DATA": {"ACKA": True, "DATA": []},
    })
    clone = e40.SecsS16F07()
    clone.decode(msg.encode())
    assert clone.get()["PRJOBMILESTONE"] == 4


def test_s16_confirmations_are_header_only_and_primaries_require_reply():
    assert e40.SecsS16F10().encode() == b""
    assert e40.SecsS16F08().encode() == b""
    assert e40.SecsS16F09._has_reply is True
    assert e40.SecsS16F09._is_reply_required is True
    assert e40.SecsS16F07._has_reply is True
    assert e40.SecsS16F07._is_reply_required is True


# ── parse: milestone / state -> raw event alias ──────────────────────────────

def test_parse_s16f7_milestone_to_alias():
    raw, data = e40.parse_s16f7({"PRJOBID": "J1", "PRJOBMILESTONE": 4}, _dv_name_by_id())
    assert raw == "PRJobMS_Complete"
    assert data["_e40"] is True
    assert data["PRJobID"] == "J1"

    raw2, _ = e40.parse_s16f7({"PRJOBID": "J1", "PRJOBMILESTONE": 2}, _dv_name_by_id())
    assert raw2 == "PRJobMS_Processing"


def test_parse_s16f7_preserves_alert_outcome_and_errors():
    _raw, data = e40.parse_s16f7(
        {
            "PRJOBID": "J1",
            "PRJOBMILESTONE": 4,
            "DATA": {
                "ACKA": False,
                "DATA": [{"ERRCODE": 17, "ERRTEXT": "aborted"}],
            },
        },
        _dv_name_by_id(),
    )
    assert data["PRJobAlertAccepted"] is False
    assert data["PRJobErrors"] == [{"ERRCODE": 17, "ERRTEXT": "aborted"}]


def test_parse_s16f9_uses_prjobstate_dv():
    # PRJobState DV (2130002) = 3 (PROCESSING) -> PRJobMS_Processing
    raw, data = e40.parse_s16f9(
        {"PREVENTID": 2, "PRJOBID": "J9",
         "DATA": [{"VID": 2130002, "V": 3}, {"VID": 2130001, "V": "J9"}]},
        _dv_name_by_id(),
    )
    assert raw == "PRJobMS_Processing"
    assert data["PRJobState"] == 3
    assert data["PRJobID"] == "J9"


def test_parse_s16f9_falls_back_to_preventid():
    raw, _ = e40.parse_s16f9({"PREVENTID": 1, "PRJOBID": "J9", "DATA": []}, _dv_name_by_id())
    assert raw == "PRJobMS_WaitingForStart"


# ── integration: E40 event -> CanonicalMapper -> canonical event type ────────

def test_e40_complete_maps_to_lot_end():
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    mapper = CanonicalMapper(profile, tracker=JobTracker())
    _raw, data = e40.parse_s16f7({"PRJOBID": "LOT_77", "PRJOBMILESTONE": 4}, _dv_name_by_id())
    event = mapper.from_secs_event(_machine(), 0, data)
    assert event.event_type == "lot_end"
    assert event.raw_payload.get("PRJobID") == "LOT_77"


def test_e40_processing_maps_to_process_start():
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    mapper = CanonicalMapper(profile, tracker=JobTracker())
    _raw, data = e40.parse_s16f7({"PRJOBID": "LOT_77", "PRJOBMILESTONE": 2}, _dv_name_by_id())
    event = mapper.from_secs_event(_machine(), 0, data)
    assert event.event_type == "process_start"


def test_e40_event_not_unknown():
    # Regression: every E40 milestone must resolve to a real event type, never
    # "unknown" (which would silently drop the event from CSV/dashboard).
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    mapper = CanonicalMapper(profile, tracker=JobTracker())
    for milestone in (1, 2, 3, 4, 5):
        _raw, data = e40.parse_s16f7({"PRJOBID": "J", "PRJOBMILESTONE": milestone}, _dv_name_by_id())
        event = mapper.from_secs_event(_machine(), 0, data)
        assert event.event_type != "unknown", f"milestone {milestone} -> unknown"


# ── spool drain uses the Transmit code ───────────────────────────────────────

def test_spool_uses_transmit_code():
    import secsgem.hsms
    s = secsgem.hsms.HsmsSettings(
        address="127.0.0.1", port=5000,
        connect_mode=secsgem.hsms.HsmsConnectMode.ACTIVE, session_id=0,
    )
    assert s.data_items.RSDC.TRANSMIT == 0  # 0 = transmit (not 1 = purge)
