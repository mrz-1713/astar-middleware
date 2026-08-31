"""Verifies DaVinci's actually-subscribed event flow works end-to-end.

The audit found that LP1/CarrierArrived (CEID 3160001) is NOT in DaVinci's
stock EventSubscription.json - it has empty Valid Variables and would delete
the link if subscribed. So the v1/v2 JobTracker activation paths that relied
on that CEID would silently do nothing on a real DaVinci.

This test pins the audit-corrected behavior: MaterialReceived (3050001,
carries PortID) is the real LP activation trigger.
"""

from __future__ import annotations

from eap_middleware.job_tracker import JobTracker
from eap_middleware.mapper import CanonicalMapper
from eap_middleware.models import MachineConfig
from eap_middleware.profiles import ProfileRegistry


def _machine(tmp_path):
    return MachineConfig(
        endpoint_id="DAV_01",
        display_name="DAVINCI200_MC4_HC1_01",
        machine_profile="davinci_200_mc4_hc1",
        host="10.10.20.32", port=5000,
        local_csv_path=str(tmp_path / "local"),
        network_csv_path=str(tmp_path / "network"),
        admin_config_path=str(tmp_path / "admin"),
    )


def test_material_received_activates_load_port_from_payload(tmp_path):
    """MaterialReceived (CEID 3050001) carries PortID via DV. Tracker must
    pick it up and activate that LP - this is the real-DaVinci path."""
    tracker = JobTracker()
    machine = _machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile, tracker=tracker)

    # Real DaVinci S6F11 for MaterialReceived carries V = [PortID]
    ev = mapper.from_secs_event(
        machine, 3050001,
        {"DATETIME": "20251128094659", "_v_raw": [2]},  # PortID=2 (LP2)
    )
    assert ev.event_type == "mounted"
    assert ev.load_port == "2", "MaterialReceived's PortID DV should set load_port"
    # Tracker should now know LP2 is active
    assert tracker.snapshot(machine.endpoint_id)["active_lp"] == "2"


def test_material_received_then_pm_event_routes_to_correct_lp(tmp_path):
    """The real DaVinci sequence: MaterialReceived (PortID=1) ->
    PM1/ProcessingStarted should inherit load_port=1."""
    tracker = JobTracker()
    machine = _machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile, tracker=tracker)

    # 1. Material arrives on LP1
    mapper.from_secs_event(
        machine, 3050001, {"_v_raw": [1], "DATETIME": "20251128094659"},
    )

    # 2. PM1/ProcessingStarted (no port info in payload, V = [W, L, R])
    ev = mapper.from_secs_event(
        machine, 3140002,
        {
            "DATETIME": "20251128094700",
            "_v_raw": ["W001", "LOT_X", "Rcp_A"],
        },
    )
    assert ev.event_type == "process_start"
    assert ev.load_port == "1", "PM event should inherit LP from tracker"


def test_e90_substrate_list_lot_id_collapses_from_list(tmp_path):
    """E90 substrate events deliver SubstLotIDList as a list (e.g. ['LOT_M42']).
    The mapper must extract 'LOT_M42' as lot_id, not the str-of-list."""
    tracker = JobTracker()
    machine = _machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile, tracker=tracker)

    # Activate LP1 first so the substrate event has a port
    mapper.from_secs_event(
        machine, 3050001, {"_v_raw": [1], "DATETIME": "20251128094659"},
    )

    # NeedsProcessing2InProcess - 13 positional DVs
    ev = mapper.from_secs_event(
        machine, 3220013,
        {
            "DATETIME": "20251128094700",
            "_v_raw": [
                ["W001"],          # SubstIDStatusList
                ["LP1.Slot1"],     # SubstSubstLocIDList
                ["PM1"],           # SubstDestinationList
                ["LP1"],           # SubstSourceList
                [],                # SubstHistoryList
                ["NeedsProcessing"],  # SubstMtrlStatusList
                [],                # AcquiredIDList
                ["W001"],          # SubstIDList -> wafer_id
                ["InProcess"],     # SubstProcStateList
                ["WaitingForHost"],# SubstStateList
                ["LOT_M42"],       # SubstLotIDList -> lot_id
                ["Production"],    # SubstTypeList
                [],                # SubstUsageList
            ],
        },
    )

    assert ev.event_type == "wafer_start"
    assert ev.lot_id == "LOT_M42", "list -> first scalar must collapse"
    assert ev.wafer_id == "W001"
    assert ev.load_port == "1"  # via tracker


def test_e90_multiple_substrates_expand_without_data_loss(tmp_path):
    tracker = JobTracker()
    machine = _machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile, tracker=tracker)
    mapper.from_secs_event(machine, 3050001, {"_v_raw": [1]})
    mapper.from_secs_event(machine, 3080001, {"_v_raw": ["W1", "LP1.1"]})
    mapper.from_secs_event(machine, 3080001, {"_v_raw": ["W2", "LP1.2"]})

    events = mapper.from_secs_events(
        machine,
        3220013,
        {"_v_raw": [
            [1, 1], ["LP1.1", "LP1.2"], ["PM1", "PM1"], ["LP1", "LP1"],
            ["", ""], [1, 1], ["", ""], ["W1", "W2"], [3, 3], [1, 1],
            ["LOT-A", "LOT-A"], [0, 0], [0, 0],
        ]},
    )
    assert [(event.wafer_id, event.lot_id, event.load_port) for event in events] == [
        ("W1", "LOT-A", "1"),
        ("W2", "LOT-A", "1"),
    ]


def test_material_removed_deactivates_load_port(tmp_path):
    tracker = JobTracker()
    machine = _machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile, tracker=tracker)

    # Bring LP1 up then take it back down
    mapper.from_secs_event(machine, 3050001, {"_v_raw": [1]})
    assert tracker.snapshot(machine.endpoint_id)["active_lp"] == "1"
    mapper.from_secs_event(machine, 3050002, {"_v_raw": [1]})  # MaterialRemoved
    assert tracker.snapshot(machine.endpoint_id)["active_lp"] is None


def test_concurrent_lots_via_material_received_on_different_ports(tmp_path):
    """Two live ports are deliberately ambiguous until payload evidence."""
    tracker = JobTracker()
    machine = _machine(tmp_path)
    profile = ProfileRegistry().get(machine.machine_profile)
    mapper = CanonicalMapper(profile, tracker=tracker)

    mapper.from_secs_event(machine, 3050001, {"_v_raw": [1]})
    mapper.from_secs_event(machine, 3050001, {"_v_raw": [2]})
    snap = tracker.snapshot(machine.endpoint_id)
    assert snap["active_lp"] is None
    assert snap["lp_history"] == ["1", "2"]

    # LP2 carrier departs (CEID 3170002, name-based deactivation)
    mapper.from_secs_event(machine, 3170002, {"_v_raw": ["CARRIER_LP2"]})
    snap = tracker.snapshot(machine.endpoint_id)
    assert snap["active_lp"] == "1"
