"""SPTS fxP / Omega (Cimetrix) SECS-II tables.

Derived from the Omega SECSII SPTS fxP 200mm manual.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, Sequence

from .base import (
    TRANSITION_LP_ACTIVATE_1,
    TRANSITION_LP_ACTIVATE_2,
    TRANSITION_LP_DEACTIVATE_1,
    TRANSITION_LP_DEACTIVATE_2,
)


def _spts_wafer_tracking_svids() -> Dict[str, int]:
    out: Dict[str, int] = {}
    # VCE A wafer tracking SVIDs are slots 1-25 at ID 1201-1225 (manual 12.8).
    for slot in range(1, 26):
        out[f"VCEAWaferTrackingW{slot:02d}"] = 1200 + slot
        out[f"VCEBWaferTrackingW{slot:02d}"] = 1250 + slot
    return out


# Sourced from "Omega_SECSII_SPTS fxP 200mm SECSII Manual (Cimetrix)" sections
# 12.4 (GEM SVs), 12.8 (General Equipment-Specific SVs), and 12.10 (DVs).
SPTS_SVIDS: Dict[str, int] = {
    # GEM Status Variables (Section 12.4 / Table 7)
    "AlarmID": 22,
    "AlarmsEnabled": 23,
    "AlarmsSet": 24,
    "AlarmState": 25,
    "Aser": 26,
    "Clock": 27,
    "ControlState": 28,
    "DataID": 29,
    "EventsEnabled": 30,
    "CommState": 31,
    "MDLN": 32,
    "LastCEID": 34,
    "PreviousControlState": 36,
    "SOFTREV": 39,
    "Time": 40,
    "SpoolCountActual": 2016,
    "SpoolCountTotal": 2017,
    "SpoolFullTime": 2018,
    "SpoolStartTime": 2019,
    # General Equipment-Specific Status Variables (Section 12.8 / Table 9)
    "ArmAWaferStatus": 1100,
    "ArmBWaferStatus": 1101,
    "VCEAWaferStatus": 1102,
    "PM1WaferStatus": 1103,
    "PM2WaferStatus": 1104,
    "PM3WaferStatus": 1105,
    "PM4WaferStatus": 1106,
    "PM5WaferStatus": 1107,
    "PM6WaferStatus": 1108,
    "VCEBWaferStatus": 1109,
    "AlignerWaferStatus": 1110,
    "InCoolerWaferStatus": 1114,
    "BufferWaferStatus": 1115,
    **_spts_wafer_tracking_svids(),
    # Modes
    "TransportMode": 1500,
    "VCEAMode": 1501,
    "PM1Mode": 1502,
    "PM2Mode": 1503,
    "PM3Mode": 1504,
    "PM4Mode": 1505,
    "PM5Mode": 1506,
    "PM6Mode": 1507,
    "VCEBMode": 1508,
    "CoolerMode": 1510,
    # States
    "TransportState": 1550,
    "VCEAState": 1551,
    "PM1State": 1552,
    "PM2State": 1553,
    "PM3State": 1554,
    "PM4State": 1555,
    "PM5State": 1556,
    "PM6State": 1557,
    "VCEBState": 1558,
    "CoolerState": 1560,
    # Per-PM recipe selection
    "PM1ModuleRecipe": 1572,
    "PM2ModuleRecipe": 1573,
    "PM3ModuleRecipe": 1574,
    "PM4ModuleRecipe": 1575,
    "PM5ModuleRecipe": 1576,
    "PM6ModuleRecipe": 1577,
    # Process state per VCE
    "VCEAProcessState": 1601,
    "VCEBProcessState": 1602,
    "VCEAPreviousProcessState": 1611,
    "VCEBPreviousProcessState": 1612,
    "VCEAProcessPauseState": 1621,
    "VCEBProcessPauseState": 1622,
    # Per-VCE recipe & lot tracking
    "VCEACassetteRecipe": 1631,
    "VCEBCassetteRecipe": 1632,
    "VCEALotid": 1641,
    "VCEBLotid": 1642,
    "VCEACyclingEnabled": 1651,
    "VCEBCyclingEnabled": 1652,
    "VCEADataloggingConnected": 1661,
    "VCEBDataloggingConnected": 1662,
    # Wafer counters
    "MchTotalWaferCount": 1700,
    "PM1TotalWaferCount": 1701,
    "PM2TotalWaferCount": 1702,
    "PM3TotalWaferCount": 1703,
    "PM4TotalWaferCount": 1704,
    "PM5TotalWaferCount": 1705,
    "PM6TotalWaferCount": 1706,
    "MchRunningWaferCount": 1720,
    "PM1RunningWaferCount": 1721,
    "PM2RunningWaferCount": 1722,
    "PM3RunningWaferCount": 1723,
    "PM4RunningWaferCount": 1724,
    "PM5RunningWaferCount": 1725,
    "PM6RunningWaferCount": 1726,
    "NVSPath": 2039,
    "RecipeHandling": 2040,
    "RecipeType": 2041,
    "RecipePath": 2042,
    "RecipeExtension": 2048,
    "ResyncNVS": 2055,
    "EnergyConsumption": 5103,
    "LampTowerLamp1Status": 5200,
    "LampTowerLamp2Status": 5201,
    "LampTowerLamp3Status": 5202,
    "LampTowerLamp4Status": 5203,
    "LampTowerAlarmStatus": 5204,
    "PrevLamp1Status": 5205,
    "PrevLamp2Status": 5206,
    "PrevLamp3Status": 5207,
    "PrevLamp4Status": 5208,
    "PrevAlarmStatus": 5209,
    "CassetteWaferMapA": 5300,
    "CassetteWaferMapB": 5301,
    "CassetteLotMapA": 5302,
    "CassetteLotMapB": 5303,
    "EquipmentReady": 5310,
}


# Data Variables (used inside event reports - not pollable via S1F3, but the
# mapper looks them up by name to extract LotID/WaferID/Recipe from S6F11).
SPTS_DVS: Dict[str, int] = {
    "WaferStatisticalDataDV": 5100,
    "WaferRecipeDV": 5101,
    "ModuleRecipeDV": 5102,
    "CassetteID": 5110,
    "WaferID": 5111,
    "CassetteSlotMap": 5112,
    "StationID": 5113,
    "LotID": 5114,
    "RecipeID": 5115,
    "StepID": 5116,
    "StepName": 5117,
    "WaferNo": 5118,
    "PortID": 6102,
    "OperatorCommand": 16,
    "PPChangeName": 3,
    "PPChangeStatus": 4,
}


def _spts_pm_index_aliases(prefix: str, start: int) -> Dict[int, str]:
    return {start + i: f"PM{i + 1}{prefix}" for i in range(6)}


# Per-CEID V[] layout. Each list names the DVs in the order the equipment
# packs them into the S6F11 report. Sourced from the SPTS Omega SECSII Manual
# section 7 ("Valid DVs for Reports" column of Table 5).
_SPTS_RECIPE_DV_LAYOUT: Sequence[str] = (
    "WaferID", "StationID", "LotID", "RecipeID", "StepID", "StepName", "WaferNo",
)
_SPTS_WAFER_DV_LAYOUT: Sequence[str] = ("WaferID", "LotID", "WaferNo", "PortID")
_SPTS_PMWAFER_DV_LAYOUT: Sequence[str] = ("WaferID", "StationID", "WaferNo")
_SPTS_CASSETTE_DV_LAYOUT: Sequence[str] = ("CassetteID", "PortID", "LotID")

SPTS_CEID_DV_LAYOUT: Dict[int, Sequence[str]] = {
    3: ("PortID",),
    4: ("PortID",),
    6: ("PortID", "OperatorCommand"),
    7: ("PPChangeName", "PPChangeStatus"),
    24: ("ECID", "ECChangeName", "ECChangeValue"),
    810: ("PortID", "WaferStatisticalDataDV", "WaferRecipeDV", "ModuleRecipeDV",
          "StepName", "LotID"),
    850: ("CassetteSlotMap", "PortID"),
    851: _SPTS_CASSETTE_DV_LAYOUT,
    852: _SPTS_CASSETTE_DV_LAYOUT,
    853: _SPTS_PMWAFER_DV_LAYOUT,
    854: _SPTS_PMWAFER_DV_LAYOUT,
    855: _SPTS_RECIPE_DV_LAYOUT,
    856: _SPTS_RECIPE_DV_LAYOUT,
    857: _SPTS_RECIPE_DV_LAYOUT,
    858: _SPTS_RECIPE_DV_LAYOUT,
    859: _SPTS_WAFER_DV_LAYOUT,
    860: _SPTS_WAFER_DV_LAYOUT,
    **{422 + i: _SPTS_RECIPE_DV_LAYOUT for i in range(6)},
    430: _SPTS_RECIPE_DV_LAYOUT,
    **{442 + i: _SPTS_RECIPE_DV_LAYOUT for i in range(6)},
    450: _SPTS_RECIPE_DV_LAYOUT,
    **{462 + i: _SPTS_RECIPE_DV_LAYOUT for i in range(6)},
    470: _SPTS_RECIPE_DV_LAYOUT,
    **{482 + i: _SPTS_RECIPE_DV_LAYOUT for i in range(6)},
    490: _SPTS_RECIPE_DV_LAYOUT,
    **{880 + i: _SPTS_PMWAFER_DV_LAYOUT for i in range(6)},
    **{886 + i: _SPTS_PMWAFER_DV_LAYOUT for i in range(6)},
}

# SPTS *1 family = VCE A (load port 1), *2 family = VCE B (load port 2).
SPTS_CEID_LOAD_PORT: Dict[int, str] = {
    **{ceid: "1" for ceid in (100, 330, 336, 342, 348, 354, 360, 366, 372, 378, 384,
                              390, 520, 721, 731, 741, 751, 761, 771, 781, 791, 801)},
    **{ceid: "2" for ceid in (101, 331, 337, 343, 349, 355, 361, 367, 373, 379, 385,
                              391, 521, 722, 732, 742, 752, 762, 772, 782, 792, 802)},
}


# v2 Track A: SPTS uses VCE A (LP1) / VCE B (LP2) terminology. *1 family of
# CEIDs activates LP1, *2 family activates LP2. MBCStart = cassette/lot
# start, MBCComplete = end. SMIFPodPresent/Absent also count as activation /
# deactivation since they bracket carrier physical presence.
SPTS_CEID_STATE_TRANSITIONS: Dict[int, str] = {
    330: TRANSITION_LP_ACTIVATE_1,
    331: TRANSITION_LP_ACTIVATE_2,
    336: TRANSITION_LP_DEACTIVATE_1,
    337: TRANSITION_LP_DEACTIVATE_2,
    721: TRANSITION_LP_ACTIVATE_1,    # SMIFPodPresent1
    722: TRANSITION_LP_ACTIVATE_2,    # SMIFPodPresent2
    731: TRANSITION_LP_DEACTIVATE_1,  # SMIFPodAbsent1
    732: TRANSITION_LP_DEACTIVATE_2,  # SMIFPodAbsent2
    851: TRANSITION_LP_ACTIVATE_1,    # CassetteStarted - assume VCE A unless
                                      # superseded by an explicit *1/*2 CEID
    852: TRANSITION_LP_DEACTIVATE_1,  # CassetteComplete
}

# v2 Track A: SPTS PM/cooler events that fire from a process module without
# naming a VCE. Recipe start/end/step + per-PM wafer-in/out + the generic
# ProcessingStarted/Finished pair. ALL routed through JobTracker.
SPTS_CHAMBER_EVENT_CEIDS: FrozenSet[int] = frozenset(
    set(range(422, 428))     # PM1-6 RecipeStart
    | {430}                  # CoolerRecipeStart
    | set(range(442, 448))   # PM1-6 RecipeEnd
    | {450}                  # CoolerRecipeEnd
    | set(range(462, 468))   # PM1-6 RecipeStepStart
    | {470}                  # CoolerRecipeStepStart
    | set(range(482, 488))   # PM1-6 RecipeStepEnd
    | {490}                  # CoolerRecipeStepEnd
    | set(range(880, 886))   # PM1-6 WaferIn
    | set(range(886, 892))   # PM1-6 WaferOut
    | {853, 854, 855, 856, 857, 858, 859, 860, 810}  # generic PM events
)


SPTS_CEID_ALIASES: Dict[int, str] = {
    # Generic GEM lifecycle events (Section 7)
    3: "MaterialReceived",
    4: "MaterialRemoved",
    6: "OperatorCommandIssued",
    7: "PPChange",
    8: "EquipmentOffline",
    9: "ControlStateLocal",
    10: "ControlStateRemote",
    15: "MessageRecognition",
    16: "SpoolTransmitFailure",
    17: "SpoolingActivated",
    18: "SpoolingDeactivated",
    19: "HostECChange",
    20: "HostPPChange",
    24: "ECChange",
    # Per-cassette process-state notification (manual Table 5). The tool also
    # publishes 52 individual transition CEIDs (151-176 for VCE A, 181-206 for
    # VCE B) covering the section 15.2 Table 11 state machine; those are not
    # subscribed because the per-lot CSV lifecycle already comes from the
    # MBC/MB/PM families. These two are, because they are the cheapest way to
    # see a cassette enter STOPPING, RESTARTING or ABANDONING - the states that
    # explain why a lot stalled, and which were otherwise invisible to the host.
    100: "ProcessStateChange1",
    101: "ProcessStateChange2",
    # Lot lifecycle - MBC = Multi-Batch Cassette / cassette processing
    330: "MBCStart1",
    331: "MBCStart2",
    336: "MBCComplete1",
    337: "MBCComplete2",
    # Per-wafer lifecycle
    342: "MBStart1",
    343: "MBStart2",
    348: "MBComplete1",
    349: "MBComplete2",
    # Operator control
    354: "OpSelect1",
    355: "OpSelect2",
    360: "OpStart1",
    361: "OpStart2",
    366: "OpStop1",
    367: "OpStop2",
    372: "OpPause1",
    373: "OpPause2",
    378: "OpResume1",
    379: "OpResume2",
    384: "OpCancel1",
    385: "OpCancel2",
    390: "OpAbandon1",
    391: "OpAbandon2",
    # Recipe lifecycle per PM
    **_spts_pm_index_aliases("RecipeStart", 422),
    430: "CoolerRecipeStart",
    **_spts_pm_index_aliases("RecipeEnd", 442),
    450: "CoolerRecipeEnd",
    # Lot/Cassette events
    520: "ReadyForProcessA",
    521: "ReadyForProcessB",
    721: "SMIFPodPresent1",
    722: "SMIFPodPresent2",
    731: "SMIFPodAbsent1",
    732: "SMIFPodAbsent2",
    741: "SMIFPodClamped1",
    742: "SMIFPodClamped2",
    751: "SMIFPodUnClamped1",
    752: "SMIFPodUnClamped2",
    761: "SMIFPodHomed1",
    762: "SMIFPodHomed2",
    771: "VCEAMaterialPresent",
    772: "VCEBMaterialPresent",
    781: "VCEAMaterialAbsent",
    782: "VCEBMaterialAbsent",
    791: "VCEALoadComplete",
    792: "VCEBLoadComplete",
    801: "VCEAUnloadComplete",
    802: "VCEBUnloadComplete",
    810: "WaferStatisticalDataAvailable",
    850: "SlotMapRead",
    851: "CassetteStarted",
    852: "CassetteComplete",
    853: "PMWaferIn",
    854: "PMWaferOut",
    855: "ProcessingStarted",
    856: "ProcessingFinished",
    857: "RecipeStepStart",
    858: "RecipeStepEnd",
    859: "WaferStarted",
    860: "WaferComplete",
    **{880 + i: f"PM{i + 1}WaferIn" for i in range(6)},
    **{886 + i: f"PM{i + 1}WaferOut" for i in range(6)},
}
