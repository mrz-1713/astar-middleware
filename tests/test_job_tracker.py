"""Unit tests for JobTracker - the per-machine LP attribution state."""

from __future__ import annotations

from eap_middleware.job_tracker import JobTracker
from eap_middleware.profiles import (
    TRANSITION_CTRL_JOB_END,
    TRANSITION_CTRL_JOB_START,
    TRANSITION_LP_ACTIVATE_1,
    TRANSITION_LP_ACTIVATE_2,
    TRANSITION_LP_DEACTIVATE_1,
    TRANSITION_LP_DEACTIVATE_2,
    MachineProfile,
)


def _profile_with_transitions(**transitions: str) -> MachineProfile:
    """Tiny synthetic profile so tests don't depend on vendor profiles."""
    return MachineProfile(
        profile_id="test",
        vendor="Test",
        model="Test",
        ceid_state_transitions=transitions,
    )


def test_post_restart_lookup_returns_none_until_carrier_arrival():
    tracker = JobTracker()
    assert tracker.lookup_lp("M1", ceid=999, data={}) is None


def test_carrier_arrival_sets_active_lp():
    tracker = JobTracker()
    # profile.ceid_state_transitions is keyed by int in production; this
    # mini-profile keys by str to demo - normalize via direct call instead.
    # (Real wiring uses int CEIDs - see populate-step tests.)
    tracker._apply_transition("M1", TRANSITION_LP_ACTIVATE_1, {})
    assert tracker.lookup_lp("M1", ceid=0, data={}) == "1"


def test_ctrl_job_id_resolves_to_lp_when_carrier_arrived_first():
    tracker = JobTracker()
    tracker._apply_transition("M1", TRANSITION_LP_ACTIVATE_2, {})
    tracker._apply_transition(
        "M1", TRANSITION_CTRL_JOB_START, {"CtrlJobID": "CJ-42"}
    )
    assert tracker.lookup_lp("M1", ceid=0, data={"CtrlJobID": "CJ-42"}) == "2"


def test_concurrent_carriers_use_ctrl_job_disambiguation():
    """LP1 carrier + LP2 carrier, two ControlJobs - PM events with the right
    CtrlJobID route to the correct LP regardless of which is 'active'."""
    tracker = JobTracker()
    tracker._apply_transition("M1", TRANSITION_LP_ACTIVATE_1, {})
    tracker._apply_transition(
        "M1", TRANSITION_CTRL_JOB_START, {"CtrlJobID": "CJ-LP1"}
    )
    tracker._apply_transition("M1", TRANSITION_LP_ACTIVATE_2, {})
    tracker._apply_transition(
        "M1", TRANSITION_CTRL_JOB_START,
        {"CtrlJobID": "CJ-LP2", "PortID": "2"},
    )

    # PM event tagged with the LP1 job routes to LP1 even though LP2 is active
    assert tracker.lookup_lp("M1", ceid=0, data={"CtrlJobID": "CJ-LP1"}) == "1"
    assert tracker.lookup_lp("M1", ceid=0, data={"CtrlJobID": "CJ-LP2"}) == "2"


def test_pm_event_falls_back_to_active_lp_when_ctrl_job_unknown():
    tracker = JobTracker()
    tracker._apply_transition("M1", TRANSITION_LP_ACTIVATE_1, {})
    # No CtrlJobID in the PM event -> fall back to active LP
    assert tracker.lookup_lp("M1", ceid=0, data={}) == "1"
    # CtrlJobID present but we never started it -> fall back to active LP
    assert tracker.lookup_lp("M1", ceid=0, data={"CtrlJobID": "CJ-ghost"}) == "1"


def test_carrier_departure_demotes_active_lp():
    tracker = JobTracker()
    tracker._apply_transition("M1", TRANSITION_LP_ACTIVATE_1, {})
    tracker._apply_transition("M1", TRANSITION_LP_ACTIVATE_2, {})
    assert tracker.snapshot("M1")["active_lp"] is None
    tracker._apply_transition("M1", TRANSITION_LP_DEACTIVATE_2, {})
    # LP2 departed - active falls back to LP1
    assert tracker.snapshot("M1")["active_lp"] == "1"
    tracker._apply_transition("M1", TRANSITION_LP_DEACTIVATE_1, {})
    assert tracker.snapshot("M1")["active_lp"] is None


