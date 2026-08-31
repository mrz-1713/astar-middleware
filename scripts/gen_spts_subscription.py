"""Extract the SPTS fxP Omega collection events and data variables.

Source: `docs/vendor/Omega_SECSII_SPTS fxP 200mm SECSII Manual (Cimetrix).pdf`
  - Table 5 (section 9) - collection events, and the report data variables
    each one may carry ("Valid DVs for Reports")
  - Section 12.5 - GEM specific discrete data variables
  - Section 12.10 - equipment specific discrete data variables
  - Section 25 (Appendix G) - wafer statistical data: CEID 811 and the
    Etch/DeltaAPM statistical data variables

    python -m scripts.gen_spts_subscription

Writes `output/spts_fxp_omega/EventSubscription.full.json`: the reference
superset of every event the manual documents.

It writes both the 225-event reference superset and a curated active file
containing only the vendor CEIDs the canonical SPTS profile maps. The active
file replaces the old generic 1001-1006 subscription, which enabled none of the
SPTS lifecycle events. It deliberately does not subscribe the remaining
unclassified vendor CEIDs; widening beyond mapped events remains a per-tool
commissioning decision.

Section boundaries are derived from the headings, never hardcoded as line
numbers - the manual is revised (Rev B through Rev D are in its own history)
and line numbers move. Counts are asserted so a revision that adds or removes
rows fails loudly here instead of shipping a half-parsed table.

Requires poppler's `pdftotext` on PATH (macOS: `brew install poppler`).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "docs" / "vendor" / "Omega_SECSII_SPTS fxP 200mm SECSII Manual (Cimetrix).pdf"
OUTPUT = ROOT / "output" / "spts_fxp_omega" / "EventSubscription.full.json"
ACTIVE_OUTPUT = ROOT / "output" / "spts_fxp_omega" / "EventSubscription.json"

# Counts verified against the Rev D manual with the section windows printed and
# inspected. A mismatch means either the manual changed or the parser
# regressed; both must be looked at, not papered over.
#
# These numbers cost three parser bugs to establish. An unbounded Appendix E
# scan yielded 2276 rows (page numbers read as offsets); anchoring on the first
# heading match yielded 0 (the window landed in the table of contents); and a
# window of 12.5 through 12.8 yielded 45 GEM data variables by swallowing the
# equipment-constant tables of 12.6 and 12.7. The real figure is 28.
EXPECTED_EVENTS = 224
EXPECTED_GEM_DVS = 28
EXPECTED_EQUIP_DVS = 13
EXPECTED_EVENTS_WITH_REPORT_DVS = 57
EXPECTED_APPENDIX_G_DVS = 321
RPTID_BASE = 1_000_000_000


# Subscription bands by CEID range. Table 5 groups events into families by
# hundreds; each family is an independent S2F33/35/37 band so one wrong or
# unimplemented constant degrades that family's feed and leaves the rest
# reporting (the same containment the NexGen profile uses).
def _spts_band(ceid: int) -> str:
    if ceid < 100:
        return "misc"
    if ceid < 300:
        return "process_state"
    if ceid < 400:
        return "lot_wafer"
    if ceid < 500:
        return "module_recipe"
    if ceid < 600:
        return "wafer_status"
    if ceid < 700:
        return "mode_change"
    if ceid < 800:
        return "door_smif"
    if ceid < 900:
        return "cassette_statistics"
    if ceid < 1000:
        return "rf_power"
    return "generic"


# A report this wide is isolated into a band of its own. The Omega's
# RecipeStepEnd family carries 172 DVIDs - 165 of them module statistics, the
# single largest all-or-nothing S2F33 the middleware sends, and the one most
# likely to name a statistic a given module set does not implement. Left in
# its family band it would take that family's other 23 reports down with it.
# The threshold sits in the gap in the real distribution: 172 for those, 7 for
# every other report the manual documents.
LARGE_REPORT_DVIDS = 32


def _band_for(ceid: int, dvids: List[int]) -> str:
    """The subscription band one event and its report both belong to.

    Report and event must agree: `EventSubscriptionManager._bands()` groups
    both by the same string, and S2F35 may only link a CEID to an RPTID that
    the same band's S2F33 already defined.
    """
    family = _spts_band(ceid)
    if len(dvids) > LARGE_REPORT_DVIDS:
        return f"{family}_ceid{ceid}"
    return family


def _text() -> List[str]:
    if not shutil.which("pdftotext"):
        sys.exit("pdftotext not found. Install poppler (brew install poppler).")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "omega.txt"
        subprocess.run(
            ["pdftotext", "-layout", str(PDF), str(out)],
            check=True, capture_output=True,
        )
        return out.read_text(encoding="utf-8", errors="replace").splitlines()


def _find(lines: List[str], pattern: str, after: int = 0) -> int:
    """First line matching `pattern`, skipping table-of-contents entries.

    Every section heading in this manual appears twice - once in the TOC and
    once as the real heading - and the TOC always comes first. Anchoring on the
    first raw match therefore yields a one-line window that parses to nothing.
    TOC lines are the ones carrying a dotted leader to a page number, so that
    is what identifies them.
    """
    for index in range(after, len(lines)):
        line = lines[index]
        if re.search(pattern, line) and "...." not in line:
            return index
    sys.exit(f"anchor not found in the manual: {pattern!r}")


def _rows(lines: List[str], start: int, end: int) -> Dict[int, Dict[str, str]]:
    """{id: {name, description}} for `<id>  <Name>  <Description>` rows.

    Descriptions wrap onto continuation lines with no leading id; those are
    appended to the row above so a wrapped description is not lost.
    """
    out: Dict[int, Dict[str, str]] = {}
    current: int | None = None
    for line in lines[start:end]:
        match = re.match(r"^\s*(\d{1,5})\s\s+([A-Za-z]\S*)\s*(.*)$", line)
        if match:
            item_id, name, description = match.groups()
            current = int(item_id)
            out.setdefault(current, {
                "name": name,
                "description": " ".join(description.split()),
            })
        elif current is not None and line.strip() and not line.strip()[0].isdigit():
            out[current]["description"] = " ".join(
                (out[current]["description"] + " " + line).split()
            )
    return out


def _event_rows(
    lines: List[str], start: int, end: int
) -> Dict[int, Dict[str, object]]:
    """Parse Table 5 without flattening its right-hand DVID column."""
    out: Dict[int, Dict[str, object]] = {}
    current: int | None = None
    dvids_only = re.compile(r"^\s*(\d+(?:\s*,\s*\d+)*,?)\s*$")
    for line in lines[start:end]:
        match = re.match(r"^\s*(\d{1,4})\s{2,}(\S+)\s{2,}(.*)$", line)
        if match:
            ceid_text, name, remainder = match.groups()
            ceid = int(ceid_text)
            dv_match = re.search(
                r"\s{2,}(\d+(?:\s*,\s*\d+)*,?)\s*$", remainder
            )
            dv_text = dv_match.group(1) if dv_match else ""
            description = (
                remainder[: dv_match.start()].strip()
                if dv_match else remainder.strip()
            )
            out[ceid] = {
                "name": name,
                "description": " ".join(description.split()),
                "dvids": [int(value) for value in re.findall(r"\d+", dv_text)],
            }
            current = ceid
            continue

        if current is None or not line.strip():
            continue
        stripped = line.strip()
        # Physical page numbers (21-24) sit alone between wrapped table rows.
        # All wrapped report-DVID continuations in this table are four digits.
        if stripped.isdigit() and int(stripped) < 1000:
            continue
        continuation = dvids_only.match(line)
        if continuation:
            dvids = out[current]["dvids"]
            assert isinstance(dvids, list)
            dvids.extend(
                int(value) for value in re.findall(r"\d+", continuation.group(1))
            )
            continue
        if (
            stripped.startswith(("CEID ", "SPTS fxP", "Table 5:"))
        ):
            continue
        description = str(out[current]["description"])
        out[current]["description"] = " ".join(
            (description + " " + stripped).split()
        )
    return out


def _statistical_dvs(
    lines: List[str], start: int, end: int
) -> Dict[int, Dict[str, str]]:
    """Parse Appendix G's eight-digit statistical DVID/name rows."""
    out: Dict[int, Dict[str, str]] = {}
    for line in lines[start:end]:
        match = re.match(r"^\s*((?:124|134)\d{5})\s{2,}(\S+)\s*$", line)
        if match:
            dvid, name = match.groups()
            out[int(dvid)] = {"name": name, "description": ""}
    return out


