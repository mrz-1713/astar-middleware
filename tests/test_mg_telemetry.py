"""What the MG publishes to Linkstuffs, at the mapper -> telemetry seam.

The per-lot CSV contract has nine columns; everything else the MG reports -
wafer counts, cycle times, the per-medium chemistry block, recipe selection,
slot maps, carrier and job IDs - only ever reaches a dashboard through the
telemetry payload. These pin that it survives the positional decode and comes
out under readable names, which the CSV-only tests cannot show.
"""

from __future__ import annotations

from typing import Dict, List

from eap_middleware.mapper import CanonicalMapper
from eap_middleware.models import (
    LegacyApiConfig,
    LinkstuffsConfig,
    LinkstuffsHttpConfig,
    MachineConfig,
    MiddlewarePaths,
    ServiceConfig,
)
from eap_middleware.profiles import NEXGEN_MG_REPORTS, ProfileRegistry
from eap_middleware.service import EapMiddlewareService

PROFILE_ID = "nexgen_mg_series"


def _machine(tmp_path, display="NEXGEN_MG_01"):
    return MachineConfig(
        endpoint_id="TOOL_04",
        display_name=display,
        machine_profile=PROFILE_ID,
        host="127.0.0.1",
        port=5000,
        local_csv_path=str(tmp_path / "local"),
        admin_config_path=str(tmp_path / "admin"),
    )


def _mapper():
    return CanonicalMapper(ProfileRegistry().get(PROFILE_ID))


def _values(machine, ceid, raw_v):
    """Telemetry values for one S6F11 delivered as a raw positional V[]."""
    event = _mapper().from_secs_event(machine, ceid, {"_v_raw": list(raw_v)})
    return event, event.telemetry_values()


# ----- per-lot summary (the largest report in the subscription) -----

def _lot_summary_v(job="JOB_1", lot="LOT_A", recipe="RCP", port="1", carrier="CAR_1"):
    chemistry = [round(10.0 + index, 1)
                 for index in range(len(NEXGEN_MG_REPORTS[5]) - 5)]
    return [job, lot, recipe, port, carrier] + chemistry


def test_lot_completion_publishes_the_whole_chemistry_block(tmp_path):
    machine = _machine(tmp_path)
    event, values = _values(machine, 5, _lot_summary_v())

    assert event.event_type == "process_end"
    assert event.lot_id == "LOT_A"
    assert event.recipe == "RCP"
    assert event.load_port == "1"

    # Every chemistry slot arrives under its own readable name, in order.
    layout = NEXGEN_MG_REPORTS[5]
    for index, (slot, _vid) in enumerate(layout[5:]):
        assert values[f"raw_{slot}"] == round(10.0 + index, 1), slot

    # Spot-check the min/max/average triples the dashboards actually plot.
    assert values["raw_N2ChuckFlowMinLot"] == 10.0
    assert values["raw_N2ChuckFlowMaxLot"] == 11.0
    assert values["raw_N2ChuckFlowAvrLot"] == 12.0
    assert "raw_Med1TempMinLot" in values
    assert "raw_ChuckSpeedAvrLot" in values


def test_ready_to_unload_publishes_wafer_count_and_cycle_times(tmp_path):
    machine = _machine(tmp_path)
    event, values = _values(machine, 124, [25, 1800, 1500])

    assert event.event_type == "lot_end"
    assert event.load_port == "1", "port comes from the CEID, not the payload"
    assert values["raw_WafersFinished"] == 25
    assert values["raw_TotalLotTime"] == 1800
    assert values["raw_TotalProcessTime"] == 1500


def test_processing_started_publishes_the_lot_plan(tmp_path):
    machine = _machine(tmp_path)
    event, values = _values(machine, 151, [13, 2, "2026-08-11", "08:00:05"])

    assert event.event_type == "lot_start"
    assert event.load_port == "2"
    assert values["raw_WafersToProcess"] == 13
    assert values["raw_OutputPort"] == 2
    assert values["raw_StartProcessDate"] == "2026-08-11"
    assert values["raw_StartProcessTime"] == "08:00:05"


# ----- recipe selection -----

def test_recipe_selection_carries_the_name_and_the_port(tmp_path):
    machine = _machine(tmp_path)
    event, values = _values(machine, 13, ["MG_CLEAN_02", "3"])

    assert event.event_type == "recipe_selected"
    assert event.recipe == "MG_CLEAN_02"
    assert event.load_port == "3", "which port the recipe was selected for"
    assert event.raw_event_name == "processRecipeSelected"


# ----- slot map -----

def test_slot_map_publishes_cross_and_double_slotted_positions(tmp_path):
    machine = _machine(tmp_path)
    # 1=FULLSLOT 2=EMPTYSLOT 3=CROSSSLOTTED 4=DOUBLESLOTTED
    event, values = _values(machine, 145, [2, [1, 3, 2, 4, 1]])

    assert event.event_type == "mapped"
    assert event.load_port == "2"
    # "SlotMapGem", not "SlotMap": the manual defines two incompatible slot
    # encodings and 3 means CROSSSLOTTED in this one (SVID 4306) but
    # CORRECTLY OCCUPIED in the E87 carrier attribute on DVID 2093. The name
    # is what tells a downstream consumer which is which.
    assert values["raw_SlotMapGem"] == "[1, 3, 2, 4, 1]", "serialized for transport"
    assert "raw_SlotMap" not in values, "must not collide with the E87 encoding"


