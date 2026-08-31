"""NexGen MG collection event identifiers and their attribution tables."""
from __future__ import annotations

from typing import Dict, FrozenSet, Sequence

from ..base import (
    TRANSITION_LP_ACTIVATE_FROM_PAYLOAD,
    TRANSITION_LP_DEACTIVATE_FROM_PAYLOAD,
)
from .reports import NEXGEN_MG_REPORTS


NEXGEN_MG_CEID_DV_LAYOUT: Dict[int, Sequence[str]] = {
    ceid: tuple(name for name, _vid in slots)
    for ceid, slots in NEXGEN_MG_REPORTS.items()
}


# Load port is encoded in the CEID for the per-port lifecycle families. Process
# module events do NOT appear here - they carry their own PortID in the payload.
NEXGEN_MG_CEID_LOAD_PORT: Dict[int, str] = {
    base + port: str(port)
    for base in (119, 123, 129, 133, 139, 149)
    for port in range(1, 5)
}

# Process-module attribution for the CSV Chamber column.
NEXGEN_MG_CEID_CHAMBER: Dict[int, str] = {
    **{ceid: "PM1" for ceid in
       list(range(200, 232)) + [514, 515, 518, 519, 522, 523, 533, 534, 535]},
    **{ceid: "PM2" for ceid in
       list(range(300, 332)) + [516, 517, 520, 521, 524, 525, 536, 537, 538]},
}


# Process-module events that name a chamber but no load port. The tool states
# the pairing on the wafer-level reports (pmNWaferStarted/Finished,
# pmNStepFinished carry pmNCurrWaferLoadPort); every other event in the same
# chamber band carries either no report at all or one without a port, so it is
# resolved against the chamber binding those reports leave behind.
NEXGEN_MG_CHAMBER_EVENT_CEIDS: FrozenSet[int] = frozenset(
    ceid
    for ceid in NEXGEN_MG_CEID_CHAMBER
    if "PortID" not in NEXGEN_MG_CEID_DV_LAYOUT.get(ceid, ())
)

# Carrier lifecycle. Each of these names its port in the CEID itself (the
# profile's ceid_load_port map), which the mapper resolves before the tracker
# sees the event - so the payload-driven transition reads the resolved port and
# works for all four load ports without a per-port transition tag.
NEXGEN_MG_CEID_STATE_TRANSITIONS: Dict[int, str] = {
    **{ceid: TRANSITION_LP_ACTIVATE_FROM_PAYLOAD for ceid in (130, 131, 132, 133)},
    **{ceid: TRANSITION_LP_DEACTIVATE_FROM_PAYLOAD for ceid in (134, 135, 136, 137)},
}