def build() -> Dict[str, object]:
    lines = _text()

    ev_start = _find(lines, r"Table 5 details the Collection Events")
    ev_end = _find(lines, r"Table 5: Collection Events", ev_start + 1)
    events = _event_rows(lines, ev_start, ev_end)

    gem_start = _find(lines, r"^\s*12\.5\s+GEM Specific Discrete Data Variables")
    gem_end = _find(lines, r"^\s*12\.6\s+\S", gem_start + 1)
    gem_dvs = _rows(lines, gem_start, gem_end)

    eq_start = _find(
        lines, r"^\s*12\.10\s+Equipment Specific Discrete Data Variables"
    )
    eq_end = _find(lines, r"^\s*13\b", eq_start + 1)
    equip_dvs = _rows(lines, eq_start, eq_end)

    delta_start = _find(
        lines, r"^\s*25\.2\.1\s+DeltaAPM Statistical Data DVIDs"
    )
    etch_start = _find(lines, r"^\s*25\.3\s+Etch PM Generated Summary Data")
    delta_dvs = _statistical_dvs(lines, delta_start, etch_start)
    etch_dvs = _statistical_dvs(lines, etch_start, len(lines))

    for label, got, want in (
        ("collection events", len(events), EXPECTED_EVENTS),
        ("12.5 GEM data variables", len(gem_dvs), EXPECTED_GEM_DVS),
        ("12.10 equipment data variables", len(equip_dvs), EXPECTED_EQUIP_DVS),
        (
            "Table 5 events with report DVs",
            sum(bool(item["dvids"]) for item in events.values()),
            EXPECTED_EVENTS_WITH_REPORT_DVS,
        ),
        (
            "Appendix G statistical data variables",
            len(delta_dvs) + len(etch_dvs),
            EXPECTED_APPENDIX_G_DVS,
        ),
    ):
        if got != want:
            sys.exit(
                f"{label}: parsed {got}, expected {want}. Either the manual "
                "revision changed or the section anchors moved - check before "
                "updating the expected count."
            )

    # 13 IDs (5100-5118, 6102) appear in both sections. In the Rev D manual
    # they carry identical names, so merging is lossless - but a dict merge
    # would silently keep one side if a later revision diverged, which is the
    # same class of failure as the Appendix E VID collision. Fail instead.
    conflicts = {
        dvid: (gem_dvs[dvid]["name"], equip_dvs[dvid]["name"])
        for dvid in set(gem_dvs) & set(equip_dvs)
        if gem_dvs[dvid]["name"] != equip_dvs[dvid]["name"]
    }
    if conflicts:
        sys.exit(
            "sections 12.5 and 12.10 disagree on data-variable names: "
            f"{conflicts}. Resolve against the manual before generating."
        )
    all_dvs = {**gem_dvs, **equip_dvs, **delta_dvs, **etch_dvs}
    dvid_names = {
        str(dvid): item["name"]
        for dvid, item in sorted(all_dvs.items())
    }

    # Appendix G supplements Table 5 rather than revising it. CEID 811 and its
    # Etch report exist only there; the DeltaAPM statistical DVIDs are valid on
    # the seven RecipeStepEnd events listed in section 25.2.
    appendix_common = [5101, 5102, 5110, 5111, 5113, 5116, 6102]
    events[811] = {
        "name": "WaferStatisticalStepDataAvailable",
        "description": (
            "Etch module statistical data for a wafer process step is available"
        ),
        "dvids": appendix_common + sorted(etch_dvs),
    }
    for ceid in (482, 483, 484, 485, 486, 487, 858):
        dvids = events[ceid]["dvids"]
        assert isinstance(dvids, list)
        dvids.extend(dvid for dvid in sorted(delta_dvs) if dvid not in dvids)

    reports = [
        {
            "rptid": RPTID_BASE + ceid,
            "name": f"{item['name']}Report",
            "description": item["description"],
            "dvids": item["dvids"],
            "band": _band_for(ceid, item["dvids"]),
        }
        for ceid, item in sorted(events.items())
        if item["dvids"]
    ]
    return {
        "description": (
            "SPTS fxP Omega FULL event subscription: every collection event "
            "Table 5 documents. Reference only - the active subscription for "
            "this profile is generated separately from the mapped subset so "
            "unclassified vendor events are not enabled by accident."
        ),
        "_source": PDF.name + " Table 5, sections 12.5, 12.10 and Appendix G",
        "_generated_by": "scripts/gen_spts_subscription.py",
        "_manual_note": (
            "Table 5 assigns wafer statistical data to CEID 810; Appendix G "
            "section 25.3 says the newer Etch step-statistics event is CEID "
            "811. Both are retained because the Rev D manual is internally "
            "contradictory and only the vendor can resolve which a specific "
            "tool revision emits. Table 5 also links DVID 6503 to CEID 810 "
            "without defining or naming DVID 6503 anywhere else in the manual."
        ),
        "_unnamed_dvids": [6503],
        "reports": reports,
        "events": [
            {
                "ceid": ceid,
                "name": item["name"],
                "description": item["description"]
                or f"SPTS fxP collection event {ceid}",
                "rptids": [RPTID_BASE + ceid] if item["dvids"] else [],
                "band": _band_for(ceid, item["dvids"]),
                "enabled": True,
            }
            for ceid, item in sorted(events.items())
        ],
        "dvid_names": dvid_names,
    }


