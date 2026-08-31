"""The shipped item tables must still match the vendor documents.

Transcription drift is silent: a CEID whose name changed in a later revision
still decodes, it just decodes into the wrong canonical event. These tests diff
what we ship against the vendor source so drift fails here rather than on the
fab floor.

Regenerate the DaVinci reference file with:
    python -m scripts.gen_davinci_full_subscription
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict

import pytest

ROOT = Path(__file__).resolve().parents[1]
XLSX = ROOT / "docs" / "vendor" / "SECS-Items_MueTec DaVinci 200 MC4_HC1.xlsx"
FULL = ROOT / "output" / "davinci200_mc4_hc1" / "EventSubscription.full.json"


def _sheet_ids(sheet: str) -> Dict[int, str]:
    pd = pytest.importorskip("pandas")
    # pandas defers .xlsx reading to openpyxl, which is not in requirements.txt
    # on purpose: the offline installer runs `pip install --no-index` against
    # the bundled win_amd64 wheel set, so an extra runtime requirement with no
    # bundled wheel would fail every air-gapped install. Skip rather than fail
    # when it is absent; CI installs it alongside pytest.
    pytest.importorskip("openpyxl")
    frame = pd.ExcelFile(XLSX).parse(sheet)
    out: Dict[int, str] = {}
    for _, row in frame.iterrows():
        try:
            item_id = int(row["ID"])
        except (TypeError, ValueError):
            continue  # blank rows and section captions carry no ID
        name = str(row["Name"]).strip()
        if name and name != "nan":
            out[item_id] = name
    return out


@pytest.mark.skipif(not XLSX.is_file(), reason="vendor spreadsheet not present")
def test_davinci_reference_covers_every_documented_event():
    full = json.loads(FULL.read_text(encoding="utf-8"))
    ours = {int(event["ceid"]): event["name"] for event in full["events"]}
    documented = _sheet_ids("Events")

    assert not (set(documented) - set(ours)), (
        "CEIDs in the spreadsheet but not shipped: "
        f"{sorted(set(documented) - set(ours))[:20]}"
    )
    assert not (set(ours) - set(documented)), (
        "CEIDs shipped but absent from the spreadsheet - nothing may be "
        f"invented: {sorted(set(ours) - set(documented))[:20]}"
    )
    mismatched = {
        ceid: (documented[ceid], ours[ceid])
        for ceid in set(documented) & set(ours)
        if documented[ceid] != ours[ceid]
    }
    assert not mismatched, f"name drift from the spreadsheet: {mismatched}"


@pytest.mark.skipif(not XLSX.is_file(), reason="vendor spreadsheet not present")
def test_davinci_reference_names_every_documented_data_variable():
    full = json.loads(FULL.read_text(encoding="utf-8"))
    ours = {int(key): value for key, value in full["dvid_names"].items()}
    documented = _sheet_ids("DV")

    assert not (set(documented) - set(ours)), (
        f"unnamed data variables: {sorted(set(documented) - set(ours))[:20]}"
    )
    mismatched = {
        dvid: (documented[dvid], ours[dvid])
        for dvid in set(documented) & set(ours)
        if documented[dvid] != ours[dvid]
    }
    assert not mismatched, f"data-variable name drift: {mismatched}"


def test_davinci_reference_has_no_dangling_report_links():
    """An event pointing at a report that does not exist would define a link
    S2F35 cannot satisfy, and the tool rejects the whole message."""
    full = json.loads(FULL.read_text(encoding="utf-8"))
    defined = {int(report["rptid"]) for report in full["reports"]}
    referenced = {
        int(rptid) for event in full["events"] for rptid in event["rptids"]
    }
    assert not (referenced - defined), (
        f"events reference undefined reports: {sorted(referenced - defined)}"
    )


def test_davinci_loadport_carrier_tag_reports_use_their_own_dvid_namespace():
    full = json.loads(FULL.read_text(encoding="utf-8"))
    reports = {report["name"]: report["dvids"] for report in full["reports"]}
    assert reports["LP1/CarrierTagReadReport"] == [2110001, 2110002]
    assert reports["LP1/CarrierTagWrittenReport"] == [2110001, 2110002]
    assert reports["LP2/CarrierTagReadReport"] == [2120001, 2120002]
    assert reports["LP2/CarrierTagWrittenReport"] == [2120001, 2120002]


def test_active_davinci_subscription_stays_curated():
    """The active file is deliberately smaller than the reference: it holds only
    events the canonical mapper classifies, so no 'unknown' telemetry is
    published. Widening it to the full superset is a behaviour change, not a
    coverage fix - see docs/VENDOR_DOC_AUDIT.md."""
    active = json.loads(
        (ROOT / "output" / "davinci200_mc4_hc1" / "EventSubscription.json")
        .read_text(encoding="utf-8")
    )
    full = json.loads(FULL.read_text(encoding="utf-8"))
    assert len(active["events"]) < len(full["events"])
    assert {int(e["ceid"]) for e in active["events"]} <= {
        int(e["ceid"]) for e in full["events"]
    }, "the active subscription must be a subset of the documented superset"


def test_spts_appendix_e_formula_ambiguity_is_reproduced_not_hidden():
    """SPTS Appendix E spaces station types 100 apart but four families exceed
    99 variables, so 107 VIDs at one station are claimed by two families. A
    flat SVID table would silently pick one. The resolver takes the station's
    module type as input instead, and the collision must keep reproducing - it
    is the reason the module exists."""
    from eap_middleware.spts_module_vids import (
        ModuleLayoutError,
        resolve,
        type_offsets,
        vid_for,
    )

    assert vid_for("Process Module 1", "Etch", 100) == 32500
    assert vid_for("Process Module 1", "Deposition", 0) == 32500

    etch = resolve({"Process Module 1": "Etch"})
    deposition = resolve({"Process Module 1": "Deposition"})
    assert etch[32500] == "Statx_Etch_DSV_BackingPumpAlarm"
    assert deposition[32500] == "Statx_Deposition_MV_ProcessTime"

    # Families whose type offset the manual never states must stay unmapped:
    # a guessed offset mislabels the whole family while looking healthy.
    for family in ("DeltaAPM", "VCE", "PreHeat"):
        assert family not in type_offsets()
        with pytest.raises(ModuleLayoutError):
            resolve({"Process Module 1": family})


def test_spts_module_vids_self_check_runs():
    from eap_middleware.spts_module_vids import demo

    demo()


def test_spts_module_variable_table_matches_the_manual_counts():
    """Guards the extractor's window. Anchoring on the first 'Appendix E' hit
    lands in the table of contents (17 lines, parses to nothing); leaving the
    end unbounded swallows Appendix F and turns page numbers into offsets
    (2276 bogus rows). Both bugs happened - these counts caught them."""
    data = json.loads(
        (ROOT / "output" / "spts_fxp_omega" / "ModuleVariables.json")
        .read_text(encoding="utf-8")
    )
    families = data["families"]
    assert sum(len(v) for v in families.values()) == 880
    for family, expected in (
        ("Etch", 199), ("DeltaAPM", 134), ("Deposition", 108),
        ("Softetch", 103), ("ForceFill", 21),
    ):
        assert len(families[family]) == expected, family
    # Offsets are per-family variable indices, never page numbers.
    for family, offsets in families.items():
        assert max(int(o) for o in offsets) <= 200, family


OMEGA_PDF = (
    ROOT / "docs" / "vendor" / "Omega_SECSII_SPTS fxP 200mm SECSII Manual (Cimetrix).pdf"
)
SPTS_FULL = ROOT / "output" / "spts_fxp_omega" / "EventSubscription.full.json"


def test_spts_reference_superset_covers_table_5_and_appendix_g():
    """Table 5 documents 224 collection events; the profile's alias table
    carries 94 of them plus six generic GEM fallbacks. The reference file is
    what makes the other 130 *recognisable* without inventing a canonical
    lifecycle classification for each - see docs/VENDOR_DOC_AUDIT.md."""
    from eap_middleware.profiles import (
        ProfileRegistry,
        profile_with_subscription_file,
    )

    full = json.loads(SPTS_FULL.read_text(encoding="utf-8"))
    superset = {int(event["ceid"]) for event in full["events"]}
    assert len(superset) == 225
    assert 811 in superset

    base = ProfileRegistry().get("spts_fxp_omega")
    profile = profile_with_subscription_file(base, base.event_subscription_path)
    assert set(profile.ceid_aliases) <= superset


def test_spts_active_subscription_uses_only_documented_mapped_ceids():
    """The generic 1001-1006 file enabled none of the SPTS lifecycle CEIDs."""
    from eap_middleware.profiles import ProfileRegistry, SPTS_CEID_ALIASES

    base = ProfileRegistry().get("spts_fxp_omega")
    assert base.event_subscription_path.endswith(
        "output/spts_fxp_omega/EventSubscription.json"
    )
    active = json.loads(
        (ROOT / base.event_subscription_path).read_text(encoding="utf-8")
    )
    assert {int(event["ceid"]) for event in active["events"]} == set(
        SPTS_CEID_ALIASES
    )
    assert not ({1001, 1002, 1003, 1004, 1005, 1006} & set(SPTS_CEID_ALIASES))


def test_spts_reference_carries_documented_report_links_and_appendix_g_dvids():
    full = json.loads(SPTS_FULL.read_text(encoding="utf-8"))
    assert len(full["reports"]) == 58  # 57 in Table 5, plus Appendix G CEID 811
    linked = {event["ceid"] for event in full["events"] if event["rptids"]}
    assert len(linked) == 58
    assert {810, 811} <= linked

    appendix = {
        int(dvid) for dvid in full["dvid_names"]
        if str(dvid).startswith(("124", "134"))
    }
    assert len(appendix) == 321

    defined = {report["rptid"] for report in full["reports"]}
    referenced = {
        rptid for event in full["events"] for rptid in event["rptids"]
    }
    assert referenced == defined

    named = {int(dvid) for dvid in full["dvid_names"]}
    used = {dvid for report in full["reports"] for dvid in report["dvids"]}
    assert used - named == {6503}
    assert full["_unnamed_dvids"] == [6503]


def test_appendix_e_and_f_use_different_station_type_numbers():
    """The SPTS manual numbers station types twice, differently. Appendix E's
    offsets drive VID arithmetic (Etch = 24); Appendix F section 24.1 gives what
    the StationType status variable reports at runtime (Etch = 180-185, six
    variants). The manual never states the correspondence, so plugging a live
    reading straight into the formula computes VIDs for the wrong module."""
    from eap_middleware.spts_module_vids import (
        ModuleLayoutError,
        family_for_station_type,
        type_offsets,
    )

    data = json.loads(
        (ROOT / "output" / "spts_fxp_omega" / "ModuleVariables.json")
        .read_text(encoding="utf-8")
    )
    runtime = {int(k): v for k, v in data["station_type_values"].items()}
    offsets = type_offsets()

    # The two schemes must not be confused for one another.
    assert offsets["Etch"] == 24
    assert all(
        "Etch" in runtime[value] for value in (180, 181, 182, 183, 184, 185)
    )
    assert set(offsets.values()) != set(runtime), "two distinct numbering schemes"

    for value in (180, 181, 182, 183, 184, 185):
        assert family_for_station_type(value) == "Etch"
    assert family_for_station_type(90) == family_for_station_type(91) == "HeatNT"

    # Delta APM reports 169 but Appendix E gives it no offset; "Station Not
    # Fitted" is not a module at all. Both must raise, never resolve.
    for value in (169, 0, 255, 999):
        with pytest.raises(ModuleLayoutError):
            family_for_station_type(value)


def test_nexgen_report_vids_all_exist_in_the_manual():
    """Report ordering cannot be diffed programmatically for the MG - the manual
    documents report contents inside state-model diagrams, which pdftotext
    flattens into columns and destroys the row association. What is checkable is
    that every VID a report references is a documented variable, which catches
    the realistic failure: a typo'd VID decoding into a silently wrong slot."""
    sub = json.loads(
        (ROOT / "output" / "nexgen_mg_series" / "EventSubscription.json")
        .read_text(encoding="utf-8")
    )
    named = {int(key) for key in sub["dvid_names"]}
    used = {
        int(dvid) for report in sub["reports"] for dvid in report.get("dvids", [])
    }
    assert used <= named, f"report VIDs with no name: {sorted(used - named)}"


