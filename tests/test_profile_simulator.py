"""The one simulator has to speak every profile's own CEIDs.

Before ProfileSimulator existed, the GUI simulated an SPTS or PTIQ machine with
a DaVinci simulator: every CEID it sent was unknown to the configured profile,
so the whole run produced `unknown` events and empty CSVs. These tests pin the
fix - each profile's simulated lot must map back to real canonical events.
"""

from __future__ import annotations

import json
import socket
import struct
import time
from pathlib import Path

import pytest

pytest.importorskip("secsgem")

import secsgem.hsms

from eap_middleware.mapper import CanonicalMapper
from eap_middleware.models import MachineConfig
from eap_middleware.profiles import (
    ProfileRegistry,
    profile_with_subscription_file,
)
from eap_middleware.secs_runtime import SecsMachineSession
from simulator.profile_simulator import (
    GENERAL_CEIDS,
    LOT_END_FLOW,
    LOT_FLOW,
    WAFER_FLOW,
    ProfileSimulator,
    resolve_flow_ceids,
)

ALL_STEPS = LOT_FLOW + WAFER_FLOW + LOT_END_FLOW


def test_inline_dvid_value_overrides_generated_payload_value():
    simulator = object.__new__(ProfileSimulator)
    simulator.dvid_overrides = {"lotid": "INLINE_LOT"}
    simulator.dvid_override_types = {"lotid": "A"}

    value = simulator._dv_value("LotID", {})

    assert value.get() == "INLINE_LOT"
    assert type(value).__name__ == "String"


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.mark.parametrize(
    "profile_id",
    ["spts_fxp_omega", "davinci_200_mc4_hc1", "nexgen_mg_series"],
)
def test_documented_profiles_resolve_their_own_lifecycle_ceids(profile_id):
    """Every step of the lot comes from the profile, not from a placeholder."""
    profile = ProfileRegistry().get(profile_id)
    ceids = resolve_flow_ceids(profile)
    for step in ALL_STEPS:
        if step == "clamped" and profile_id == "nexgen_mg_series":
            continue  # the MG manual documents no clamp event
        assert step in ceids, f"{profile_id} has no CEID for {step}"
        assert profile.resolve_event(ceid=ceids[step]).event_type == step


def test_ptiq_takes_its_ceids_from_the_subscription_file(tmp_path):
    """PTIQ ships no CEID numbers - they come per installation."""
    subscription = tmp_path / "EventSubscription.json"
    subscription.write_text(
        json.dumps(
            {
                "reports": [
                    {"rptid": 1, "name": "LotReport", "dvids": [10, 11]},
                ],
                "events": [
                    {"ceid": 4001, "name": "SCH1.LotStarted", "rptids": [1]},
                    {"ceid": 4002, "name": "SCH1.LotComplete", "rptids": [1]},
                ],
                "dvid_names": {"10": "LotID", "11": "PortID"},
            }
        ),
        encoding="utf-8",
    )
    base = ProfileRegistry().get("ptiq_secsgem")
    assert base.resolve_event(ceid=4001).event_type == "unknown"

    profile = profile_with_subscription_file(base, str(subscription))
    assert profile.resolve_event(ceid=4001).event_type == "lot_start"
    assert profile.resolve_event(ceid=4002).event_type == "lot_end"
    # The report's DVID list becomes the positional V[] layout.
    assert profile.ceid_dv_layout[4001] == ("LotID", "PortID")
    # ...and the base profile is untouched.
    assert base.resolve_event(ceid=4001).event_type == "unknown"


def test_subscription_file_never_overrides_a_documented_ceid(tmp_path):
    subscription = tmp_path / "EventSubscription.json"
    subscription.write_text(
        json.dumps(
            {
                "reports": [],
                # 3050001 is MaterialReceived in the DaVinci workbook.
                "events": [{"ceid": 3050001, "name": "LotStarted", "rptids": []}],
                "dvid_names": {},
            }
        ),
        encoding="utf-8",
    )
    base = ProfileRegistry().get("davinci_200_mc4_hc1")
    profile = profile_with_subscription_file(base, str(subscription))
    assert profile.resolve_event(ceid=3050001).event_type == "mounted"


