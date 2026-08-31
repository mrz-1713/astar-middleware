"""The profile registry and the built-in profile definitions.

Imports every vendor table module, so it must stay the leaf of the package's
import graph.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .base import MachineProfile, alias_table, event_mapping
from .davinci import (
    DAVINCI_CEID_ALIASES,
    DAVINCI_CEID_DV_LAYOUT,
    DAVINCI_CEID_LOAD_PORT,
    DAVINCI_CEID_STATE_TRANSITIONS,
    DAVINCI_CHAMBER_EVENT_CEIDS,
    DAVINCI_DVS,
    DAVINCI_SVIDS,
)
from .nexgen_mg.ceids import (
    NEXGEN_MG_CEID_ALIASES,
    NEXGEN_MG_CEID_CHAMBER,
    NEXGEN_MG_CEID_DV_LAYOUT,
    NEXGEN_MG_CEID_LOAD_PORT,
    NEXGEN_MG_CEID_STATE_TRANSITIONS,
    NEXGEN_MG_CHAMBER_EVENT_CEIDS,
)
from .nexgen_mg.events import nexgen_mg_event_aliases
from .nexgen_mg.reports import NEXGEN_MG_DVS
from .nexgen_mg.variables import NEXGEN_MG_SVIDS
from .ptiq import PTIQ_DVS, PTIQ_SVIDS
from .spts import (
    SPTS_CEID_ALIASES,
    SPTS_CEID_DV_LAYOUT,
    SPTS_CEID_LOAD_PORT,
    SPTS_CEID_STATE_TRANSITIONS,
    SPTS_CHAMBER_EVENT_CEIDS,
    SPTS_DVS,
    SPTS_SVIDS,
)


class ProfileRegistry:
    """Registry for built-in and future machine profiles."""

    def __init__(self, profiles: Optional[Iterable[MachineProfile]] = None):
        self._profiles: Dict[str, MachineProfile] = {}
        for profile in profiles or built_in_profiles():
            self.register(profile)

    def register(self, profile: MachineProfile) -> None:
        self._profiles[profile.profile_id] = profile

    def get(self, profile_id: str) -> MachineProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            known = ", ".join(sorted(self._profiles))
            raise KeyError(f"Unknown machine_profile '{profile_id}'. Known: {known}") from exc

    def has(self, profile_id: str) -> bool:
        return profile_id in self._profiles

    def list_profile_ids(self) -> List[str]:
        return sorted(self._profiles)


def built_in_profiles() -> List[MachineProfile]:
    spts_lot_start = event_mapping("lot_start", "Lot_Start", "LotStarted")
    # closes_lot_file is reserved for carrier/material removal - lot_end alone
    # is not the end of the per-lot CSV file because wafers are still unloaded
    # after Lot_End fires.
    spts_lot_end = event_mapping("lot_end", "Lot_End", "LotEnded")
    spts_wfr_start = event_mapping("wafer_start", "Wfr_Start", "WaferStarted")
    spts_wfr_end = event_mapping("wafer_end", "Wfr_End", "WaferComplete")
    spts_proc_start = event_mapping("process_start", "Proc_Start", "ProcessingStarted")
    spts_proc_end = event_mapping("process_end", "Proc_End", "ProcessingFinished")
    spts_loaded = event_mapping("loaded", "Loaded", "SMIFPodPresent")
    spts_unloaded = event_mapping("unloaded", "Unloaded", "SMIFPodAbsent", closes=True)
    spts_clamped = event_mapping("clamped", "Clamped", "SMIFPodClamped")
    spts_unclamped = event_mapping("unclamped", "UnClamped", "SMIFPodUnClamped")
    spts_mounted = event_mapping("mounted", "Mounted", "MaterialReceived")
    spts_unmounted = event_mapping("unmounted", "UnMounted", "MaterialRemoved")

    spts_events = alias_table(
        {
            # Generic canonical aliases for upstream-renamed events
            "ReadyToLoad": event_mapping("ready_to_load", "Ready_Load", "ReadyToLoad"),
            "ReadyToUnload": event_mapping("ready_to_unload", "Ready_Unload", "ReadyToUnload"),
            "Loaded": spts_loaded,
            "Unloaded": spts_unloaded,
            "Clamped": spts_clamped,
            "UnClamped": spts_unclamped,
            "Unclamped": spts_unclamped,
            "Mounted": spts_mounted,
            "UnMounted": spts_unmounted,
            "Unmounted": spts_unmounted,
            "Lot_Start": spts_lot_start,
            "Lot_End": spts_lot_end,
            "Wfr_Start": spts_wfr_start,
            "Wfr_End": spts_wfr_end,
            "Proc_Start": spts_proc_start,
            "Proc_End": spts_proc_end,
            # Real CEID keywords from manual section 7
            "MaterialReceived": spts_mounted,
            "MaterialRemoved": spts_unmounted,
            "OperatorCommandIssued": event_mapping("control_state", "OperatorCommand", "OperatorCommandIssued"),
            "PPChange": event_mapping("control_state", "PPChange", "PPChange"),
            "EquipmentOffline": event_mapping("control_state", "ControlState", "EquipmentOffline"),
            "ControlStateLocal": event_mapping("control_state", "ControlState", "ControlStateLocal"),
            "ControlStateRemote": event_mapping("control_state", "ControlState", "ControlStateRemote"),
            "HostECChange": event_mapping("control_state", "HostECChange", "HostECChange"),
            "HostPPChange": event_mapping("control_state", "HostPPChange", "HostPPChange"),
            "ECChange": event_mapping("control_state", "ECChange", "ECChange"),
            # Per-cassette process state (manual section 15.2, Table 11:
            # IDLE / SELECTING / SELECTED / STARTING / RUNNING / STOPPING /
            # RESTARTING / ABANDONING / STOPPED). Reported as a state change
            # rather than a lifecycle event: the lot file's start and end come
            # from MBCStart/MBCComplete, and duplicating them here would open
            # or close a lot file twice.
            "ProcessStateChange1": event_mapping(
                "control_state", "ProcessState", "ProcessStateChange1"
            ),
            "ProcessStateChange2": event_mapping(
                "control_state", "ProcessState", "ProcessStateChange2"
            ),
            # Cassette / Lot lifecycle
            "MBCStart1": spts_lot_start,
            "MBCStart2": spts_lot_start,
            "MBCComplete1": spts_lot_end,
            "MBCComplete2": spts_lot_end,
            "CassetteStarted": spts_lot_start,
            "CassetteComplete": spts_lot_end,
            "ReadyForProcessA": event_mapping("ready_to_load", "Ready_Load", "ReadyForProcessA"),
            "ReadyForProcessB": event_mapping("ready_to_load", "Ready_Load", "ReadyForProcessB"),
            # Wafer lifecycle
            "MBStart1": spts_wfr_start,
            "MBStart2": spts_wfr_start,
            "MBComplete1": spts_wfr_end,
            "MBComplete2": spts_wfr_end,
            "WaferStarted": spts_wfr_start,
            "WaferComplete": spts_wfr_end,
            "PMWaferIn": spts_wfr_start,
            "PMWaferOut": spts_wfr_end,
            **{f"PM{i + 1}WaferIn": spts_wfr_start for i in range(6)},
            **{f"PM{i + 1}WaferOut": spts_wfr_end for i in range(6)},
            # Process / recipe events
            "ProcessingStarted": spts_proc_start,
            "ProcessingFinished": spts_proc_end,
            "RecipeStepStart": event_mapping("recipe_step", "Recipe_Step_Start", "RecipeStepStart"),
            "RecipeStepEnd": event_mapping("recipe_step", "Recipe_Step_End", "RecipeStepEnd"),
            **{f"PM{i + 1}RecipeStart": spts_proc_start for i in range(6)},
            **{f"PM{i + 1}RecipeEnd": spts_proc_end for i in range(6)},
            "CoolerRecipeStart": spts_proc_start,
            "CoolerRecipeEnd": spts_proc_end,
            # Operator stop / cancel - lot_end with original raw event preserved
            "OpStop1": event_mapping("lot_end", "Lot_End", "OpStop1"),
            "OpStop2": event_mapping("lot_end", "Lot_End", "OpStop2"),
            "OpCancel1": event_mapping("lot_end", "Lot_End", "OpCancel1"),
            "OpCancel2": event_mapping("lot_end", "Lot_End", "OpCancel2"),
            "OpAbandon1": event_mapping("lot_end", "Lot_End", "OpAbandon1"),
            "OpAbandon2": event_mapping("lot_end", "Lot_End", "OpAbandon2"),
            # SMIF pod / Load Port - keep per-side identity in the raw event
            "SMIFPodPresent1": event_mapping("loaded", "Loaded", "SMIFPodPresent1"),
            "SMIFPodPresent2": event_mapping("loaded", "Loaded", "SMIFPodPresent2"),
            "SMIFPodAbsent1": event_mapping("unloaded", "Unloaded", "SMIFPodAbsent1", closes=True),
            "SMIFPodAbsent2": event_mapping("unloaded", "Unloaded", "SMIFPodAbsent2", closes=True),
            "SMIFPodClamped1": event_mapping("clamped", "Clamped", "SMIFPodClamped1"),
            "SMIFPodClamped2": event_mapping("clamped", "Clamped", "SMIFPodClamped2"),
            "SMIFPodUnClamped1": event_mapping("unclamped", "UnClamped", "SMIFPodUnClamped1"),
            "SMIFPodUnClamped2": event_mapping("unclamped", "UnClamped", "SMIFPodUnClamped2"),
            "VCEALoadComplete": event_mapping("loaded", "Loaded", "VCEALoadComplete"),
            "VCEBLoadComplete": event_mapping("loaded", "Loaded", "VCEBLoadComplete"),
            "VCEAUnloadComplete": event_mapping("unloaded", "Unloaded", "VCEAUnloadComplete", closes=True),
            "VCEBUnloadComplete": event_mapping("unloaded", "Unloaded", "VCEBUnloadComplete", closes=True),
            "VCEAMaterialPresent": event_mapping("mounted", "Mounted", "VCEAMaterialPresent"),
            "VCEBMaterialPresent": event_mapping("mounted", "Mounted", "VCEBMaterialPresent"),
            "VCEAMaterialAbsent": event_mapping("unmounted", "UnMounted", "VCEAMaterialAbsent"),
            "VCEBMaterialAbsent": event_mapping("unmounted", "UnMounted", "VCEBMaterialAbsent"),
            # Convenience canonicals
            "LotStarted": spts_lot_start,
            "LotEnded": spts_lot_end,
        }
    )

    davinci_events = alias_table(
        {
            # Online / control state (alias names match the Excel "Events" sheet)
            "Equipment OFF-LINE": event_mapping("control_state", "ControlState", "Equipment_OFFLINE"),
            "Control State LOCAL": event_mapping("control_state", "ControlState", "Equipment_ONLINE_Local"),
            "Control State REMOTE": event_mapping("control_state", "ControlState", "Equipment_ONLINE_Remote"),
            "OperatorCommandIssued": event_mapping("control_state", "OperatorCommand", "OperatorCommandIssued"),
            # Alarms
            "AlarmNDetected": event_mapping("alarm", "AlarmSet", "AlarmDetected"),
            "AlarmNCleared": event_mapping("alarm", "AlarmCleared", "AlarmCleared"),
            # Material movement
            "MaterialReceived": event_mapping("mounted", "Mounted", "MaterialReceived"),
            "MaterialRemoved": event_mapping("unmounted", "UnMounted", "MaterialRemoved"),
            "LP1/ReceivedMaterial": event_mapping("mounted", "Mounted", "MaterialReceived"),
            "LP1/SentMaterial": event_mapping("unmounted", "UnMounted", "MaterialRemoved"),
            "LP2/ReceivedMaterial": event_mapping("mounted", "Mounted", "MaterialReceived"),
            "LP2/SentMaterial": event_mapping("unmounted", "UnMounted", "MaterialRemoved"),
            # Process module (per-recipe-run signals)
            "PM1/ProcessingStarted": event_mapping("process_start", "Proc_Start", "ProcessingStarted"),
            "PM1/ProcessingFinished": event_mapping("process_end", "Proc_End", "ProcessingFinished"),
            "PM1/ProcessingAborted": event_mapping("process_end", "Proc_End", "ProcessingAborted"),
            "PM1/ProcessingResultArrived": event_mapping("process_end", "Proc_End", "ProcessingResultArrived"),
            "PM1/RecipeSelected": event_mapping("recipe_selected", "RecipeSelected", "RecipeSelected"),
            "PM1/RecipeSelectFailed": event_mapping("recipe_selected", "RecipeSelectFailed", "RecipeSelectFailed"),
            "ProcessingStarted": event_mapping("process_start", "Proc_Start", "ProcessingStarted"),
            "ProcessingFinished": event_mapping("process_end", "Proc_End", "ProcessingFinished"),
            "ProcessingAborted": event_mapping("process_end", "Proc_End", "ProcessingAborted"),
            # Carrier / Load Port lifecycle
            "LP1/CarrierArrived": event_mapping("loaded", "Loaded", "CarrierArrived"),
            "LP1/CarrierDeparted": event_mapping("unloaded", "Unloaded", "CarrierDeparted", closes=True),
            "LP1/LoadComplete": event_mapping("loaded", "Loaded", "LoadComplete"),
            "LP1/UnloadComplete": event_mapping("unloaded", "Unloaded", "UnloadComplete", closes=True),
            "LP2/CarrierArrived": event_mapping("loaded", "Loaded", "CarrierArrived"),
            "LP2/CarrierDeparted": event_mapping("unloaded", "Unloaded", "CarrierDeparted", closes=True),
            "LP2/LoadComplete": event_mapping("loaded", "Loaded", "LoadComplete"),
            "LP2/UnloadComplete": event_mapping("unloaded", "Unloaded", "UnloadComplete", closes=True),
            "CarrierApproachingComplete": event_mapping("loaded", "Loaded", "CarrierApproachingComplete"),
            "CarrierClamped": event_mapping("clamped", "Clamped", "CarrierClamped"),
            "CarrierClosed": event_mapping("clamped", "Clamped", "CarrierClosed"),
            "CarrierOpened": event_mapping("unclamped", "UnClamped", "CarrierOpened"),
            "CarrierIDRead": event_mapping("loaded", "Loaded", "CarrierIDRead"),
            "CarrierIDReadFail": event_mapping("control_state", "CarrierIDReadFail", "CarrierIDReadFail"),
            "CarrierArrived": event_mapping("loaded", "Loaded", "CarrierArrived"),
            "CarrierRemoved": event_mapping("unloaded", "Unloaded", "CarrierRemoved", closes=True),
            # E94 PRJob / ControlJob
            "PRJobMS_Setup": event_mapping("control_state", "PRJobSetup", "PRJobMS_Setup"),
            "PRJobMS_WaitingForStart": event_mapping("control_state", "PRJobWaiting", "PRJobMS_WaitingForStart"),
            "PRJobMS_Processing": event_mapping("process_start", "Proc_Start", "PRJobMS_Processing"),
            "PRJobMS_ProcessingComplete": event_mapping("process_end", "Proc_End", "PRJobMS_ProcessingComplete"),
            # Lot/job completion does NOT close the per-lot CSV file - the
            # wafers are still being unloaded after PRJob/ControlJob complete.
            # Only LP carrier departure closes the file.
            "PRJobMS_Complete": event_mapping("lot_end", "Lot_End", "PRJobMS_Complete"),
            "PRJobStateChange": event_mapping("control_state", "PRJobStateChange", "PRJobStateChange"),
            "ControlJob:Selected-Executing": event_mapping("lot_start", "Lot_Start", "LotStarted"),
            "ControlJob:Executing-Completed": event_mapping("lot_end", "Lot_End", "LotEnded"),
            "ControlJob:Active-Completed": event_mapping("lot_end", "Lot_End", "LotEnded"),
            "ControlJob:Completed-NoState": event_mapping("lot_end", "Lot_End", "LotEnded"),
            "ControlJob:NoState-Queued": event_mapping("control_state", "ControlJobQueued", "ControlJobQueued"),
            # E90 Substrate
            "NeedsProcessing2InProcess": event_mapping("wafer_start", "Wfr_Start", "WaferStarted"),
            "InProcess2ProcessingComplete": event_mapping("wafer_end", "Wfr_End", "WaferComplete"),
            "InProcess2Processed": event_mapping("wafer_end", "Wfr_End", "WaferComplete"),
            "InProcess2Aborted": event_mapping("wafer_end", "Wfr_End", "WaferAborted"),
            # Every documented way a wafer can leave the flow. All are
            # wafer_end - the wafer is finished with, one way or another - and
            # the outcome rides in the raw event name so a lot file shows WHY
            # a slot has no processed result. Workbook sheet "Events",
            # material states per TC User Documentation Table 33.
            "InProcess2Stopped": event_mapping("wafer_end", "Wfr_End", "WaferStopped"),
            "InProcess2Rejected": event_mapping("wafer_end", "Wfr_End", "WaferRejected"),
            "InProcess2Lost": event_mapping("wafer_end", "Wfr_End", "WaferLost"),
            "InProcess2Skipped": event_mapping("wafer_end", "Wfr_End", "WaferSkipped"),
            "NeedsProcessing2Lost": event_mapping("wafer_end", "Wfr_End", "WaferLost"),
            "NeedsProcessing2Skipped": event_mapping(
                "wafer_end", "Wfr_End", "WaferSkipped"
            ),
            # Convenience aliases for vendors that send canonical strings
            "LotStarted": event_mapping("lot_start", "Lot_Start", "LotStarted"),
            "LotEnded": event_mapping("lot_end", "Lot_End", "LotEnded"),
        }
    )

    ptiq_lot_start = event_mapping("lot_start", "Lot_Start", "LotStarted")
    ptiq_lot_end = event_mapping("lot_end", "Lot_End", "LotEnded")
    ptiq_proc_start = event_mapping("process_start", "Proc_Start", "ProcessingStarted")
    ptiq_proc_end = event_mapping("process_end", "Proc_End", "ProcessingFinished")
    ptiq_mounted = event_mapping("mounted", "Mounted", "MaterialReceived")
    ptiq_unmounted = event_mapping("unmounted", "UnMounted", "MaterialRemoved")
    ptiq_loaded = event_mapping("loaded", "Loaded", "CarrierArrived")
    ptiq_unloaded = event_mapping("unloaded", "Unloaded", "CarrierRemoved", closes=True)

    ptiq_events = alias_table(
        {
            # Control state (PTIQ spec section 2.2.4)
            "EquipmentOFFLINE": event_mapping("control_state", "ControlState", "EquipmentOFFLINE"),
            "EquipmentOffline": event_mapping("control_state", "ControlState", "EquipmentOFFLINE"),
            "ControlStateLOCAL": event_mapping("control_state", "ControlState", "ControlStateLOCAL"),
            "ControlStateLocal": event_mapping("control_state", "ControlState", "ControlStateLOCAL"),
            "ControlStateREMOTE": event_mapping("control_state", "ControlState", "ControlStateREMOTE"),
            "ControlStateRemote": event_mapping("control_state", "ControlState", "ControlStateREMOTE"),
            "OperatorCommandIssued": event_mapping("control_state", "OperatorCommand", "OperatorCommandIssued"),
            # Equipment processing state (section 2.4.3)
            "ProcessingStarted": ptiq_proc_start,
            "ProcessingStopped": event_mapping("process_end", "Proc_End", "ProcessingStopped"),
            "ProcessingCompleted": ptiq_proc_end,
            "ProcessingFinished": ptiq_proc_end,
            "ProcessStateChange": event_mapping("control_state", "ProcessStateChange", "ProcessStateChange"),
            # Scheduler state machine events (section 2.5.3 - SCHn.* family)
            "SCH1.LotStarted": ptiq_lot_start,
            "SCH2.LotStarted": ptiq_lot_start,
            "SCH3.LotStarted": ptiq_lot_start,
            "SCH4.LotStarted": ptiq_lot_start,
            "SCH1.LotComplete": ptiq_lot_end,
            "SCH2.LotComplete": ptiq_lot_end,
            "SCH3.LotComplete": ptiq_lot_end,
            "SCH4.LotComplete": ptiq_lot_end,
            "SCH1.WaitingForStart-Processing": ptiq_lot_start,
            "SCH2.WaitingForStart-Processing": ptiq_lot_start,
            "SCH3.WaitingForStart-Processing": ptiq_lot_start,
            "SCH4.WaitingForStart-Processing": ptiq_lot_start,
            "SCH1.Processing-Processed": event_mapping("wafer_end", "Wfr_End", "ProcessingProcessed"),
            "SCH2.Processing-Processed": event_mapping("wafer_end", "Wfr_End", "ProcessingProcessed"),
            "SCH1.Processed-Unloaded": ptiq_unloaded,
            "SCH2.Processed-Unloaded": ptiq_unloaded,
            "SCH1.WaitingForUnload-Unloaded": ptiq_unloaded,
            "SCH2.WaitingForUnload-Unloaded": ptiq_unloaded,
            # Material movement (section 2.6.1) - spec uses "MaterialRecieved" [sic]
            "MaterialReceived": ptiq_mounted,
            "MaterialRecieved": ptiq_mounted,
            "MaterialRemoved": ptiq_unmounted,
            "TransferIn": event_mapping("loaded", "Loaded", "TransferIn"),
            "TransferOut": event_mapping("unloaded", "Unloaded", "TransferOut", closes=True),
            # Carrier (vendor-implementations commonly add these)
            "CarrierArrived": ptiq_loaded,
            "CarrierRemoved": ptiq_unloaded,
            "CarrierDeparted": ptiq_unloaded,
            # Convenience canonicals
            "LotStarted": ptiq_lot_start,
            "LotEnded": ptiq_lot_end,
            "LotComplete": ptiq_lot_end,
            "WaferStarted": event_mapping("wafer_start", "Wfr_Start", "WaferStarted"),
            "WaferComplete": event_mapping("wafer_end", "Wfr_End", "WaferComplete"),
            # Process program changes
            "ProcessProgramChange": event_mapping("control_state", "PPChange", "ProcessProgramChange"),
            "ProcessProgramVerificationFailed": event_mapping(
                "control_state",
                "PPVerifyFail",
                "ProcessProgramVerificationFailed",
            ),
        }
    )

    mg_events = alias_table(nexgen_mg_event_aliases())

    return [
        MachineProfile(
            profile_id="spts_fxp_omega",
            vendor="SPTS",
            model="fxP Omega 200mm",
            default_port=5000,
            default_secs_device_id=0,
            event_aliases=spts_events,
            ceid_aliases=SPTS_CEID_ALIASES,
            svids_by_name=SPTS_SVIDS,
            identity_svid_names=["MDLN", "SOFTREV", "ControlState", "LastCEID", "Clock"],
            event_subscription_path=str(
                Path("output") / "spts_fxp_omega" / "EventSubscription.json"
            ),
            ceid_dv_layout=SPTS_CEID_DV_LAYOUT,
            ceid_load_port=SPTS_CEID_LOAD_PORT,
            chamber_event_ceids=SPTS_CHAMBER_EVENT_CEIDS,
            ceid_state_transitions=SPTS_CEID_STATE_TRANSITIONS,
            dvs_by_name=SPTS_DVS,
            # Manual sections 12.4/12.8: LastCEID (34) advances on every
            # internal collection event, EventsEnabled (30) corroborates, and
            # SpoolCountActual (2016) exposes the spool backlog. Wired here so
            # SPTS gets the same acked-but-silent watchdog as DaVinci/MG.
            health_last_event_svid=SPTS_SVIDS["LastCEID"],
            health_events_enabled_svid=SPTS_SVIDS["EventsEnabled"],
            health_spool_count_svid=SPTS_SVIDS["SpoolCountActual"],
            # Omega manual Table 3 "Protocol Parameters" (section 4.4): the
            # tool's own typical values. Every one of them differs from the
            # DaVinci figures this host used to apply to all four profiles -
            # notably T6 (tool 10s vs 5s) and T7 (tool 5s vs 10s), where the
            # shorter side gives up first and the link drops for no visible
            # reason. Connect Mode is documented as Passive on the tool, which
            # is why the middleware dials out (hsms_mode: active) by default.
            hsms_timers={"t3": 30, "t5": 5, "t6": 10, "t7": 5, "t8": 6},
            notes="Derived from Omega SECSII SPTS fxP 200mm SECSII Manual sections 7 (CEIDs), 12.4/12.8 (SVIDs), 12.10 (DVs); protocol timers from section 4.4 Table 3.",
        ),
        MachineProfile(
            profile_id="davinci_200_mc4_hc1",
            vendor="MueTec",
            model="DaVinci 200 MC4 HC1",
            default_port=5000,
            default_secs_device_id=0,
            event_aliases=davinci_events,
            ceid_aliases=DAVINCI_CEID_ALIASES,
            svids_by_name=DAVINCI_SVIDS,
            dvs_by_name=DAVINCI_DVS,
            identity_svid_names=["ControlState", "ProcessState", "PM1/RecipeName"],
            event_subscription_path=str(Path("output") / "davinci200_mc4_hc1" / "EventSubscription.json"),
            ceid_dv_layout=DAVINCI_CEID_DV_LAYOUT,
            ceid_load_port=DAVINCI_CEID_LOAD_PORT,
            chamber_event_ceids=DAVINCI_CHAMBER_EVENT_CEIDS,
            ceid_state_transitions=DAVINCI_CEID_STATE_TRANSITIONS,
            # LastEventID (1010004) advances on every internal collection event;
            # EventsEnabled (1010003) corroborates. Used to detect an
            # acked-but-silent subscription (E40 event style / spooling).
            health_last_event_svid=DAVINCI_SVIDS["LastEventID"],
            health_events_enabled_svid=DAVINCI_SVIDS["EventsEnabled"],
            health_spool_count_svid=DAVINCI_SVIDS["SpoolCountActual"],
            # DaVinci Host Interface Manual section 4.3.1.2. These were the
            # values hardcoded for every profile; stated here so the source of
            # each profile's timers is visible in one place.
            hsms_timers={"t3": 45, "t5": 10, "t6": 5, "t7": 10, "t8": 5},
            notes="Derived from MueTec DaVinci 200 MC4 HC1 SECS-Items workbook; 113 SVs, 282 events, E40/E87/E90/E94 conformant.",
        ),
        MachineProfile(
            profile_id="ptiq_secsgem",
            vendor="PTIQ",
            model="SECS/GEM Equipment",
            default_port=5000,
            default_secs_device_id=0,
            event_aliases=ptiq_events,
            ceid_aliases={},  # CEID numbers come from the per-equipment EIB export
            svids_by_name=PTIQ_SVIDS,
            identity_svid_names=["MDLN", "SOFTREV", "ControlState", "ProcessState", "Clock"],
            dvs_by_name=PTIQ_DVS,
            event_subscription_path="config/EventSubscription.json",
            # No vendor document states PTIQ's protocol timers, so these are
            # the shipped defaults written out for the same reason as the MG's:
            # an inherited value should be visible as a choice.
            hsms_timers={"t3": 45, "t5": 10, "t6": 5, "t7": 10, "t8": 5},
            notes=(
                "PTIQ SECS/GEM Host Interface Spec is generic - the spec defines named "
                "events (ProcessingStarted, SCHn.LotStarted, MaterialReceived...) but "
                "per-equipment CEID/SVID numbers come from the EIB model export. "
                "Override SVID numbers per machine via SvidList.json {SVID, Name} entries."
            ),
        ),
        MachineProfile(
            profile_id="nexgen_mg_series",
            vendor="NexGen Wafersystems",
            model="MG Series (MG21/MG22/MG22-300)",
            # The manual cites SEMI E37 but never states a port, device ID or
            # HSMS role. These are the same defaults as the other three
            # profiles and are GUESSES - correct them per machine in
            # production.yaml from the tool's own SECS/GEM configuration
            # screen; no rebuild is needed.
            default_port=5000,
            default_secs_device_id=0,
            event_aliases=mg_events,
            ceid_aliases=NEXGEN_MG_CEID_ALIASES,
            svids_by_name=NEXGEN_MG_SVIDS,
            dvs_by_name=NEXGEN_MG_DVS,
            identity_svid_names=["ControlState", "ProcessState", "LastEventID", "Clock"],
            event_subscription_path=str(
                Path("output") / "nexgen_mg_series" / "EventSubscription.json"
            ),
            ceid_dv_layout=NEXGEN_MG_CEID_DV_LAYOUT,
            ceid_load_port=NEXGEN_MG_CEID_LOAD_PORT,
            ceid_chamber=NEXGEN_MG_CEID_CHAMBER,
            # The MG carries pmNCurrWaferLoadPort inside the wafer-level
            # process-module reports, but the step family (222-231, 322-331)
            # and the rest of the chamber band arrive with no report at all -
            # 129 of 243 subscribed events link none. Those are resolved from
            # the chamber binding the wafer-level reports leave behind, which
            # is exact on a two-chamber tool running two lots where a
            # machine-wide "active load port" is not.
            chamber_event_ceids=NEXGEN_MG_CHAMBER_EVENT_CEIDS,
            ceid_state_transitions=NEXGEN_MG_CEID_STATE_TRANSITIONS,
            health_last_event_svid=NEXGEN_MG_SVIDS["LastEventID"],
            health_events_enabled_svid=NEXGEN_MG_SVIDS["EventsEnabled"],
            # No spool health variable: the MG documents spooling as
            # unsupported and all four spool SVs as not supported. There is no
            # equipment-side buffer, so middleware downtime is unrecoverable
            # data loss rather than a backlog to drain.
            health_spool_count_svid=None,
            # The MG manual cites SEMI E37 but states no T3/T5/T6/T7/T8 at
            # all, so these are the shipped defaults (the DaVinci figures)
            # written out rather than left empty. Stated explicitly so the
            # panel and this table both show that the tool in front of the
            # operator is running another vendor's numbers, and so correcting
            # them from the tool's own SECS/GEM screen is a config change
            # rather than a code change.
            hsms_timers={"t3": 45, "t5": 10, "t6": 5, "t7": 10, "t8": 5},
            notes=(
                "Derived from NWS MG Series SECS/GEM Documentation V1.1.18 "
                "(NexGen Wafersystems GmbH, 01.04.2025), sections 8.1/8.2. "
                "DOCUMENTATION-DERIVED AND NOT HARDWARE-VERIFIED: the manual "
                "disclaims its own constants (section 2 - CEIDs/VIDs/process "
                "states 'may change without prior notice') and omits every "
                "connection parameter. One superset profile covers MG21, MG22 "
                "and MG22-300 because the manual publishes one CEID table and "
                "one variable table for all three. Known manual contradictions "
                "handled defensively: ProcessState is a one-byte integer in "
                "the state-model section but ASCII in the SV table (both "
                "decode); terminal services are marked unimplemented yet fully "
                "documented (unused); spooling is marked unsupported yet "
                "spooling CEIDs 16-18 are listed as maintained (declared, "
                "never expected to fire)."
            ),
        ),
    ]