# The MG is a two-chamber tool and the manual documents PM1 and PM2 as exact
# mirrors: every pmN metric variable in section 8.2 exists for both chambers at
# a fixed +200 VID offset, and every pmN collection event exists for both at a
# fixed +100 CEID offset. So the two chambers' reports must carry the same
# slots, and any difference is transcription drift rather than a tool fact.
#
# This is not hypothetical. DVIDs 1210-1218 (pm2Med{1,2,3}Temp{Avr,Max,Min}
# Wafer) are the only nine rows in the whole of section 8.2 whose name column
# wraps onto the line *below* the VID, so a text-layer transcription drops
# them and nothing else. They went missing from CEID 313 while their PM1
# mirrors (1010-1018) were present on CEID 213, and every wafer processed in
# PM2 lost its medium-temperature record while PM1 kept one. The per-VID
# checks above cannot see that: the VIDs that *are* present were all valid.
_MG_PM_CEID_PAIRS = (
    # (PM1 CEID, PM2 CEID) - the status/wafer/step families at +100 ...
    tuple((c, c + 100) for c in range(200, 232))
    # ... and the HPC/BEM/LowFlow (+2) and EPD (+3) families, which break the
    # +100 stride because both chambers share one numbering block.
    + ((514, 516), (515, 517), (518, 520), (519, 521), (522, 524), (523, 525))
    + ((533, 536), (534, 537), (535, 538))
)