def main() -> None:
    data = build()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    # The profile used to point at config/EventSubscription.json (generic CEIDs
    # 1001-1006), none of which is an SPTS lifecycle event. Keep the active file
    # curated to the CEIDs the canonical profile understands, but derive every
    # report and DVID from the full vendor extraction above.
    from eap_middleware.profiles import SPTS_CEID_ALIASES

    active_ceids = set(SPTS_CEID_ALIASES)
    active_events = [
        event for event in data["events"]  # type: ignore[index]
        if int(event["ceid"]) in active_ceids
    ]
    active_rptids = {
        int(rptid) for event in active_events for rptid in event["rptids"]
    }
    active = {
        "description": (
            "SPTS fxP Omega active subscription: vendor-documented events the "
            "canonical profile maps. Generated from EventSubscription.full.json."
        ),
        "_source": data["_source"],
        "_generated_by": "scripts/gen_spts_subscription.py",
        "reports": [
            report for report in data["reports"]  # type: ignore[index]
            if int(report["rptid"]) in active_rptids
        ],
        "events": active_events,
        "dvid_names": data["dvid_names"],
    }
    ACTIVE_OUTPUT.write_text(
        json.dumps(active, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Wrote {OUTPUT}: {len(data['events'])} events, "  # type: ignore[arg-type]
        f"{len(data['reports'])} reports, "  # type: ignore[arg-type]
        f"{len(data['dvid_names'])} named data variables"  # type: ignore[arg-type]
    )
    print(
        f"Wrote {ACTIVE_OUTPUT}: {len(active_events)} events, "
        f"{len(active['reports'])} reports"
    )


if __name__ == "__main__":
    main()