def test_missing_or_broken_subscription_file_falls_back_to_the_profile(tmp_path):
    base = ProfileRegistry().get("davinci_200_mc4_hc1")
    assert profile_with_subscription_file(base, None) is base
    assert profile_with_subscription_file(base, str(tmp_path / "nope.json")) is base
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert profile_with_subscription_file(base, str(broken)) is base


def test_malformed_subscription_file_does_not_take_the_machine_down(tmp_path):
    """Valid JSON of the wrong shape must not raise out of the overlay.

    A hand-edited file with names where DVID numbers belong parses fine and
    then blows up on int(). It reaches this code at service start, once per
    machine, so an exception here stops the whole middleware.
    """
    base = ProfileRegistry().get("ptiq_secsgem")
    for payload in (
        {"dvid_names": {"LotID": "x"}, "reports": [], "events": []},
        {"dvid_names": {}, "reports": [{"rptid": "one", "dvids": [1]}]},
        {"reports": [{"rptid": 1, "dvids": ["two"]}], "events": []},
        {"events": [{"ceid": 1, "name": "LOT_START", "rptids": ["nope"]}]},
        {"dvid_names": [], "reports": None},
    ):
        broken = tmp_path / "broken.json"
        broken.write_text(json.dumps(payload), encoding="utf-8")
        assert profile_with_subscription_file(base, str(broken)) is base


def test_relative_subscription_path_resolves_from_any_directory(monkeypatch):
    """Profile defaults are repo-relative; a packaged app runs from elsewhere."""
    monkeypatch.chdir(tmp_path_root := Path(__file__).resolve().parent.parent)
    from_repo = profile_with_subscription_file(
        ProfileRegistry().get("ptiq_secsgem"), "config/EventSubscription.json"
    )
    monkeypatch.chdir(tmp_path_root.anchor)  # "/" - a Start Menu shortcut's cwd
    from_elsewhere = profile_with_subscription_file(
        ProfileRegistry().get("ptiq_secsgem"), "config/EventSubscription.json"
    )
    assert from_elsewhere.ceid_aliases == from_repo.ceid_aliases
    assert from_elsewhere.resolve_event(ceid=1002).event_type == "lot_start"


def test_general_ceids_map_through_the_shipped_subscription_file():
    """Out of the box: no numbers configured anywhere, and it still maps.

    A profile with no published CEID table falls back to the general GEM
    events, and the shipped config/EventSubscription.json names those same
    numbers - so a host and a simulator that were both left at their defaults
    understand each other.
    """
    base = ProfileRegistry().get("ptiq_secsgem")
    profile = profile_with_subscription_file(
        base, "config/EventSubscription.json"
    )
    for step, ceid in GENERAL_CEIDS.items():
        if step in ("mounted", "unmounted"):
            # The general pod events cover load+mount in one CEID; the shipped
            # file names them for the load/unload half.
            continue
        if step in ("process_start", "process_end"):
            continue  # not in the shipped file's six-event set
        assert profile.resolve_event(ceid=ceid).event_type == step, (
            f"general CEID {ceid} for {step} does not map"
        )


def test_ceid_overrides_win_over_the_profile():
    profile = ProfileRegistry().get("spts_fxp_omega")
    ceids = resolve_flow_ceids(profile, {"lot_start": 7777})
    assert ceids["lot_start"] == 7777
    assert ceids["lot_end"] == resolve_flow_ceids(profile)["lot_end"]