def test_nexgen_pm1_and_pm2_reports_are_mirrors():
    from eap_middleware.profiles.nexgen_mg import (  # noqa: PLC0415
        NEXGEN_MG_METRIC_REPORTS,
        NEXGEN_MG_REPORTS,
    )

    reports = {**NEXGEN_MG_REPORTS, **NEXGEN_MG_METRIC_REPORTS}
    mismatches = []
    for pm1_ceid, pm2_ceid in _MG_PM_CEID_PAIRS:
        if pm1_ceid not in reports or pm2_ceid not in reports:
            continue
        pm1 = tuple(_strip_pm(name) for name, _vid in reports[pm1_ceid])
        pm2 = tuple(_strip_pm(name) for name, _vid in reports[pm2_ceid])
        if pm1 != pm2:
            mismatches.append(
                f"CEID {pm1_ceid} vs {pm2_ceid}: "
                f"only on PM1 ={sorted(set(pm1) - set(pm2))}, "
                f"only on PM2 ={sorted(set(pm2) - set(pm1))}"
            )
    assert not mismatches, "PM1/PM2 reports are not mirrors:\n" + "\n".join(
        mismatches
    )


def test_nexgen_pm2_wafer_report_carries_the_medium_temperatures():
    """Pin the nine rows the manual's line-wrapping hides (DVIDs 1210-1218)."""
    sub = json.loads(
        (ROOT / "output" / "nexgen_mg_series" / "EventSubscription.json")
        .read_text(encoding="utf-8")
    )
    reports = {int(r["rptid"]): r for r in sub["reports"]}
    pm2_wafer_finished = reports[1000000000 + 313]["dvids"]
    assert set(range(1210, 1219)) <= set(pm2_wafer_finished), (
        "pm2WaferFinished (CEID 313) is missing PM2's medium-temperature "
        "variables; every wafer processed in PM2 loses its temperature record "
        f"while PM1 keeps one. Present: {sorted(pm2_wafer_finished)}"
    )