def test_ctrl_job_end_removes_from_map():
    tracker = JobTracker()
    tracker._apply_transition("M1", TRANSITION_LP_ACTIVATE_1, {})
    tracker._apply_transition(
        "M1", TRANSITION_CTRL_JOB_START, {"CtrlJobID": "CJ-1"}
    )
    assert tracker.snapshot("M1")["ctrl_jobs"] == {"CJ-1": "1"}
    tracker._apply_transition(
        "M1", TRANSITION_CTRL_JOB_END, {"CtrlJobID": "CJ-1"}
    )
    assert tracker.snapshot("M1")["ctrl_jobs"] == {}


def test_per_machine_isolation():
    tracker = JobTracker()
    tracker._apply_transition("M1", TRANSITION_LP_ACTIVATE_1, {})
    tracker._apply_transition("M2", TRANSITION_LP_ACTIVATE_2, {})
    assert tracker.snapshot("M1")["active_lp"] == "1"
    assert tracker.snapshot("M2")["active_lp"] == "2"


def test_note_event_with_unknown_ceid_is_noop():
    tracker = JobTracker()
    profile = _profile_with_transitions()  # empty transitions dict
    tracker.note_event("M1", profile, 12345, {})
    assert tracker.snapshot("M1")["active_lp"] is None


def test_note_event_with_known_ceid_applies_transition():
    tracker = JobTracker()
    profile = MachineProfile(
        profile_id="t", vendor="t", model="t",
        ceid_state_transitions={3160001: TRANSITION_LP_ACTIVATE_1},
    )
    tracker.note_event("M1", profile, 3160001, {})
    assert tracker.lookup_lp("M1", ceid=0, data={}) == "1"


def test_davinci_pm_event_routes_to_lp_after_carrier_arrives():
    """End-to-end: DaVinci LP2 carrier arrives, then PM1/ProcessingStarted
    fires with no LP info in its payload. Mapper consults the tracker and
    stamps load_port='2' on the canonical event."""
    from eap_middleware.mapper import CanonicalMapper
    from eap_middleware.models import MachineConfig
    from eap_middleware.profiles import ProfileRegistry

    tracker = JobTracker()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = MachineConfig(
        endpoint_id="DAV_01", display_name="DAVINCI_01",
        machine_profile="davinci_200_mc4_hc1",
        host="127.0.0.1", port=5000,
    )
    mapper = CanonicalMapper(profile, tracker=tracker)

    # 1. LP2/CarrierArrived - tracker activates LP2
    mapper.from_secs_event(machine, 3170001, {"DATETIME": "20251128094659"})
    assert tracker.snapshot("DAV_01")["active_lp"] == "2"

    # 2. PM1/ProcessingStarted with no LP info in payload
    ev = mapper.from_secs_event(
        machine, 3140002,
        {"DATETIME": "20251128094700", "_v_raw": ["W1", "LOT_X", "Rcp"]},
    )
    assert ev.event_type == "process_start"
    assert ev.load_port == "2", "PM event should inherit LP from tracker"


def test_real_pm_shape_uses_wafer_evidence_with_two_active_ports():
    from eap_middleware.mapper import CanonicalMapper
    from eap_middleware.models import MachineConfig
    from eap_middleware.profiles import ProfileRegistry

    tracker = JobTracker()
    profile = ProfileRegistry().get("davinci_200_mc4_hc1")
    machine = MachineConfig(
        endpoint_id="DAV_01", display_name="DAVINCI_01",
        machine_profile="davinci_200_mc4_hc1", host="127.0.0.1", port=5000,
    )
    mapper = CanonicalMapper(profile, tracker=tracker)
    mapper.from_secs_event(machine, 3050001, {"_v_raw": [1]})
    mapper.from_secs_event(machine, 3050001, {"_v_raw": [2]})
    mapper.from_secs_event(machine, 3080001, {"_v_raw": ["W-LP1", "LP1.1"]})
    mapper.from_secs_event(machine, 3090001, {"_v_raw": ["W-LP2", "LP2.1"]})

    lp1_event = mapper.from_secs_event(
        machine, 3140002, {"_v_raw": ["W-LP1", "LOT-1", "RCP"]}
    )
    lp2_event = mapper.from_secs_event(
        machine, 3140002, {"_v_raw": ["W-LP2", "LOT-2", "RCP"]}
    )
    assert lp1_event.load_port == "1"
    assert lp2_event.load_port == "2"