def _mg_ceid_aliases() -> Dict[int, str]:
    aliases: Dict[int, str] = {
        0: "EquipmentOffline",
        1: "ControlStateLocal",
        2: "ControlStateRemote",
        3: "OperatorCommandIssued",
        4: "ProcessingStarted",
        5: "ProcessingCompleted",
        6: "ProcessingStopped",
        7: "ProcessingStateChange",
        8: "AlarmDetected",
        9: "AlarmCleared",
        10: "OperatorEquipmentConstantChange",
        11: "LimitZoneTransition",
        12: "ProcessProgramChange",
        13: "processRecipeSelected",
        14: "MaterialReceived",
        15: "MaterialRemoved",
        16: "SpoolingActivated",
        17: "SpoolingDeactivated",
        18: "SpoolTransmitFailure",
        19: "MessageRecognition",
        100: "initCompleted",
        101: "processStateSetup",
        102: "setupCompleted",
        103: "readyForProcess",
        110: "buzzerStateChanged",
        111: "signalTowerStateChanged",
        145: "cassetteMapped",
        510: "Atmsi1MeasFinished",
        511: "Atmsi2MeasFinished",
        531: "Roughness1MeasFinished",
        532: "Roughness2MeasFinished",
        600: "WaferAlignmentStatus",
    }
    for port in range(1, 5):
        aliases[119 + port] = f"port{port}ReadyToLoad"
        aliases[123 + port] = f"port{port}ReadyToUnload"
        aliases[129 + port] = f"port{port}CasPlaced"
        aliases[133 + port] = f"port{port}CasRemoved"
        aliases[139 + port] = f"port{port}CasMapped"
        aliases[149 + port] = f"processingStartedPort{port}"
    for pm, base in ((1, 200), (2, 300)):
        aliases[base] = f"pm{pm}StatusChanged"
        for offset, name in (
            (10, "Occupied"), (11, "Unoccupied"), (12, "WaferStarted"),
            (13, "WaferFinished"), (14, "ProcessStopping"),
            (15, "ProcessAborting"), (16, "ProcessAborted"),
            (20, "StepStarted"), (21, "StepFinished"),
            (22, "MediumStepStarted"), (23, "MediumStepFinished"),
            (24, "DiStepStarted"), (25, "DiStepFinished"),
            (26, "N2DryStepStarted"), (27, "N2DryStepFinished"),
            (28, "DiwO3StepStarted"), (29, "DiwO3StepFinished"),
            (30, "MediumOffStepStarted"), (31, "MediumOffStepFinished"),
        ):
            aliases[base + offset] = f"pm{pm}{name}"
        for start, name in ((514, "Hpc"), (518, "Bem"), (522, "LowFlow")):
            aliases[start + (pm - 1) * 2] = f"Pm{pm}{name}StepStarted"
            aliases[start + 1 + (pm - 1) * 2] = f"Pm{pm}{name}StepFinished"
        # The endpoint-detection family breaks the +2 stride: PM1 owns 533-535
        # and PM2 owns 536-538, each with a third "EndpointDetected" CEID.
        epd = 533 + (pm - 1) * 3
        aliases[epd] = f"Pm{pm}EpdStepStarted"
        aliases[epd + 1] = f"Pm{pm}EpdStepFinished"
        aliases[epd + 2] = f"Pm{pm}EpdEndpointDetected"
    # Chemistry cabinets / media
    for cabinet in (1, 2, 3):
        aliases[399 + cabinet] = f"chc{cabinet}StateChanged"
        aliases[409 + cabinet] = f"chc{cabinet}Ready"
        aliases[414 + cabinet] = f"med{cabinet}NotReady"
        aliases[418 + cabinet * 2] = f"med{cabinet}RefillStarted"
        aliases[419 + cabinet * 2] = f"med{cabinet}RefillFinished"
    for cabinet in (1, 2, 3, 4):
        aliases[879 + cabinet] = f"chc{cabinet}CanisterFillPaused"
    for medium in (1, 2, 3):
        for component in (1, 2, 3, 4, 5):
            index = (medium - 1) * 5 + (component - 1)
            aliases[450 + index] = f"med{medium}Comp{component}ConcHighLimit"
            aliases[465 + index] = f"med{medium}Comp{component}ConcLowLimit"
    # GEM300 state-transition families (manual 8.1). Names are taken verbatim.
    for ceid, name in (
        (700, "PjStateChanged"), (701, "PjNoStateToQueued"),
        (702, "PjQueuedToSettingUp"), (703, "PjSettingUpToWaitingForStart"),
        (704, "PjSettingUpToProcessing"), (705, "PjWaitingForStartToProcessing"),
        (706, "PjProcessingToProcessComplete"), (707, "PjPostActiveToNoState"),
        (708, "PjExecutingToPausing"), (709, "PjPausingToPaused"),
        (710, "PjPauseToExecuting"), (711, "PjExecutingToStopping"),
        (712, "PjPauseToStopping"), (713, "PjExecutingToAborting"),
        (714, "PjStoppingToAborting"), (715, "PjPauseToAborting"),
        (716, "PjAbortingToAborted"), (717, "PjStoppingToStopped"),
        (718, "PjQueuedToNoState"),
        (720, "CjStateChanged"), (721, "CjNoStateToQueued"),
        (722, "CjQueuedToNoState"), (723, "CjQueuedToSelected"),
        (724, "CjSelectedToQueued"), (725, "CjSelectedToExecuting"),
        (726, "CjSelectedToWaitingForStart"),
        (727, "CjWaitingForStartToExecuting"), (728, "CjExecutingToPaused"),
        (729, "CjPausedToExecuting"), (730, "CjExecutingToCompleted"),
        (731, "CjAbortedToCompleted"), (732, "CjStoppedToCompleted"),
        (733, "CjCompletedToNoState"),
        (751, "LpNoStateToServiceState"), (752, "LpOutOfServiceToInService"),
        (753, "LpInServiceToOutOfService"), (754, "LpInServiceToTransferState"),
        (755, "LpTransferReadyToReadyState"),
        (756, "LpReadyToLoadToTransferBlocked"),
        (757, "LpReadyToUnloadToTransferBlocked"),
        (758, "LpTransferBlockedToReadyToLoad"),
        (759, "LpTransferBlockedToReadyToUnload"),
        (760, "LpTransferBlockedToTransferReady"),
        (772, "CarrierIdNoStateToNotRead"),
        (773, "CarrierIdNoStateToWaitingForHost"),
        (774, "CarrierIdNoStateToVerificationOk"),
        (775, "CarrierIdNoStateToVerificationFail"),
        (776, "CarrierIdNotReadToVerificationOk"),
        (777, "CarrierIdNotReadToWaitingForHost"),
        (778, "CarrierIdWaitingForHostToVerificationOk"),
        (779, "CarrierIdWaitingForHostToVerificationFail"),
        (783, "CarrierSlotMapNotReadToVerificationOk"),
        (784, "CarrierSlotMapNotReadToWaitingForHost"),
        (785, "CarrierSlotMapWaitingForHostToVerificationOk"),
        (786, "CarrierSlotMapWaitingForHostToVerificationFail"),
        (787, "CarrierNotAccessedToInAccess"),
        (788, "CarrierInAccessToCarrierComplete"),
        (789, "CarrierInAccessToCarrierStopped"),
        (790, "CarrierToNoState"),
        (801, "LpAccessModeNoStateToManualOrAuto"),
        (802, "LpAccessModeManualToAuto"), (803, "LpAccessModeAutoToManual"),
        (804, "LpNotReservedToReserved"), (805, "LpReservedToNotReserved"),
        (806, "LpNotAssociatedToAssociated"),
        (807, "LpAssociatedToNotAssociated"), (808, "LpAssociatedToAssociated"),
        (850, "SubstNoStateToAtSource"), (851, "SubstAtSourceToAtWork"),
        (852, "SubstAtWorkToAtSource"), (853, "SubstAtWorkToAtWork"),
        (854, "SubstAtWorkToAtDestination"),
        (855, "SubstAtDestinationToNoState"),
        (856, "SubstNoStateToNeedsProcessing"),
        (857, "SubstNeedsProcessingToInProcess"),
        (858, "SubstInProcessToComplete"),
        (859, "SubstNeedsProcessingToComplete"),
        (860, "SubstLocUnoccupiedToOccupied"),
        (861, "SubstLocOccupiedToUnoccupied"), (862, "SubstToNoState"),
    ):
        aliases[ceid] = name
    return aliases


NEXGEN_MG_CEID_ALIASES: Dict[int, str] = _mg_ceid_aliases()
