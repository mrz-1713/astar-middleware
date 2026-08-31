"""MueTec DaVinci 200 MC4/HC1 SECS-II tables.

Derived from the SECS-Items workbook shipped with the tool.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, Sequence

from .base import (
    TRANSITION_CTRL_JOB_END,
    TRANSITION_CTRL_JOB_START,
    TRANSITION_LP_ACTIVATE_1,
    TRANSITION_LP_ACTIVATE_2,
    TRANSITION_LP_ACTIVATE_FROM_PAYLOAD,
    TRANSITION_LP_DEACTIVATE_1,
    TRANSITION_LP_DEACTIVATE_2,
    TRANSITION_LP_DEACTIVATE_FROM_PAYLOAD,
)


DAVINCI_SVIDS = {
    "ControlState": 1010001,
    "PreviousControlState": 1010002,
    "EventsEnabled": 1010003,
    "LastEventID": 1010004,
    "Clock": 1010005,
    "AlarmsEnabled": 1020001,
    "AlarmsSet": 1020002,
    "SpoolCountActual": 1030001,
    "SpoolCountTotal": 1030002,
    "SpoolStartTime": 1030003,
    "SpoolFullTime": 1030004,
    "PPError": 1040001,
    "ProcessState": 1050001,
    "PreviousProcessState": 1050002,
    "PM1/OperationMode": 1060005,
    "PM1/RecipeActive": 1060006,
    "PM1/RecipeName": 1060007,
    "RecipeName": 1060007,  # alias
    "PM1/ReadyForProcess": 1060008,
    "TM1/OperationMode": 1070001,
    "TM1/WaferID": 1070002,
    "LP1/E84Busy": 1080001,
    "LP1/E84CommunicationsValid": 1080002,
    "LP1/E84Complete": 1080003,
    "LP1/E84CS0": 1080004,
    "LP1/E84CS1": 1080005,
    "LP1/E84EmergencyStop": 1080006,
    "LP1/E84HandoffAvailable": 1080007,
    "LP1/E84LoadRequest": 1080008,
    "LP1/E84Ready": 1080009,
    "LP1/E84UnloadRequest": 1080010,
    "LP1/ClampStatus": 1080011,
    "LP1/DoorStatus": 1080012,
    "LP1/IsMapped": 1080013,
    "LP1/CarrierPresentStatus": 1080014,
    "LP1/MaterialMap": 1080015,
    "LP1/OperationMode": 1080016,
    "LP1/State": 1080017,
    "LP2/E84Busy": 1090001,
    "LP2/E84CommunicationsValid": 1090002,
    "LP2/E84Complete": 1090003,
    "LP2/E84CS0": 1090004,
    "LP2/E84CS1": 1090005,
    "LP2/E84EmergencyStop": 1090006,
    "LP2/E84HandoffAvailable": 1090007,
    "LP2/E84LoadRequest": 1090008,
    "LP2/E84Ready": 1090009,
    "LP2/E84UnloadRequest": 1090010,
    "LP2/ClampStatus": 1090011,
    "LP2/DoorStatus": 1090012,
    "LP2/IsMapped": 1090013,
    "LP2/CarrierPresentStatus": 1090014,
    "LP2/MaterialMap": 1090015,
    "LP2/OperationMode": 1090016,
    "LP2/State": 1090017,
    "QueueAvailableSpace": 1100001,
    "QueuedCJobs": 1100002,
    "CarrierLocationMatrix": 1110001,
    "LoadPortReservationStateList": 1110002,
    "PortTransferStateList": 1110003,
    "PortAssociationStateList": 1110004,
    "PortStateInfoList": 1110005,
    "LP1/CarrierID": 1120001,
    "LP1/PortID": 1120002,
    "LP1/PortTransferState": 1120003,
    "LP1/PortAssociationState": 1120004,
    "LP1/PortStateInfo": 1120005,
    "LP1/LoadPortReservationState": 1120006,
    "LP1/AccessMode": 1120007,
    "LP1/ClampedLocation": 1120008,
    "LP1/DockedLocation": 1120009,
    "LP1/MaterialID": 1120010,
    "LP1/LocationID": 1120011,
    "LP2/CarrierID": 1130001,
    "LP2/PortID": 1130002,
    "LP2/PortTransferState": 1130003,
    "LP2/PortAssociationState": 1130004,
    "LP2/PortStateInfo": 1130005,
    "LP2/LoadPortReservationState": 1130006,
    "LP2/AccessMode": 1130007,
    "LP2/ClampedLocation": 1130008,
    "LP2/DockedLocation": 1130009,
    "LP2/MaterialID": 1130010,
    "LP2/LocationID": 1130011,
    "PM1/Station1/SubstLocID": 1140001,
    "PM1/Station1/SubstLocState": 1140002,
    "PM1/Station1/SubstID": 1140003,
    "TM1/Arm1/SubstLocID": 1150001,
    "TM1/Arm1/SubstLocState": 1150002,
    "TM1/Arm1/SubstID": 1150003,
    "TM1/Arm2/SubstLocID": 1150004,
    "TM1/Arm2/SubstLocState": 1150005,
    "TM1/Arm2/SubstID": 1150006,
    "AL/Station1/SubstLocID": 1160001,
    "AL/Station1/SubstLocState": 1160002,
    "AL/Station1/SubstID": 1160003,
    "FFUGaugePressurePM": 1170001,
    "FFUGaugePressureEFEM1": 1170002,
    "FFUGaugePressureEFEM2": 1170003,
    "MainPressure": 1170004,
    "MainVacuumEFEM": 1170005,
    "MainVacuumPM": 1170006,
    "Vacuum8PM1": 1170007,
    "Vacuum12PM1": 1170008,
    "Vacuum8PM2": 1170009,
    "Vacuum12PM2": 1170010,
    "FFUFan1EFEM": 1170011,
    "FFUFan2EFEM": 1170012,
    "FFUFan3EFEM": 1170013,
    "FFUFan4EFEM": 1170014,
    "FFUFan1PM": 1170015,
    "FFUFan2PM": 1170016,
    "FFUFan3PM": 1170017,
    "FFUFan4PM": 1170018,
}


# Per-CEID V[] layout sourced from the SECS-Items_MueTec DaVinci 200 MC4_HC1
# workbook's Events sheet, "Valid Variables" column. The trailing aliases
# (WaferID, LotID, SubstID) let the mapper's generic _get_first lookups find
# the same data without having to know about the *List variants - they all
# resolve to the first element of the list via _scalar() in the mapper.
_DAVINCI_SUBST_LIST_LAYOUT: Sequence[str] = (
    "SubstIDStatusList", "SubstSubstLocIDList", "SubstDestinationList",
    "SubstSourceList", "SubstHistoryList", "SubstMtrlStatusList",
    "AcquiredIDList", "SubstIDList", "SubstProcStateList", "SubstStateList",
    "SubstLotIDList", "SubstTypeList", "SubstUsageList",
)

DAVINCI_CEID_DV_LAYOUT: Dict[int, Sequence[str]] = {
    3010004: ("OperatorCommand",),
    3020001: ("AlarmID", "AlarmCode", "AlarmText"),
    3020002: ("AlarmID", "AlarmCode", "AlarmText"),
    3050001: ("PortID",),
    3050002: ("PortID",),
    3080001: ("SubstID", "SubstLocID"),
    3080002: ("SubstID", "SubstLocID"),
    3090001: ("SubstID", "SubstLocID"),
    3090002: ("SubstID", "SubstLocID"),
    3140002: ("WaferID", "LotID", "RecipeName"),
    3140003: ("WaferID", "LotID", "RecipeName", "ResultFile", "ResultPath",
              "PathOfImages", "TestResults"),
    3140004: ("WaferID", "LotID", "RecipeName", "AbortReason"),
    3140005: ("RecipeName",),
    3140006: ("RecipeName",),
    3140007: ("WaferID", "LotID", "SlotID", "UnitFoupID", "Results"),
    3160002: ("CarrierID",),
    3170002: ("CarrierID",),
    3210001: ("CarrierID",),
    3210002: ("LocationID", "PortID"),
    3210003: ("CarrierID", "LocationID", "PortID"),
    3210006: ("CarrierID", "PortID"),
    3210007: ("PortID",),
    3210009: ("CarrierID", "LocationID", "PortID"),
    3190044: ("PRJobID",),
    3190045: ("PRJobID",),
    3190046: ("PRJobID",),
    3190047: ("PRJobID",),
    3190048: ("PRJobID", "PRJobState"),
    3190050: ("PRJobID",),
    3200001: ("CtrlJobID",),
    3200002: ("CtrlJobID",),
    3200003: ("CtrlJobID",),
    3200008: ("CtrlJobID",),
    3200013: ("CtrlJobID",),
    3200017: ("CtrlJobID",),
    3220013: _DAVINCI_SUBST_LIST_LAYOUT,
    3220014: _DAVINCI_SUBST_LIST_LAYOUT,
    3220016: _DAVINCI_SUBST_LIST_LAYOUT,
    3220017: _DAVINCI_SUBST_LIST_LAYOUT,
    3220018: _DAVINCI_SUBST_LIST_LAYOUT,
    3220019: _DAVINCI_SUBST_LIST_LAYOUT,
    3220020: _DAVINCI_SUBST_LIST_LAYOUT,
    3220021: _DAVINCI_SUBST_LIST_LAYOUT,
    3220022: _DAVINCI_SUBST_LIST_LAYOUT,
    3220023: _DAVINCI_SUBST_LIST_LAYOUT,
}

# DaVinci CEIDs encode the load port in the name (LP1/* = port 1, LP2/* = 2).
DAVINCI_CEID_LOAD_PORT: Dict[int, str] = {
    **{ceid: "1" for ceid in (3080001, 3080002,
                              3160001, 3160002, 3160005, 3160006)},
    **{ceid: "2" for ceid in (3090001, 3090002,
                              3170001, 3170002, 3170005, 3170006)},
}


# v2 Track A: CEIDs the JobTracker watches to maintain LP-attribution state.
# Audit fix: the manual confirms LP1/2 CarrierArrived (3160001/3170001) have
# empty Valid Variables and are NOT included in the standard subscription
# (an empty RPTID list would delete the link). We drive LP activation from
# MaterialReceived (3050001) instead - it carries PortID and IS subscribed.
DAVINCI_CEID_STATE_TRANSITIONS: Dict[int, str] = {
    # Primary LP activation/deactivation via PortID payload (real DaVinci path)
    3050001: TRANSITION_LP_ACTIVATE_FROM_PAYLOAD,   # MaterialReceived
    3050002: TRANSITION_LP_DEACTIVATE_FROM_PAYLOAD, # MaterialRemoved
    # CarrierDeparted CEIDs encode LP in name and are subscribed (have CarrierID DV)
    3160002: TRANSITION_LP_DEACTIVATE_1, # LP1/CarrierDeparted
    3170002: TRANSITION_LP_DEACTIVATE_2, # LP2/CarrierDeparted
    # Kept for completeness if a custom subscription enables them later -
    # they're no-ops on the stock subscription because they don't fire.
    3160001: TRANSITION_LP_ACTIVATE_1,   # LP1/CarrierArrived (not subscribed stock)
    3170001: TRANSITION_LP_ACTIVATE_2,   # LP2/CarrierArrived (not subscribed stock)
    # ControlJob lifecycle for CtrlJobID disambiguation
    3200017: TRANSITION_CTRL_JOB_START,  # ControlJob:Selected-Executing
    3200002: TRANSITION_CTRL_JOB_END,    # ControlJob:Executing-Completed
    3200003: TRANSITION_CTRL_JOB_END,    # ControlJob:Active-Completed
    3200008: TRANSITION_CTRL_JOB_END,    # ControlJob:Active-Completed (variant)
    3200013: TRANSITION_CTRL_JOB_END,    # ControlJob:Completed-NoState
}

# v2 Track A: PM-chamber events that don't name an LP themselves. When the
# mapper hits one of these without a load_port from the payload or
# ceid_load_port map, it asks JobTracker for the active LP on the machine.
DAVINCI_CHAMBER_EVENT_CEIDS: FrozenSet[int] = frozenset({
    3140002,  # PM1/ProcessingStarted
    3140003,  # PM1/ProcessingFinished
    3140004,  # PM1/ProcessingAborted
    3140005,  # PM1/RecipeSelected
    3140006,  # PM1/RecipeSelectFailed
    3140007,  # PM1/ProcessingResultArrived
    # E90 substrate state transitions also fire from inside the tool and
    # don't carry a load port - route via JobTracker too.
    3220013,  # NeedsProcessing2InProcess (wafer_start)
    3220014,  # InProcess2ProcessingComplete (wafer_end)
    3220016,  # InProcess2Processed (wafer_end)
    3220017,  # InProcess2Aborted (wafer_end with abort)
    3220018,  # InProcess2Stopped
    3220019,  # InProcess2Rejected
    3220020,  # InProcess2Lost
    3220021,  # InProcess2Skipped
    3220022,  # NeedsProcessing2Lost
    3220023,  # NeedsProcessing2Skipped
})


DAVINCI_CEID_ALIASES: Dict[int, str] = {
    # Control / online state
    3010001: "Equipment OFF-LINE",
    3010002: "Control State LOCAL",
    3010003: "Control State REMOTE",
    3010004: "OperatorCommandIssued",
    # Alarms
    3020001: "AlarmNDetected",
    3020002: "AlarmNCleared",
    # Material movement
    3050001: "MaterialReceived",
    3050002: "MaterialRemoved",
    3080001: "LP1/ReceivedMaterial",
    3080002: "LP1/SentMaterial",
    3090001: "LP2/ReceivedMaterial",
    3090002: "LP2/SentMaterial",
    # Process Module 1
    3140002: "PM1/ProcessingStarted",
    3140003: "PM1/ProcessingFinished",
    3140004: "PM1/ProcessingAborted",
    3140005: "PM1/RecipeSelected",
    3140006: "PM1/RecipeSelectFailed",
    3140007: "PM1/ProcessingResultArrived",
    # Carrier / Load Port lifecycle
    3160001: "LP1/CarrierArrived",
    3160002: "LP1/CarrierDeparted",
    3160005: "LP1/LoadComplete",
    3160006: "LP1/UnloadComplete",
    3170001: "LP2/CarrierArrived",
    3170002: "LP2/CarrierDeparted",
    3170005: "LP2/LoadComplete",
    3170006: "LP2/UnloadComplete",
    # PRJob state (E94)
    3190044: "PRJobMS_Complete",
    3190045: "PRJobMS_Processing",
    3190046: "PRJobMS_ProcessingComplete",
    3190047: "PRJobMS_Setup",
    3190048: "PRJobStateChange",
    3190050: "PRJobMS_WaitingForStart",
    # ControlJob state (E94)
    3200001: "ControlJob:NoState-Queued",
    3200002: "ControlJob:Executing-Completed",
    3200003: "ControlJob:Active-Completed",
    3200008: "ControlJob:Active-Completed",
    3200013: "ControlJob:Completed-NoState",
    3200017: "ControlJob:Selected-Executing",
    # Carrier (E87)
    3210001: "CarrierApproachingComplete",
    3210002: "CarrierClamped",
    3210003: "CarrierClosed",
    3210006: "CarrierIDRead",
    3210007: "CarrierIDReadFail",
    3210009: "CarrierOpened",
    # Substrate (E90)
    3220013: "NeedsProcessing2InProcess",
    3220014: "InProcess2ProcessingComplete",
    3220016: "InProcess2Processed",
    3220017: "InProcess2Aborted",
    # The rest of the workbook's E90 substrate outcomes. Without these a
    # stopped, rejected, lost or skipped wafer produced no event at all - the
    # lot file simply had one fewer row than the cassette had wafers, with
    # nothing to say which wafer was missing or why. They are the same
    # transition family as 3220016/3220017 above, from the same sheet.
    3220018: "InProcess2Stopped",
    3220019: "InProcess2Rejected",
    3220020: "InProcess2Lost",
    3220021: "InProcess2Skipped",
    3220022: "NeedsProcessing2Lost",
    3220023: "NeedsProcessing2Skipped",
}


# Data Variables that come in event reports - documented here so the mapper
# can pull lot/wafer/recipe IDs out of S6F11 payloads keyed by these names.
DAVINCI_DVS: Dict[str, int] = {
    "OperationMode": 2010001,
    "OperatorCommand": 2010002,
    "AlarmID": 2020001,
    "AlarmCode": 2020002,
    "AlarmText": 2020003,
    "RecipeName": 2080001,
    "WaferID": 2080002,
    "LotID": 2080003,
    "ResultFile": 2080005,
    "ResultPath": 2080006,
    "AbortReason": 2080007,
    "SlotID": 2090012,
    "UnitFoupID": 2090013,
    "SubstrateID": 2090015,
    "CarrierTag": 2110001,
    "PRJobID": 2130001,
    "PRJobState": 2130002,
    "PRRecipeMethod": 2130004,
}
