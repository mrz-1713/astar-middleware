"""NexGen MG subscription bands.

The middleware issues S2F33/S2F35/S2F37 once per band so that a CEID a given
MG variant does not implement degrades one family instead of voiding the whole
subscription.
"""
from __future__ import annotations

from typing import Dict

from .ceids import NEXGEN_MG_CEID_ALIASES, NEXGEN_MG_CEID_LOAD_PORT
from .metrics import NEXGEN_MG_METRIC_BANDS


# Subscription bands. Report definition and event linking are all-or-nothing
# per message (the manual rejects the whole message on any error, and S2F36
# has a dedicated "at least one CEID does not exist" code), so one CEID that a
# given MG variant does not implement would void an unbanded subscription.
# Splitting by family limits a wrong constant to degrading one family's feed.


MG_BAND_CORE = "core_gem"
MG_BAND_PM1 = "process_module_1"
MG_BAND_PM2 = "process_module_2"
MG_BAND_RECIPE = "recipe"
MG_BAND_AUX = "metrology_aux"
MG_BAND_SLOT_MAP = "slot_map"
MG_BAND_WAFER_ALIGNMENT = "wafer_alignment"

# The GEM300 range 700-879 used to be one band of 79 reports and 80 events -
# by far the largest S2F33 the middleware sent, and the one most likely to
# contain a CEID a given MG variant does not implement, because GEM300 is
# where variants differ most (a tool with no carrier ID reader has no 772-790,
# a tool without E94 has no 720-733). One such CEID took all 80 down.
#
# Split along the SEMI standard that owns each range, which is also the
# granularity at which a variant omits them. Worst case is now 19 events, and
# a tool without an ID reader loses only its carrier band.
MG_BAND_GEM300_PROCESS_JOB = "gem300_process_job"      # E40, 700-719
MG_BAND_GEM300_CONTROL_JOB = "gem300_control_job"      # E94, 720-750
MG_BAND_GEM300_LP_STATE = "gem300_load_port_state"     # E87, 751-771
MG_BAND_GEM300_CARRIER = "gem300_carrier"              # E87, 772-800
MG_BAND_GEM300_LP_ACCESS = "gem300_load_port_access"   # E87, 801-849
MG_BAND_GEM300_SUBSTRATE = "gem300_substrate"          # E90, 850-879

MG_GEM300_BANDS = (
    MG_BAND_GEM300_PROCESS_JOB,
    MG_BAND_GEM300_CONTROL_JOB,
    MG_BAND_GEM300_LP_STATE,
    MG_BAND_GEM300_CARRIER,
    MG_BAND_GEM300_LP_ACCESS,
    MG_BAND_GEM300_SUBSTRATE,
)


def mg_band_gem300(ceid: int) -> str:
    """The GEM300 sub-band that owns one CEID in the 700-879 range."""
    if ceid < 720:
        return MG_BAND_GEM300_PROCESS_JOB
    if ceid < 751:
        return MG_BAND_GEM300_CONTROL_JOB
    if ceid < 772:
        return MG_BAND_GEM300_LP_STATE
    if ceid < 801:
        return MG_BAND_GEM300_CARRIER
    if ceid < 850:
        return MG_BAND_GEM300_LP_ACCESS
    return MG_BAND_GEM300_SUBSTRATE


def mg_band_load_port(port: int) -> str:
    return f"load_port_{port}"


def _mg_ceid_bands() -> Dict[int, str]:
    bands: Dict[int, str] = {}
    for ceid in NEXGEN_MG_CEID_ALIASES:
        if ceid in (12, 13):
            band = MG_BAND_RECIPE
        elif ceid < 120:
            band = MG_BAND_CORE
        elif ceid == 145:
            # The only report that puts status variables in an event report;
            # isolated so that assumption cannot cost a port its lifecycle.
            band = MG_BAND_SLOT_MAP
        elif ceid == 600:
            # WaferAlignmentStatus is the ONLY metrology/aux CEID that carries
            # a report. Left in metrology_aux it made that band 1 report and
            # 61 report-less events, so one VID this variant does not
            # implement failed the band's S2F33 and cost all 62 - the largest
            # remaining single-refusal loss on the profile. The other 61 have
            # no S2F33 leg at all and cannot be lost that way. Same reasoning
            # as CEID 145 above.
            band = MG_BAND_WAFER_ALIGNMENT
        elif ceid < 200:
            # One band PER LOAD PORT, not one for all four. Port count is the
            # most likely difference between MG variants, and an MG with two
            # ports would reject the CEIDs for ports 3 and 4 - taking every
            # port's lot-file open and close down with them if they shared a
            # band. Split this way, a 2-port tool simply loses two empty bands.
            band = mg_band_load_port(int(NEXGEN_MG_CEID_LOAD_PORT[ceid]))
        elif ceid < 300:
            band = MG_BAND_PM1
        elif ceid < 400:
            band = MG_BAND_PM2
        elif 700 <= ceid < 880:
            band = mg_band_gem300(ceid)
        else:
            band = MG_BAND_AUX
        # A CEID that carries process metrics moves wholesale into that
        # chemistry family's band. It cannot sit in two: the generator emits
        # one event entry per CEID, and S2F35 has a dedicated ack code for
        # "at least one CEID link already defined" (LRACK=3), so linking one
        # event from two bands is refused on a conforming tool.
        bands[ceid] = NEXGEN_MG_METRIC_BANDS.get(ceid, band)
    return bands


NEXGEN_MG_CEID_BANDS: Dict[int, str] = _mg_ceid_bands()
