"""NexGen Wafersystems MG series (MG21/MG22/MG22-300) profile tables.

Split by concern - variables, metric tables, reports, CEIDs, subscription
bands and event aliases - and re-exported here so callers can keep using
``eap_middleware.profiles`` unchanged.
"""

from .variables import (
    NEXGEN_MG_SVIDS,
    mg_port_lot_dvs,
    MG_PORT1_LOT_CHEMISTRY,
    mg_pm_identity_dvs,
)
from .metrics import (
    NEXGEN_MG_METRIC_DVS,
    NEXGEN_MG_METRIC_REPORTS,
    NEXGEN_MG_METRIC_BANDS,
)
from .reports import (
    NEXGEN_MG_DVS,
    NEXGEN_MG_REPORTS,
)
from .ceids import (
    NEXGEN_MG_CEID_DV_LAYOUT,
    NEXGEN_MG_CEID_LOAD_PORT,
    NEXGEN_MG_CEID_CHAMBER,
    NEXGEN_MG_CHAMBER_EVENT_CEIDS,
    NEXGEN_MG_CEID_STATE_TRANSITIONS,
    NEXGEN_MG_CEID_ALIASES,
)
from .bands import (
    MG_BAND_CORE,
    MG_BAND_PM1,
    MG_BAND_PM2,
    MG_BAND_RECIPE,
    MG_BAND_AUX,
    MG_BAND_SLOT_MAP,
    MG_BAND_WAFER_ALIGNMENT,
    MG_BAND_GEM300_PROCESS_JOB,
    MG_BAND_GEM300_CONTROL_JOB,
    MG_BAND_GEM300_LP_STATE,
    MG_BAND_GEM300_CARRIER,
    MG_BAND_GEM300_LP_ACCESS,
    MG_BAND_GEM300_SUBSTRATE,
    MG_GEM300_BANDS,
    mg_band_gem300,
    mg_band_load_port,
    NEXGEN_MG_CEID_BANDS,
)
from .events import (
    nexgen_mg_event_aliases,
)

__all__ = [
    "MG_BAND_AUX",
    "MG_BAND_CORE",
    "MG_BAND_GEM300_CARRIER",
    "MG_BAND_GEM300_CONTROL_JOB",
    "MG_BAND_GEM300_LP_ACCESS",
    "MG_BAND_GEM300_LP_STATE",
    "MG_BAND_GEM300_PROCESS_JOB",
    "MG_BAND_GEM300_SUBSTRATE",
    "MG_BAND_PM1",
    "MG_BAND_PM2",
    "MG_BAND_RECIPE",
    "MG_BAND_SLOT_MAP",
    "MG_BAND_WAFER_ALIGNMENT",
    "MG_GEM300_BANDS",
    "MG_PORT1_LOT_CHEMISTRY",
    "NEXGEN_MG_CEID_ALIASES",
    "NEXGEN_MG_CEID_BANDS",
    "NEXGEN_MG_CEID_CHAMBER",
    "NEXGEN_MG_CEID_DV_LAYOUT",
    "NEXGEN_MG_CEID_LOAD_PORT",
    "NEXGEN_MG_CEID_STATE_TRANSITIONS",
    "NEXGEN_MG_CHAMBER_EVENT_CEIDS",
    "NEXGEN_MG_DVS",
    "NEXGEN_MG_METRIC_BANDS",
    "NEXGEN_MG_METRIC_DVS",
    "NEXGEN_MG_METRIC_REPORTS",
    "NEXGEN_MG_REPORTS",
    "NEXGEN_MG_SVIDS",
    "mg_band_gem300",
    "mg_band_load_port",
    "mg_pm_identity_dvs",
    "mg_port_lot_dvs",
    "nexgen_mg_event_aliases",
]
