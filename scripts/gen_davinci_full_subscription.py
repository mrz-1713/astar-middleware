"""Regenerate the DaVinci FULL event subscription from the vendor spreadsheet.

`docs/vendor/SECS-Items_MueTec DaVinci 200 MC4_HC1.xlsx` is the vendor's own item
list and the only source of truth for DaVinci CEIDs, SVIDs and DVs. This script
makes the transcription reproducible: run it, diff the output, and any drift
between the spreadsheet and what we ship shows up as a diff instead of as a
decode failure on the fab floor.

    python -m scripts.gen_davinci_full_subscription

What it does NOT do: change which events or reports `EventSubscription.json`
contains. That file is deliberately curated to the events the canonical mapper
categorizes, so that no `unknown` telemetry is published. This script owns its
membership only in the reference superset.

It does maintain one field in the curated file: `band`. Bands are derived from
the CEID, never hand-written and never trusted from the file, so the curated
subscription cannot drift out of the same containment the full file uses. The
curated file shipped unbanded, which meant its 45 reports went out as one
all-or-nothing S2F33: a single VID the tool did not implement cost it all 54
events - every DaVinci event, on the profile with the fullest commissioning
guide. It is now 10 bands, worst case 10 events.

Events whose `Valid Variables` cell is empty are written with an empty
`rptids` list. That is the same enable-without-link convention the NexGen MG
profile uses: an empty RPTID list in S2F35 means "delete this link", so such an
event is enabled without a report and identified by its CEID alone.

Requires pandas + openpyxl (both already in requirements.txt via pandas).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
XLSX = ROOT / "docs" / "vendor" / "SECS-Items_MueTec DaVinci 200 MC4_HC1.xlsx"
OUTPUT = ROOT / "output" / "davinci200_mc4_hc1" / "EventSubscription.full.json"
ACTIVE = ROOT / "output" / "davinci200_mc4_hc1" / "EventSubscription.json"
RPTID_BASE = 1_000_000_000


def _numbered(frame: pd.DataFrame) -> Dict[int, Dict[str, str]]:
    """{id: {name, description, variables}} for rows with a numeric ID.

    The sheet uses blank rows and section captions ("GEM services items") as
    visual grouping, so a non-numeric ID is a layout artefact, not data.
    """
    out: Dict[int, Dict[str, str]] = {}
    for _, row in frame.iterrows():
        try:
            item_id = int(row["ID"])
        except (TypeError, ValueError):
            continue
        name = str(row.get("Name", "")).strip()
        if not name or name == "nan":
            continue
        variables = str(row.get("Valid Variables", "")).strip()
        out[item_id] = {
            "name": name,
            "description": _clean(row.get("Description")),
            "variables": "" if variables in ("nan", "None") else variables,
        }
    return out


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text == "nan" else " ".join(text.split())


# Subscription bands by event family. The workbook groups events into named
# families by their 4-digit prefix; each family is an independent S2F33/35/37
# band so one wrong or unimplemented constant degrades that family's feed and
# leaves the rest reporting (the same containment the NexGen profile uses).
DAVINCI_BANDS: Dict[int, str] = {
    3010: "gem_core",      # control state / alarms / spooling / EC changes
    3020: "gem_core",
    3030: "gem_core",
    3040: "gem_core",
    3050: "material",      # material received / removed
    3060: "material_transfer",  # PM1/TM1/LP1/LP2 received/sent substrate
    3070: "material_transfer",
    3080: "material_transfer",
    3090: "material_transfer",
    3100: "operations",     # remote commands / terminal / PP / process state
    3110: "operations",
    3120: "operations",
    3130: "operations",
    3140: "process_module_1",
    3150: "transfer_module",
    3160: "load_port_1",
    3170: "load_port_2",
    3180: "id_reader",
    3190: "process_jobs",
    3200: "control_jobs",
    3210: "carrier",
    3220: "substrate",
    3230: "e39_errors",
}


def _davinci_band(ceid: int) -> str:
    return DAVINCI_BANDS.get(ceid // 1000, "other")


def _apply_bands(data: Dict[str, Any], source: Path) -> Dict[str, int]:
    """Derive `band` on every event and report, in place.

    A report belongs to the band of the event that links it, because
    `EventSubscriptionManager._bands()` groups both by the same string and
    S2F35 may only link a CEID to an RPTID the same band's S2F33 defined. Two
    situations would break that invariant silently, so both raise instead:

      * a report two events in different bands both link - the report can only
        be defined in one of them, and the other band's S2F35 would reference
        an RPTID that band never defined;
      * a report no event links - it would be defined and never used, and
        with no event to borrow a band from there is nothing to derive.

    Neither occurs in either shipped file today. If the curation changes so
    that one does, that is a decision for a person, not a default.
    """
    linked: Dict[int, str] = {}
    conflicts: List[str] = []
    for event in data["events"]:
        band = _davinci_band(int(event["ceid"]))
        event["band"] = band
        for rptid in event.get("rptids") or []:
            previous = linked.setdefault(int(rptid), band)
            if previous != band:
                conflicts.append(
                    f"RPTID {rptid} is linked from band {previous!r} and "
                    f"band {band!r} (CEID {event['ceid']})"
                )
    orphans = [
        int(report["rptid"])
        for report in data["reports"]
        if int(report["rptid"]) not in linked
    ]
    if conflicts or orphans:
        detail = "; ".join(conflicts)
        if orphans:
            detail += f"{'; ' if detail else ''}reports no event links: {orphans}"
        raise SystemExit(f"{source.name}: cannot derive report bands - {detail}")
    for report in data["reports"]:
        report["band"] = linked[int(report["rptid"])]
    counts: Dict[str, int] = {}
    for event in data["events"]:
        counts[event["band"]] = counts.get(event["band"], 0) + 1
    return counts


def build() -> Dict[str, Any]:
    sheets = pd.ExcelFile(XLSX)
    events = _numbered(sheets.parse("Events"))
    dvs = _numbered(sheets.parse("DV"))

    existing = json.loads(OUTPUT.read_text(encoding="utf-8"))
    # Preserve every existing report verbatim. The V[] ordering inside a report
    # was derived from the vendor's `Valid Variables` ordering when this file
    # was first built; re-deriving it here risks silently reordering a
    # positional decode that the mapper and its tests already depend on.
    reports: List[Dict[str, Any]] = list(existing.get("reports", []))
    # CarrierTag/PageNumber names are reused for both load ports, but each port
    # has its own DVID namespace. Repair the historical LP1 -> LP2 copy error on
    # every regeneration.
    for report in reports:
        if str(report.get("name", "")).startswith("LP1/CarrierTag"):
            report["dvids"] = [2110001, 2110002]
    known = {int(event["ceid"]) for event in existing.get("events", [])}
    kept = list(existing.get("events", []))
    # The band is derived, never trusted from a stale file.
    for event in kept:
        event["band"] = _davinci_band(int(event["ceid"]))

    added = 0
    for ceid in sorted(set(events) - known):
        item = events[ceid]
        if item["variables"]:
            raise SystemExit(
                f"CEID {ceid} ({item['name']}) has valid variables but no "
                "report. Build its report layout from the spreadsheet's "
                "'Valid Variables' column before adding it."
            )
        kept.append({
            "ceid": ceid,
            "name": item["name"],
            "description": item["description"]
            or f"DaVinci collection event {ceid}",
            "rptids": [],
            "band": _davinci_band(ceid),
            "enabled": True,
        })
        added += 1

    kept.sort(key=lambda event: int(event["ceid"]))
    dvid_names = dict(existing.get("dvid_names") or {})
    for dvid, item in sorted(dvs.items()):
        dvid_names.setdefault(str(dvid), item["name"])

    return {
        "description": (
            "DaVinci 200 MC4 HC1 FULL event subscription: every collection "
            "event the vendor spreadsheet documents. Reference only - the "
            "active EventSubscription.json is curated to the events the "
            "canonical mapper categorizes, so no 'unknown' telemetry is "
            "published."
        ),
        "_source": "docs/vendor/SECS-Items_MueTec DaVinci 200 MC4_HC1.xlsx",
        "_generated_by": "scripts/gen_davinci_full_subscription.py",
        "_manual_note": existing.get("_manual_note", ""),
        "_events_without_reports": (
            "An empty rptids list is deliberate: the spreadsheet lists no "
            "valid variables for that event, so it is enabled without a "
            "report and identified by its CEID alone. An empty RPTID list in "
            "S2F35 means 'delete this link'."
        ),
        "reports": reports,
        "events": kept,
        "dvid_names": dvid_names,
        "event_names": {
            str(ceid): item["name"] for ceid, item in sorted(events.items())
        },
    }, added


def main() -> None:
    data, added = build()
    full_bands = _apply_bands(data, OUTPUT)
    OUTPUT.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {OUTPUT}: {len(data['events'])} events "
        f"({added} newly added), {len(data['reports'])} reports, "
        f"{len(data['dvid_names'])} named data variables, "
        f"{len(full_bands)} bands"
    )

    # Membership is left exactly as curated; only `band` is rewritten.
    active = json.loads(ACTIVE.read_text(encoding="utf-8"))
    active_bands = _apply_bands(active, ACTIVE)
    ACTIVE.write_text(json.dumps(active, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {ACTIVE}: {len(active['events'])} events, "
        f"{len(active['reports'])} reports, {len(active_bands)} bands "
        f"(largest {max(active_bands.values())} events)"
    )


if __name__ == "__main__":
    main()
