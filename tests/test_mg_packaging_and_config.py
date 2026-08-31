"""The MG tool must be deployable by editing configuration, not by authoring it.

Covers the deployment half of the MG work: the production template carries a
ready machine entry, the standalone Windows simulator package is coherent, and
the generated event subscription still matches the profile it was generated
from.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from eap_middleware.config import service_config_from_dict
from eap_middleware.profiles import (
    NEXGEN_MG_CEID_ALIASES,
    NEXGEN_MG_CEID_BANDS,
    NEXGEN_MG_REPORTS,
    ProfileRegistry,
)
from gateway.event_subscription import SubscriptionConfig

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "packaging" / "mg_simulator"
SUBSCRIPTION_PATH = ROOT / "output" / "nexgen_mg_series" / "EventSubscription.json"


# ----- production configuration -----

def _production_config() -> dict:
    return yaml.safe_load(
        (ROOT / "config" / "production.yaml").read_text(encoding="utf-8")
    )


def test_production_template_ships_a_ready_mg_machine():
    config = _production_config()
    machines = {m["display_name"]: m for m in config["machines"]}
    mg = machines["NEXGEN_MG_01"]

    assert mg["machine_profile"] == "nexgen_mg_series"
    assert mg["endpoint_id"] == "TOOL_04"
    # Disabled by default: enabling the tool must be a deliberate edit.
    assert mg["enabled"] is False
    # The connection guesses must be present and overridable from config alone.
    for key in ("host", "port", "secs_device_id", "hsms_mode"):
        assert key in mg, key
    # An OFF-LINE MG discards host primaries, so the ON-LINE request is on.
    assert mg["request_online"] is True
    # Spooling is unsupported on this tool; there is nothing to drain.
    assert mg["drain_spool_on_connect"] is False


def test_shipped_spool_drain_matches_what_each_manual_documents():
    """`drain_spool_on_connect` must be set per tool, from its own manual.

    On a spooling tool the only thing that empties the spool is an S6F23 from
    the host, and a tool that will not send while a backlog exists then spools
    everything after it - so a stranded backlog silences a healthy link. Both
    tools whose manuals document spooling therefore ship with the drain on:

      * SPTS fxP - Omega GEM compliance table p9 "Spooling: Yes"; §9 Spooling
        state machine; ECID 4010 `SpoolEnabled`.
      * DaVinci  - "Spooling State" and "Spool Full" on the Host Interface
        panel in Software Operation Manual §9.6.2 and Maintenance Manual
        §3.1.2.

    The MG stays off: §2.1 states "Spooling: No" and SVIDs 17-20
    (SpoolCountActual/Total/StartTime/FullTime) are all "Not supported", so an
    S6F23 would be sent to a tool that documents no spool at all.

    The *code* default stays False (`MachineConfig.drain_spool_on_connect`) so
    this stays a per-machine decision - a blanket True would reach the MG.
    """
    config = _production_config()
    machines = {m["display_name"]: m for m in config["machines"]}

    for name in ("SPTS_fxP_OMEGA_01", "DAVINCI200_MC4_HC1_01"):
        assert machines[name]["drain_spool_on_connect"] is True, name
    assert machines["NEXGEN_MG_01"]["drain_spool_on_connect"] is False

    from eap_middleware.models import MachineConfig
    assert MachineConfig.drain_spool_on_connect is False


def test_mg_machine_template_actually_loads():
    config = _production_config()
    config["linkstuffs"] = {"enabled": True, "access_token": "token"}
    config["linkstuffs_http"].update(
        {
            "enabled": True,
            "device_tokens": {"NEXGEN_MG_01": "device-token"},
        }
    )
    config["machines"] = [
        dict(m, enabled=True) for m in config["machines"]
        if m["display_name"] == "NEXGEN_MG_01"
    ]
    loaded = service_config_from_dict(config)
    machine = loaded.machines[0]
    assert machine.machine_profile == "nexgen_mg_series"
    assert machine.request_online is True


def test_linkstuffs_device_token_slot_exists_and_is_documented():
    """An absent device token drops events silently while CSVs keep writing,
    which is easy to misdiagnose as a Linkstuffs-side fault."""
    text = (ROOT / "config" / "production.yaml").read_text(encoding="utf-8")
    config = _production_config()
    assert "NEXGEN_MG_01" in config["linkstuffs_http"]["device_tokens"]
    assert "not auto-created" in text.lower() or "NOT auto-created" in text
    assert "silently" in text


# ----- generated subscription stays in step with the profile -----

def test_subscription_file_matches_the_profile_it_was_generated_from():
    data = json.loads(SUBSCRIPTION_PATH.read_text(encoding="utf-8"))
    reports = {r["rptid"]: r for r in data["reports"]}
    events = {e["ceid"]: e for e in data["events"]}
    profile = ProfileRegistry().get("nexgen_mg_series")

    assert set(events) == set(NEXGEN_MG_CEID_ALIASES)
    for ceid, slots in NEXGEN_MG_REPORTS.items():
        report = reports[1000000000 + ceid]
        # The report's VID order IS the profile's positional layout; if these
        # ever disagree, every value in that report decodes into the wrong
        # column. Regenerate with scripts/gen_mg_subscription.py.
        assert report["dvids"] == [vid for _slot, vid in slots], ceid
        assert tuple(report["_slots"]) == profile.ceid_dv_layout[ceid], ceid
        assert events[ceid]["rptids"] == [report["rptid"]], ceid

    # CEIDs the manual gives no valid data variables get no report at all.
    for ceid, event in events.items():
        if ceid not in NEXGEN_MG_REPORTS:
            assert event["rptids"] == [], ceid


def test_subscription_file_loads_with_its_bands_intact():
    config = SubscriptionConfig.from_file(SUBSCRIPTION_PATH)
    assert config.events and config.reports
    bands = {event.band for event in config.events}
    assert bands == set(NEXGEN_MG_CEID_BANDS.values())

    # Each load port owns a band. Port count is the most likely difference
    # between MG variants, so a two-port tool must lose only the bands for the
    # ports it does not have - not every port's lot lifecycle.
    by_band = {}
    for event in config.events:
        by_band.setdefault(event.band, set()).add(event.ceid)
    for port in range(1, 5):
        band = f"load_port_{port}"
        assert by_band[band] == {119 + port, 123 + port, 129 + port,
                                 133 + port, 139 + port, 149 + port}, band

    # Within a port's band, the event that OPENS the lot file and the event
    # that CLOSES it travel together - a partial subscription can never open a
    # file it has no way to close.
    for port in range(1, 5):
        band = by_band[f"load_port_{port}"]
        assert 129 + port in band and 133 + port in band

    # slot_map is deliberately a band of one: it holds the subscription's only
    # report built from status variables rather than data variables, so if that
    # assumption is wrong it costs exactly that report and nothing else.
    assert by_band["slot_map"] == {145}


# ----- standalone Windows simulator package -----

def test_mg_simulator_ships_a_standalone_windows_package():
    for name in (
        "MGSimulator.spec", "MGSimulator.iss", "build_windows.ps1",
        "entrypoint.py", "requirements-build.txt", "README_OPERATOR.md",
        "THIRD_PARTY_NOTICES.txt", "smoke_packaged_exe.py",
        "start-active.bat", "start-passive.bat",
        "start-band-refusal-demo.bat", "start-host-offline-demo.bat",
    ):
        assert (PACKAGE_DIR / name).is_file(), name

    installer = (PACKAGE_DIR / "MGSimulator.iss").read_text(encoding="utf-8")
    build = (PACKAGE_DIR / "build_windows.ps1").read_text(encoding="utf-8")
    spec = (PACKAGE_DIR / "MGSimulator.spec").read_text(encoding="utf-8")

    # Installs without admin rights, like the DaVinci package.
    assert "PrivilegesRequired=lowest" in installer
    assert r"DefaultDirName={localappdata}\Programs\MGSimulator" in installer
    assert "MG Simulator (Passive)" in installer
    assert "MG Simulator (Active)" in installer
    # Its own AppId - sharing DaVinci's would make one uninstall the other.
    davinci_iss = (
        ROOT / "packaging" / "secsgem_simulator" / "SecsGemSimulator.iss"
    ).read_text(encoding="utf-8")
    mg_app_id = installer.split("AppId=")[1].splitlines()[0]
    davinci_app_id = davinci_iss.split("AppId=")[1].splitlines()[0]
    assert mg_app_id != davinci_app_id

    assert "MGSimulator-Setup-$Version-win-x64.exe" in build
    assert "Get-FileHash -Algorithm SHA256 $InstallerPath" in build
    # The profile is imported by the simulator at runtime, so PyInstaller has
    # to be told about it.
    assert "eap_middleware.profiles" in spec
    assert "simulator.nexgen_mg_simulator" in spec


def test_windows_ci_builds_and_smoke_tests_the_packaged_executable():
    workflow = (
        ROOT / ".github" / "workflows" / "mg-simulator-windows.yml"
    ).read_text(encoding="utf-8")

    assert "build_windows.ps1" in workflow
    assert "smoke_packaged_exe.py" in workflow
    assert "MGSimulator-Setup-1.0.0-win-x64.exe" in workflow
    # The generated subscription must not be allowed to go stale in CI: a drift
    # between it and the profile silently decodes every value into the wrong
    # column.
    assert "scripts.gen_mg_subscription" in workflow
    assert "git diff --exit-code" in workflow


def test_operator_guide_documents_the_two_silent_failure_modes():
    guide = (PACKAGE_DIR / "README_OPERATOR.md").read_text(encoding="utf-8")
    assert "refuse-band" in guide
    assert "HOST OFF-LINE" in guide
    assert "request_online" in guide
    assert "EQUIPMENT OFF-LINE" in guide


def test_every_report_dvid_has_a_name():
    """`_overlay_from_subscription` keeps a CEID's layout only when every DV
    name resolves (`if names and all(names)`). A report referencing a DVID that
    `dvid_names` does not cover therefore drops that CEID's layout silently,
    and its V[] is then decoded with no names at all.

    Manual 8.2 documents 4305 `portIdLastMapped` and 4306 `mapResultLastMap`;
    both are used by `cassetteMappedReport` (CEID 145, the isolated slot_map
    band) and both were missing from `dvid_names` until the manual audit.
    """
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sub = json.loads(
        (root / "output" / "nexgen_mg_series" / "EventSubscription.json")
        .read_text(encoding="utf-8")
    )

    named = {int(key) for key in sub["dvid_names"]}
    used: set[int] = set()
    for report in sub["reports"]:
        used.update(int(dvid) for dvid in report.get("dvids", []))

    assert not (used - named), (
        f"reports reference DVIDs with no name: {sorted(used - named)}"
    )


def test_unsupported_svids_stay_absent():
    """Manual 8.2 marks AlarmsSet (9) and the four spool variables (17-20)
    "Not supported". Their absence is load-bearing, not an oversight: no
    AlarmsSet is what raises alarm-state-unknown on reconnect, and no
    SpoolCountActual is what disables the spool-backlog health check on a tool
    with no equipment-side buffer. Re-adding them would silence both."""
    from eap_middleware.profiles import NEXGEN_MG_SVIDS

    assert 9 not in set(NEXGEN_MG_SVIDS.values()), "AlarmsSet must stay absent"
    for svid in (17, 18, 19, 20):
        assert svid not in set(NEXGEN_MG_SVIDS.values()), (
            f"spool SVID {svid} is 'Not supported' in the manual"
        )