def test_zero_config_ptiq_runs_on_the_general_ceids(tmp_path):
    """Nothing configured anywhere: pick the profile, press start, it works."""
    port = _free_port()
    simulator = ProfileSimulator(
        settings=secsgem.hsms.HsmsSettings(
            address="127.0.0.1",
            port=port,
            connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
            session_id=0,
        ),
        profile_id="ptiq_secsgem",
        tool_id="SIM_PTIQ",
        wafer_count=1,
        step_interval_sec=0.02,
        fire_alarm=False,
        loop_lots=False,
    )
    assert simulator.ceids["lot_start"] == GENERAL_CEIDS["lot_start"]

    machine = MachineConfig(
        endpoint_id="TOOL_PTIQ",
        display_name="PTIQ_TOOL",
        machine_profile="ptiq_secsgem",
        host="127.0.0.1",
        port=port,
        local_csv_path=str(tmp_path / "local"),
        admin_config_path=str(tmp_path / "admin"),
        hsms_mode="active",
    )
    # Exactly what the service builds for a machine with no overrides.
    profile = profile_with_subscription_file(
        ProfileRegistry().get("ptiq_secsgem"), "config/EventSubscription.json"
    )
    mapper = CanonicalMapper(profile)
    seen: list[str] = []

    session = SecsMachineSession(
        machine=machine,
        event_callback=lambda m, ceid, data: seen.append(
            mapper.from_secs_event(m, ceid, data).event_type
        ),
        alarm_callback=lambda *a, **k: None,
        connect_callback=lambda *a, **k: None,
        disconnect_callback=lambda *a, **k: None,
    )
    try:
        simulator.enable()
        simulator.start_events()
        time.sleep(0.3)
        session.start()
        deadline = time.time() + 30.0
        while time.time() < deadline and "unloaded" not in seen:
            time.sleep(0.1)
    finally:
        for stop in (session.stop, simulator.disable):
            try:
                stop()
            except Exception:
                pass

    assert "unknown" not in seen, f"zero-config PTIQ produced: {seen}"
    for step in ("lot_start", "wafer_start", "wafer_end", "lot_end", "unloaded"):
        assert step in seen, f"zero-config PTIQ never reported {step}: {seen}"


@pytest.mark.parametrize(
    "profile_id",
    ["spts_fxp_omega", "ptiq_secsgem", "nexgen_mg_series", "davinci_200_mc4_hc1"],
)
def test_simulated_lot_maps_to_canonical_events_over_hsms(profile_id, tmp_path):
    """One simulator, every profile, end to end over a real HSMS socket."""
    port = _free_port()
    overrides = (
        {step: 9100 + index for index, step in enumerate(ALL_STEPS)}
        if profile_id == "ptiq_secsgem"
        else {}
    )
    # A PTIQ tool's numbers reach the middleware through its own subscription
    # file; write the same numbers the simulator was given.
    subscription = tmp_path / "EventSubscription.json"
    subscription.write_text(
        json.dumps(
            {
                "reports": [{"rptid": 1, "name": "R", "dvids": [1, 2, 3]}],
                "events": [
                    {"ceid": ceid, "name": name, "rptids": [1]}
                    for name, ceid in zip(
                        ("MaterialReceived", "CarrierArrived", "TransferIn",
                         "SCH1.LotStarted", "WaferStarted", "ProcessingStarted",
                         "ProcessingCompleted", "WaferComplete",
                         "SCH1.LotComplete", "CarrierRemoved", "MaterialRemoved"),
                        (overrides[step] for step in ALL_STEPS),
                    )
                ] if overrides else [],
                "dvid_names": {"1": "LotID", "2": "WaferID", "3": "RecipeName"},
            }
        ),
        encoding="utf-8",
    )

    simulator = ProfileSimulator(
        settings=secsgem.hsms.HsmsSettings(
            address="127.0.0.1",
            port=port,
            connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
            session_id=0,
        ),
        profile_id=profile_id,
        tool_id=f"SIM_{profile_id}",
        wafer_count=1,
        step_interval_sec=0.02,
        fire_alarm=False,
        loop_lots=False,
        ceid_overrides=overrides,
    )

    machine = MachineConfig(
        endpoint_id="TOOL_SIM",
        display_name="SIM_TOOL",
        machine_profile=profile_id,
        host="127.0.0.1",
        port=port,
        local_csv_path=str(tmp_path / "local"),
        admin_config_path=str(tmp_path / "admin"),
        hsms_mode="active",
    )
    profile = profile_with_subscription_file(
        ProfileRegistry().get(profile_id),
        str(subscription) if overrides else None,
    )
    mapper = CanonicalMapper(profile)
    seen: list[str] = []

    def on_event(_machine, ceid, data):
        seen.append(mapper.from_secs_event(_machine, ceid, data).event_type)

    session = SecsMachineSession(
        machine=machine,
        event_callback=on_event,
        alarm_callback=lambda *a, **k: None,
        connect_callback=lambda *a, **k: None,
        disconnect_callback=lambda *a, **k: None,
    )

    try:
        simulator.enable()
        simulator.start_events()
        time.sleep(0.3)
        session.start()
        deadline = time.time() + 30.0
        while time.time() < deadline and "unmounted" not in seen:
            time.sleep(0.1)
    finally:
        try:
            session.stop()
        except Exception:
            pass
        try:
            simulator.disable()
        except Exception:
            pass

    assert "unknown" not in seen, f"{profile_id} produced unmapped events: {seen}"
    # process_start/process_end are not universal - the MG maps its per-port
    # "processing started" CEIDs to the lot boundary instead.
    for step in ("lot_start", "wafer_start", "wafer_end", "lot_end", "unloaded"):
        assert step in seen, f"{profile_id} never reported {step}; got {seen}"