# ----- alarms -----

def test_alarm_forwards_the_tools_own_text_and_severity_code(tmp_path):
    """No transcribed alarm table: identifier, severity and text all come off
    the wire, so they cannot disagree with what the tool actually said."""
    machine = _machine(tmp_path)
    event = _mapper().alarm_event(machine, {
        "alid": 4711,
        "alcd": 6,                     # severity, ALCD with bit 7 masked off
        "altx": "PM1 chuck N2 flow below limit",
        "is_set": True,
    })
    values = event.telemetry_values()

    assert event.event_type == "alarm"
    assert event.ceid == 4711
    assert values["secs_raw_event"] == "PM1 chuck N2 flow below limit"
    assert values["raw_alcd"] == 6
    assert values["raw_is_set"] is True


# ----- health: the MG is the first profile with no spool variable -----

def _service(tmp_path) -> EapMiddlewareService:
    config = ServiceConfig(
        machines=[_machine(tmp_path)],
        linkstuffs=LinkstuffsConfig(enabled=False),
        linkstuffs_http=LinkstuffsHttpConfig(enabled=False),
        legacy_api=LegacyApiConfig(enabled=False),
        paths=MiddlewarePaths(
            install_dir=str(tmp_path / "install"),
            outbox_db=str(tmp_path / "o.sqlite3"),
            legacy_api_outbox_db=str(tmp_path / "l.sqlite3"),
            http_outbox_db=str(tmp_path / "h.sqlite3"),
        ),
        event_liveness_grace_sec=0.0,
    )
    return EapMiddlewareService(config)


class _FakeHost:
    def __init__(self):
        self.last_event_time = None   # no S6F11 ever delivered
        self.is_connected = True


class _FakeSession:
    def __init__(self, values: Dict[int, object]):
        self.host = _FakeHost()
        self.asked: List[List[int]] = []
        self._values = values

    def request_svids(self, svids):
        self.asked.append(list(svids))
        return {s: self._values[s] for s in svids if s in self._values}


def test_acked_but_silent_subscription_still_alarms_without_a_spool_variable(tmp_path):
    """The MG documents spooling as unsupported, so health_spool_count_svid is
    None. The liveness watchdog must neither poll it nor trip over it."""
    service = _service(tmp_path)
    machine = service.config.machines[0]
    # LastEventID (16) advances while nothing is delivered -> acked but silent.
    session = _FakeSession({16: 100, 12: [1, 2, 3]})
    published = []
    service._publish_health = lambda m, state, details="": published.append(state)

    service._on_connect(machine)
    service._check_event_liveness(machine, session)   # establishes the baseline
    session._values[16] = 105
    service._check_event_liveness(machine, session)   # counter advanced

    assert session.asked, "the watchdog never polled"
    for asked in session.asked:
        assert None not in asked
        assert len(asked) == 2, f"should poll LastEventID + EventsEnabled only: {asked}"
    assert "no_event_reports" in published, published


def test_replay_sweep_covers_every_documented_ceid():
    """The lot script fires 31 of 243 CEIDs, so gem300 and metrology_aux
    reports never reach the decoder. The sweep is what closes that gap, and it
    must stay driven by the subscription file rather than a hand-kept list."""
    from eap_middleware.profiles import (
        ProfileRegistry,
        profile_with_subscription_file,
    )
    from simulator.event_replay import replay, replay_plan

    base = ProfileRegistry().get("nexgen_mg_series")
    profile = profile_with_subscription_file(base, base.event_subscription_path)

    plan = replay_plan(profile)
    assert len(plan) == len(profile.ceid_aliases) == 243

    from eap_middleware.profiles import MG_GEM300_BANDS

    bands = {ceid: profile_band(ceid) for ceid, _ in plan}
    # Every GEM300 sub-band by name, not the family as a whole: the sweep is
    # what proves each one has CEIDs behind it, so a split that lands a band
    # empty - or a range boundary that moves - fails here.
    expected = ("metrology_aux", "core_gem", "process_module_1") + MG_GEM300_BANDS
    for band in expected:
        assert any(b == band for b in bands.values()), f"{band} missing from sweep"

    seen: List[int] = []
    sent = replay(profile, lambda ceid, values: (seen.append(ceid), True)[1])
    assert sent == 243 and seen == sorted(profile.ceid_aliases)


def profile_band(ceid: int) -> str:
    from eap_middleware.profiles import NEXGEN_MG_CEID_BANDS

    return NEXGEN_MG_CEID_BANDS.get(ceid, "")


def test_event_replay_self_check_runs():
    """Keeps simulator/event_replay.py's demo() honest under CI."""
    from simulator.event_replay import demo

    demo()
