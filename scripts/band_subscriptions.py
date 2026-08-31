"""Give every shipped EventSubscription.json its subscription bands.

Why this exists
---------------
S2F33 and S2F35 are all-or-nothing. SEMI E5 equipment rejects the *entire*
message when it detects any error in it, and DRACK=4 means "invalid VID". So a
subscription sent as one batch stakes every collection event on every constant
being right - and these tables are transcribed from vendor PDFs and a
spreadsheet, for tools that have never been connected.

Bands are the containment: each band is an independent define/link/enable
round trip, so a refused band degrades that family's feed and leaves the rest
reporting. `gateway.event_subscription.EventSubscriptionManager` already
implements this; it groups by the `band` field and gives each group its own
sequence.

The measured blast radius before this script, rejecting one VID and taking the
worst case per profile:

    davinci200_mc4_hc1   1 band     0 of 54 events survive   (100% lost)
    spts_fxp_omega       1 band    42 of 96 events survive   ( 56% lost)
    nexgen_mg_series    31 bands  163 of 243 events survive  ( 33% lost)

The DaVinci is the profile with the full commissioning guide and the one most
likely to meet real hardware first, and a single unknown VID left it connected
and reporting nothing at all.

SPTS was a half-measure: its 96 events already carried 7 band labels, but all
43 of its reports sat in one unnamed band, so the S2F35 leg was contained and
the S2F33 leg was not.

What it does
------------
Events take the band their profile's own generator assigns by CEID family.
Reports take the band of the event that references them - verified 1:1 in both
files, with no shared and no orphaned reports, so the assignment is
unambiguous.

Idempotent: running it twice changes nothing. Re-run it after regenerating any
subscription file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent


def _davinci_band(ceid: int, _dvids: List[int]) -> str:
    """Workbook event families, keyed by the 4-digit CEID prefix.

    Mirrors DAVINCI_BANDS in gen_davinci_full_subscription.py; kept in step
    with it by test_subscription_bands.py. DaVinci has no large-report
    isolation, so the report's DVID count is unused.
    """
    from scripts.gen_davinci_full_subscription import DAVINCI_BANDS

    return DAVINCI_BANDS.get(ceid // 1000, "other")


def _spts_band(ceid: int, dvids: List[int]) -> str:
    """Table 5 families, plus the generator's large-report isolation.

    `gen_spts_subscription._band_for` splits a report wider than
    LARGE_REPORT_DVIDS into its own `_ceidN` band. Delegating to the
    family-only `_spts_band` here flattened CEID 858 (172 DVIDs) back into the
    family band and re-widened the S2F33 blast radius the isolation exists to
    contain.
    """
    from scripts.gen_spts_subscription import _band_for

    return _band_for(ceid, dvids)


def band_subscription(
    data: Dict[str, Any], band_for_ceid: Callable[[int, List[int]], str]
) -> Tuple[int, int]:
    """Assign bands in place. Returns (events changed, reports changed).

    Reports are banded from the event that references them rather than from
    their own RPTID: the RPTID -> CEID convention (CEID + 1_000_000_000) holds
    for most reports but is not guaranteed for every hand-added one, and the
    reference is the fact that actually matters - a report must be defined in
    the same band as the event that links to it, or S2F35 arrives before its
    S2F33 has been accepted.

    `band_for_ceid` receives the CEID and the DVID list of the event's first
    report, because the SPTS banding keys a report's isolation off its width.
    """
    events: List[Dict[str, Any]] = data.get("events", [])
    reports: List[Dict[str, Any]] = data.get("reports", [])

    dvids_by_rptid: Dict[int, List[int]] = {
        int(report["rptid"]): list(report.get("dvids", []) or [])
        for report in reports
    }

    band_by_rptid: Dict[int, str] = {}
    events_changed = 0
    for event in events:
        rptids = event.get("rptids", []) or []
        dvids = dvids_by_rptid.get(int(rptids[0]), []) if rptids else []
        band = band_for_ceid(int(event["ceid"]), dvids)
        if event.get("band") != band:
            events_changed += 1
        event["band"] = band
        for rptid in rptids:
            band_by_rptid[int(rptid)] = band

    reports_changed = 0
    for report in reports:
        rptid = int(report["rptid"])
        # A report no event references is dead weight in the subscription, but
        # it still has to land somewhere; give it its own band so it cannot
        # take a live family down with it.
        band = band_by_rptid.get(rptid, f"unreferenced_{rptid}")
        if report.get("band") != band:
            reports_changed += 1
        report["band"] = band
    return events_changed, reports_changed


TARGETS: Tuple[Tuple[str, Callable[[int, List[int]], str]], ...] = (
    ("output/davinci200_mc4_hc1/EventSubscription.json", _davinci_band),
    ("output/spts_fxp_omega/EventSubscription.json", _spts_band),
)


def main() -> None:
    for relative, band_for_ceid in TARGETS:
        path = ROOT / relative
        data = json.loads(path.read_text(encoding="utf-8"))
        events_changed, reports_changed = band_subscription(data, band_for_ceid)
        # Atomic replace: a crash mid-write must not corrupt a shipped
        # subscription file that a real tool will be configured from.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        bands = {event["band"] for event in data.get("events", [])}
        print(
            f"{relative}: {len(bands)} bands "
            f"({events_changed} events, {reports_changed} reports rebanded)"
        )


if __name__ == "__main__":
    main()