def test_spts_cassette_started_then_pm_event_routes_to_active_vce():
    """SPTS: MBCStart1 activates VCE A; subsequent generic ProcessingStarted
    (CEID 855, no VCE in name) should attribute to VCE A via tracker."""
    from eap_middleware.mapper import CanonicalMapper
    from eap_middleware.models import MachineConfig
    from eap_middleware.profiles import ProfileRegistry

    tracker = JobTracker()
    profile = ProfileRegistry().get("spts_fxp_omega")
    machine = MachineConfig(
        endpoint_id="SPTS_01", display_name="SPTS_01",
        machine_profile="spts_fxp_omega",
        host="127.0.0.1", port=5000,
    )
    mapper = CanonicalMapper(profile, tracker=tracker)

    mapper.from_secs_event(machine, 330, {"DATETIME": "20251128094659"})  # MBCStart1
    ev = mapper.from_secs_event(machine, 855, {"DATETIME": "20251128094700"})
    assert ev.event_type == "process_start"
    assert ev.load_port == "1"


# ----- chamber attribution: which port did THIS chamber's wafer come from -----

def test_a_chamber_event_is_resolved_against_its_own_wafer():
    """A multi-chamber tool interleaves lots from different load ports.

    "Which port is active on this machine" has no answer while two lots run,
    so a PM event has to be resolved against the chamber it names.
    """
    tracker = JobTracker()
    tracker.note_chamber("M1", "PM1", "1")
    tracker.note_chamber("M1", "PM2", "3")

    assert tracker.lookup_lp("M1", ceid=223, data={}, chamber="PM1") == "1"
    assert tracker.lookup_lp("M1", ceid=323, data={}, chamber="PM2") == "3"


def test_an_unbound_chamber_never_borrows_another_port():
    """The regression this guards: after the LP1 cassette leaves, LP3 is the
    only active port, and a machine-wide fallback hands PM1 events to it. A
    confidently wrong load port is worse than an empty one, because nothing
    downstream can tell it was a guess."""
    tracker = JobTracker()
    tracker.note_chamber("M1", "PM2", "3")
    tracker._apply_transition("M1", TRANSITION_LP_ACTIVATE_2, {"PortID": "3"})

    assert tracker.lookup_lp("M1", ceid=223, data={}, chamber="PM1") is None
    assert tracker.lookup_lp("M1", ceid=323, data={}, chamber="PM2") == "3"


def test_a_departing_carrier_releases_the_chamber_it_owned():
    tracker = JobTracker()
    tracker.note_chamber("M1", "PM1", "1")
    tracker.note_chamber("M1", "PM2", "3")

    tracker._apply_transition("M1", TRANSITION_LP_DEACTIVATE_1, {})
    tracker._apply_transition(
        "M1", "lp_deactivate_from_payload", {"PortID": "1"}
    )

    assert tracker.lookup_lp("M1", ceid=223, data={}, chamber="PM1") is None
    assert tracker.lookup_lp("M1", ceid=323, data={}, chamber="PM2") == "3"


def test_the_placeholder_chamber_still_uses_the_machine_wide_answer():
    """Profiles that do no chamber attribution pass the mapper's "NA"
    placeholder. They must keep the previous behaviour exactly."""
    tracker = JobTracker()
    tracker._apply_transition("M1", TRANSITION_LP_ACTIVATE_1, {})

    assert tracker.lookup_lp("M1", ceid=999, data={}, chamber="NA") == "1"
    assert tracker.lookup_lp("M1", ceid=999, data={}) == "1"


def test_an_explicit_port_in_the_payload_still_wins():
    tracker = JobTracker()
    tracker.note_chamber("M1", "PM1", "1")
    resolved = tracker.lookup_lp(
        "M1", ceid=213, data={"PortID": "4"}, chamber="PM1"
    )
    assert resolved == "4"