@pytest.mark.parametrize("command", ["START", "STOP", "PAUSE", "start", "pause"])
def test_remote_commands_do_not_raise(command):
    """S2F41 used to hit ProcessState members that did not exist.

    _execute_command lives on the shared equipment base and is overridden by
    nobody, so START/PAUSE raised AttributeError for every simulator - no
    S2F42 came back and the host sat out its T3 timeout.
    """
    simulator = ProfileSimulator(
        settings=secsgem.hsms.HsmsSettings(
            address="127.0.0.1",
            port=_free_port(),
            connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
            session_id=0,
        ),
        profile_id="spts_fxp_omega",
        tool_id="SIM_CMD",
    )
    simulator._execute_command(command)  # must not raise


def test_replay_sweep_covers_every_profile_completely():
    """The canonical lot flow reaches 4-23% of what each vendor documents
    (10/243 MG, 11/100 SPTS, 11/48 DaVinci), so reports outside the lifecycle
    path never reach the middleware's decoder. The sweep must close that for
    every profile from profile data alone - no per-vendor code."""
    from eap_middleware.profiles import (
        ProfileRegistry,
        profile_with_subscription_file,
    )
    from simulator.event_replay import replay_plan
    from simulator.profile_simulator import resolve_flow_ceids

    registry = ProfileRegistry()
    profile_ids = registry.list_profile_ids()
    assert profile_ids, "registry must expose at least one profile"

    for profile_id in profile_ids:
        base = registry.get(profile_id)
        profile = profile_with_subscription_file(
            base, base.event_subscription_path
        )
        documented = sorted(profile.ceid_aliases)
        plan = replay_plan(profile)

        assert [ceid for ceid, _ in plan] == documented, (
            f"{profile_id}: sweep must emit every documented CEID in order"
        )

        # The sweep is only worth having where it beats the lot flow.
        lot_only = set(resolve_flow_ceids(profile, None).values())
        assert len(plan) >= len(lot_only), profile_id


def test_replay_sweep_uses_the_lot_context_builder():
    """Values must come from the simulator's own _values_for, which knows the
    live lot ID, recipe and carrier - not from event_replay's name-based
    fallback. A sweep sending placeholder identifiers would pass a decode test
    while hiding that the real DV names never got populated."""
    from eap_middleware.profiles import (
        ProfileRegistry,
        profile_with_subscription_file,
    )
    from simulator.event_replay import replay_plan

    base = ProfileRegistry().get("davinci_200_mc4_hc1")
    profile = profile_with_subscription_file(base, base.event_subscription_path)

    sentinel = ["FROM_LOT_CONTEXT"]
    plan = replay_plan(profile, values_for=lambda ceid: sentinel)
    assert plan, "profile documents no CEIDs"
    assert all(values == ("FROM_LOT_CONTEXT",) for _ceid, values in plan)
