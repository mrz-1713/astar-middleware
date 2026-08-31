"""NexGen MG canonical event alias table."""
from __future__ import annotations

from typing import Dict

from ...models import EventMapping
from ..base import event_mapping
from .ceids import NEXGEN_MG_CEID_ALIASES


def nexgen_mg_event_aliases() -> Dict[str, EventMapping]:
    """Map every MG collection event onto a canonical middleware event.

    Only the five per-port lifecycle families reach the per-lot CSV, matching
    the physical-material convention used by the other profiles: the file
    opens when a cassette is placed and closes only when it is physically
    removed, not when the lot logically ends. Per-wafer rows come from the
    process-module events, which carry the full identity block.

    GEM300 job/carrier/substrate transitions are subscribed and published as
    telemetry but map to control_state, so they never produce a second
    lot_start or an unattributed CSV row alongside the port-scoped lifecycle.
    """
    def state(csv: str, raw: str) -> EventMapping:
        """Observed and published, but never a per-lot CSV row."""
        return event_mapping("control_state", csv, raw)

    events: Dict[str, EventMapping] = {
        "EquipmentOffline": state("ControlState", "EquipmentOffline"),
        "ControlStateLocal": state("ControlState", "ControlStateLocal"),
        "ControlStateRemote": state("ControlState", "ControlStateRemote"),
        "OperatorCommandIssued": state("OperatorCommand", "OperatorCommandIssued"),
        "ProcessingStarted": event_mapping("process_start", "Proc_Start", "ProcessingStarted"),
        "ProcessingCompleted": event_mapping("process_end", "Proc_End", "ProcessingCompleted"),
        "ProcessingStopped": event_mapping("process_end", "Proc_End", "ProcessingStopped"),
        "ProcessingStateChange": state("ProcessStateChange", "ProcessingStateChange"),
        "AlarmDetected": event_mapping("alarm", "AlarmSet", "AlarmDetected"),
        "AlarmCleared": event_mapping("alarm", "AlarmCleared", "AlarmCleared"),
        "OperatorEquipmentConstantChange": state("ECChange", "OperatorEquipmentConstantChange"),
        "LimitZoneTransition": state("LimitZoneTransition", "LimitZoneTransition"),
        "ProcessProgramChange": state("PPChange", "ProcessProgramChange"),
        "processRecipeSelected": event_mapping("recipe_selected", "RecipeSelected", "processRecipeSelected"),
        "MaterialReceived": event_mapping("mounted", "Mounted", "MaterialReceived"),
        "MaterialRemoved": event_mapping("unmounted", "UnMounted", "MaterialRemoved"),
        "SpoolingActivated": state("SpoolingActivated", "SpoolingActivated"),
        "SpoolingDeactivated": state("SpoolingDeactivated", "SpoolingDeactivated"),
        "SpoolTransmitFailure": state("SpoolTransmitFailure", "SpoolTransmitFailure"),
        "MessageRecognition": state("MessageRecognition", "MessageRecognition"),
        "initCompleted": state("ProcessStateChange", "initCompleted"),
        "processStateSetup": state("ProcessStateChange", "processStateSetup"),
        "setupCompleted": state("ProcessStateChange", "setupCompleted"),
        "readyForProcess": state("ProcessStateChange", "readyForProcess"),
        "buzzerStateChanged": state("BuzzerState", "buzzerStateChanged"),
        "signalTowerStateChanged": state("SignalTowerState", "signalTowerStateChanged"),
        "cassetteMapped": event_mapping("mapped", "Mapped", "cassetteMapped"),
        "WaferAlignmentStatus": state("WaferAlignment", "WaferAlignmentStatus"),
    }
    for port in range(1, 5):
        events[f"port{port}ReadyToLoad"] = event_mapping(
            "ready_to_load", "Ready_Load", f"port{port}ReadyToLoad")
        events[f"port{port}ReadyToUnload"] = event_mapping(
            "lot_end", "Lot_End", f"port{port}ReadyToUnload")
        events[f"port{port}CasPlaced"] = event_mapping(
            "loaded", "Loaded", f"port{port}CasPlaced")
        # Physical cassette removal is the only thing that closes the file.
        events[f"port{port}CasRemoved"] = event_mapping(
            "unloaded", "Unloaded", f"port{port}CasRemoved", closes=True)
        events[f"port{port}CasMapped"] = event_mapping(
            "mapped", "Mapped", f"port{port}CasMapped")
        events[f"processingStartedPort{port}"] = event_mapping(
            "lot_start", "Lot_Start", f"processingStartedPort{port}")
    for pm in (1, 2):
        events[f"pm{pm}StatusChanged"] = state("PMState", f"pm{pm}StatusChanged")
        events[f"pm{pm}Occupied"] = state("PMOccupied", f"pm{pm}Occupied")
        events[f"pm{pm}Unoccupied"] = state("PMUnoccupied", f"pm{pm}Unoccupied")
        events[f"pm{pm}WaferStarted"] = event_mapping(
            "wafer_start", "Wfr_Start", f"pm{pm}WaferStarted")
        events[f"pm{pm}WaferFinished"] = event_mapping(
            "wafer_end", "Wfr_End", f"pm{pm}WaferFinished")
        events[f"pm{pm}ProcessStopping"] = state("ProcessStopping", f"pm{pm}ProcessStopping")
        events[f"pm{pm}ProcessAborting"] = state("ProcessAborting", f"pm{pm}ProcessAborting")
        events[f"pm{pm}ProcessAborted"] = event_mapping(
            "process_end", "Proc_End", f"pm{pm}ProcessAborted")
        for name in (
            "StepStarted", "MediumStepStarted", "DiStepStarted",
            "N2DryStepStarted", "DiwO3StepStarted", "MediumOffStepStarted",
        ):
            events[f"pm{pm}{name}"] = event_mapping(
                "recipe_step", "Recipe_Step_Start", f"pm{pm}{name}")
        for name in (
            "StepFinished", "MediumStepFinished", "DiStepFinished",
            "N2DryStepFinished", "DiwO3StepFinished", "MediumOffStepFinished",
        ):
            events[f"pm{pm}{name}"] = event_mapping(
                "recipe_step", "Recipe_Step_End", f"pm{pm}{name}")
        for module in ("Hpc", "Bem", "LowFlow", "Epd"):
            events[f"Pm{pm}{module}StepStarted"] = event_mapping(
                "recipe_step", "Recipe_Step_Start", f"Pm{pm}{module}StepStarted")
            events[f"Pm{pm}{module}StepFinished"] = event_mapping(
                "recipe_step", "Recipe_Step_End", f"Pm{pm}{module}StepFinished")
        events[f"Pm{pm}EpdEndpointDetected"] = state(
            "EndpointDetected", f"Pm{pm}EpdEndpointDetected")
        events[f"Atmsi{pm}MeasFinished"] = state(
            "MeasurementFinished", f"Atmsi{pm}MeasFinished")
        events[f"Roughness{pm}MeasFinished"] = state(
            "MeasurementFinished", f"Roughness{pm}MeasFinished")
    # Everything remaining (chemistry cabinets, media limits, GEM300 job /
    # carrier / load-port / substrate transitions) is observed, published and
    # never acted on. Named from the manual so the raw event stays readable.
    for alias in NEXGEN_MG_CEID_ALIASES.values():
        events.setdefault(alias, state(alias, alias))
    return events