def _strip_pm(name: str) -> str:
    """Drop the chamber prefix so PM1 and PM2 slot names compare equal.

    The manual capitalises the prefix inconsistently (``pm1StepFinished`` but
    ``Pm1BemStepFinished``), so both spellings are stripped.
    """
    for prefix in ("pm1", "pm2", "Pm1", "Pm2"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


# Every other check in this file compares one shipped artefact against another
# shipped artefact, so a name mistyped consistently in both survives all of
# them. This one compares against the vendor text layer itself. Whitespace is
# stripped and case folded first because pdftotext splits kerned names
# ("pm1Bem Med2FlowMinWafer") and the manual capitalises the chamber prefix
# inconsistently ("Pm2DiwO3Flow" at SV 3721, "pm1DiwO3Flow" at SV 3521); those
# are artefacts of the document, not of the tool.
def _flattened_manual(name: str) -> str:
    return re.sub(r"\s+", "", (ROOT / "docs" / "vendor" / name).read_text(
        encoding="utf-8", errors="replace")).lower()


# Names we ship that the manual does not contain, each a decision recorded at
# its definition site. Anything else appearing here is transcription drift.
DOCUMENTED_RENAMES = {
    # profiles/nexgen_mg/variables.py: alias for SV 4306 mapResultLastMap under
    # the name CEID 145's report uses, to keep the GEM slot enumeration
    # distinguishable from DVID 2093's E87 one.
    "SlotMapGem",
    # profiles/nexgen_mg/metrics.py: manual section 8.2 prints
    # pm1BemFlow*PrevStep at both 2144-2146 and 2159-2161; the CEID column
    # (519 vs 521) and the block structure make the second block PM2.
    "pm2BemFlowMaxPrevStep",
    "pm2BemFlowAvrgPrevStep",
    "pm2BemFlowMinPrevStep",
}


@pytest.mark.parametrize(
    "extract, tables",
    [
        ("nexgen_secs_extracted.txt", ("NEXGEN_MG_SVIDS", "NEXGEN_MG_DVS")),
        ("omega_secs_extracted.txt", ("SPTS_SVIDS", "SPTS_DVS")),
    ],
)
def test_every_shipped_variable_name_appears_in_the_vendor_manual(extract, tables):
    from eap_middleware.profiles import registry

    manual = _flattened_manual(extract)
    for table_name in tables:
        table = getattr(registry, table_name)
        assert table, f"{table_name} is empty"
        absent = sorted(
            name for name in table
            if name not in DOCUMENTED_RENAMES and name.lower() not in manual
        )
        assert not absent, (
            f"{table_name} names absent from {extract}: {absent}. Either the "
            "name is mistyped, or it is a deliberate rename that belongs in "
            "DOCUMENTED_RENAMES with the reason recorded at its definition."
        )
