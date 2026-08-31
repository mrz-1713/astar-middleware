"""NexGen MG series status and data variable identifiers."""
from __future__ import annotations

from typing import Dict


# ---------------------------------------------------------------------------
# NexGen Wafersystems MG Series (MG21 / MG22 / MG22-300)
# ---------------------------------------------------------------------------
# Sourced from "NWS MG Series SECS/GEM Documentation V1.1.18" (01.04.2025),
# sections 8.1 (Collection Events), 8.2 (Data Variables / Status Variables).
#
# UNVERIFIED AGAINST HARDWARE. Section 2 of that manual states CEIDs, VIDs and
# processing-state numbers "may change without prior notice", and constants
# were still being added in v1.1.16 (Nov 2024) and v1.1.18 (Apr 2025). Every
# number below came from the document; none has been seen on a real tool.
#
# Structural note: unlike SPTS and DaVinci, the MG carries the originating load
# port INSIDE every process-module event (pmNCurrWaferLoadPort). This profile
# therefore declares no chamber_event_ceids and no ceid_state_transitions - the
# JobTracker is deliberately unused, so chamber events can never be misattributed
# when two process modules run lots from different ports concurrently.


def _mg_port_svids() -> Dict[str, int]:
    """Per-load-port status variables, 3100/3200/3300/3400 + offset."""
    out: Dict[str, int] = {}
    for port in range(1, 5):
        base = 3000 + port * 100
        out[f"port{port}JobPending"] = base
        out[f"port{port}MapResult"] = base + 10
        out[f"port{port}Status"] = base + 20
        out[f"port{port}PendingJobId"] = base + 21
        out[f"port{port}PendingLotId"] = base + 22
        out[f"port{port}PendingPPID"] = base + 23
        out[f"port{port}PendingCid"] = base + 24
        out[f"port{port}JobId"] = base + 30
        out[f"port{port}LotId"] = base + 31
        out[f"port{port}PPID"] = base + 32
        out[f"port{port}Cid"] = base + 33
    return out


def _mg_pm_svids() -> Dict[str, int]:
    """Process-module status variables, PM1 at 3500 and PM2 at 3700."""
    out: Dict[str, int] = {}
    for pm, base in ((1, 3500), (2, 3700)):
        out[f"pm{pm}PPExec"] = base
        out[f"pm{pm}N2ChuckFlow"] = base + 1
        out[f"pm{pm}N2DryFlow"] = base + 2
        for medium in (1, 2, 3):
            out[f"pm{pm}Med{medium}Temp"] = base + 2 + medium
            out[f"pm{pm}Med{medium}Flow"] = base + 5 + medium
        out[f"pm{pm}DiFlow"] = base + 20
        out[f"pm{pm}DiwO3Flow"] = base + 21
        out[f"pm{pm}ChuckSpeed"] = base + 30
        out[f"pm{pm}WaferCount"] = base + 31
        out[f"pm{pm}HpcDiwFlow"] = base + 32
        out[f"pm{pm}HpcN2Flow"] = base + 33
        out[f"pm{pm}Exhaust"] = base + 34
        for medium in (1, 2, 3, 4):
            out[f"pm{pm}BemMed{medium}Flow"] = base + 34 + medium
        out[f"pm{pm}LowFlowMedFlow"] = base + 39
        out[f"pm{pm}CO2Flow"] = base + 40
        out[f"pm{pm}CO2Conductivity"] = base + 41
        out[f"pm{pm}CO2Temp"] = base + 42
        out[f"pm{pm}CO2Pressure"] = base + 43
        out[f"pm{pm}Exhaust1"] = base + 44
        out[f"pm{pm}Exhaust2"] = base + 45
        out[f"pm{pm}State"] = base + 50
    return out


def _mg_gem300_lp_svids() -> Dict[str, int]:
    """E87 load-port object attributes, Lp1 at 5000, +10 per port."""
    attributes = (
        "AccessMode", "CarrierID", "CarrierIDatFIMS", "LocationID",
        "LocationIDatFIMS", "PortAssociationState", "PortID",
        "PortReservationState", "PortStateInfo", "PortTransferState",
    )
    return {
        f"Lp{port}{name}": 5000 + (port - 1) * 10 + index
        for port in range(1, 5)
        for index, name in enumerate(attributes)
    }


NEXGEN_MG_SVIDS: Dict[str, int] = {
    # GEM status variables (manual 8.2 "Status Variables"). AlarmsSet (9) and
    # the four spool variables (17-20) are documented "Not supported" and are
    # deliberately ABSENT: an absent AlarmsSet is what tells the service the
    # active-alarm set cannot be re-queried after a reconnect, and an absent
    # SpoolCountActual disables the spool-backlog health check for a tool that
    # has no equipment-side buffer at all.
    "AlarmsEnabled": 8,
    "Clock": 10,
    "ControlState": 11,
    "EventsEnabled": 12,
    "PreviousControlState": 13,
    "PreviousProcessState": 14,
    "ProcessState": 15,
    "LastEventID": 16,
    **_mg_port_svids(),
    **_mg_pm_svids(),
    # Supply / facility monitoring
    **{f"supplyMed{m}Flow": 3899 + m for m in (1, 2, 3, 4)},
    "supplyDiwFlow": 3904,
    "supplyDiwO3Flow": 3905,
    **{f"supplyMed{m}Temp": 3905 + m for m in (1, 2, 3, 4)},
    "supplyDiwTemp": 3910,
    "supplyDiwO3Temp": 3911,
    "supplyN2Temp": 3912,
    "supplyN2CoilTemp": 3913,
    **{f"supplyMed{m}HeatPurgeFlow": 3913 + m for m in (1, 2, 3, 4)},
    **{f"supplyMed{m}HeatPurgePressure": 3917 + m for m in (1, 2, 3, 4)},
    **{f"supplyMed{m}CoilTemp": 3921 + m for m in (1, 2, 3, 4)},
    **{f"supplyMed{m}LiquidTemp": 3925 + m for m in (1, 2, 3, 4)},
    "facSupplyCdaPressure": 4000,
    "facSupplyN2PressureLeft": 4001,
    "facSupplyN2PressureRight": 4002,
    "facSupplyVacuumPressure": 4003,
    "facSupplyDiwPressure": 4004,
    "lightTowerStatus": 4303,
    "buzzerStatus": 4304,
    "portIdLastMapped": 4305,
    "mapResultLastMap": 4306,
    # Same variable under the name the CEID 145 report uses. 1=FULLSLOT,
    # 2=EMPTYSLOT, 3=CROSSSLOTTED, 4=DOUBLESLOTTED - NOT the E87 enumeration
    # that DVID 2093 "SlotMap" uses.
    "SlotMapGem": 4306,
    # Chemistry cabinets / media
    **{f"med{m}EtchRate": 4309 + m for m in (1, 2, 3)},
    **{f"med{m}ElapsedTime": 4314 + m for m in (1, 2, 3)},
    **{f"med{m}BathLifeTimeRem": 4319 + m for m in (1, 2, 3)},
    **{f"med{m}BathLifeTime": 4324 + m for m in (1, 2, 3)},
    **{f"med{m}ElapsedTimeRem": 4329 + m for m in (1, 2, 3)},
    **{f"chc{c}State": 4349 + c for c in (1, 2, 3)},
    **{f"waferCountChc{c}Actual": 4354 + c for c in (1, 2, 3)},
    **{f"chc{c}RefillStatus": 4359 + c for c in (1, 2, 3)},
    **{f"waferCountChc{c}Total": 4364 + c for c in (1, 2, 3)},
    **{
        f"med{medium}Comp{component}Conc": 4400 + (medium - 1) * 5 + (component - 1)
        for medium in (1, 2, 3)
        for component in (1, 2, 3, 4, 5)
    },
    "diwO3Concentration": 4450,
    **{f"diwO3ConcChannel{c}": 4450 + c for c in range(1, 7)},
    # GEM300 objects
    "CarrierLocationMatrix": 4999,
    **_mg_gem300_lp_svids(),
    "SubstLocPM1Subst": 5100,
    "SubstLocPM2Subst": 5101,
    "SubstLocROBOTSubst": 5110,
    "SubstLocLECO1Subst": 5120,
    "SubstLocUECO1Subst": 5121,
    "SubstLocLECO2Subst": 5122,
    "SubstLocUECO2Subst": 5123,
    "SubstLocPA1Subst": 5130,
    "SubstLocPA2Subst": 5131,
    "SubstLocATMSI1Subst": 5140,
    "SubstLocATMSI2Subst": 5141,
    "CjQueueAvailableSpace": 5300,
    "QueuedCJobs": 5301,
}


def mg_port_lot_dvs() -> Dict[str, int]:
    """Per-port lot summary data variables, port1 at 200, +100 per port."""
    out: Dict[str, int] = {}
    for port in range(1, 5):
        base = 100 * (port + 1)
        out[f"port{port}wafersFinished"] = base
        out[f"port{port}wafersToProcess"] = base + 1
        out[f"port{port}wafersInCassette"] = base + 2
        out[f"port{port}OutputPort"] = base + 10
        out[f"port{port}StartProcessDate"] = base + 11
        out[f"port{port}StartProcessTime"] = base + 12
        out[f"port{port}TotalLotTime"] = base + 13
        out[f"port{port}TotalProcessTime"] = base + 14
    return out


# Per-lot chemistry summary. The manual documents this block for load port 1
# ONLY (VIDs 100-162); there is no port2/3/4 equivalent in the appendix, which
# is a documentation asymmetry rather than a transcription gap.
MG_PORT1_LOT_CHEMISTRY: Dict[str, int] = {
    "port1N2ChuckFlowMinLot": 100,
    "port1N2ChuckFlowMaxLot": 101,
    "port1N2ChuckFlowAvrLot": 102,
    "port1N2DryFlowMinLot": 103,
    "port1N2DryFlowMaxLot": 104,
    "port1N2DryFlowAvrLot": 105,
    **{f"port1Med{m}TempMinLot": 109 + m for m in (1, 2, 3)},
    **{f"port1Med{m}TempMaxLot": 112 + m for m in (1, 2, 3)},
    **{f"port1Med{m}TempAvrLot": 115 + m for m in (1, 2, 3)},
    **{f"port1Med{m}FlowMinLot": 129 + m for m in (1, 2, 3)},
    **{f"port1Med{m}FlowMaxLot": 132 + m for m in (1, 2, 3)},
    **{f"port1Med{m}FlowAvrLot": 135 + m for m in (1, 2, 3)},
    "port1DiFlowMinLot": 150,
    "port1DiFlowMaxLot": 151,
    "port1DiFlowAvrLot": 152,
    "port1ChuckSpeedMinLot": 160,
    "port1ChuckSpeedMaxLot": 161,
    "port1ChuckSpeedAvrLot": 162,
}


def mg_pm_identity_dvs() -> Dict[str, int]:
    """The identity block: PM1 at 1900, PM2 at 2000, plus the substrate IDs."""
    fields = (
        "JobId", "LotId", "CId", "PPId", "LoadSlot", "UnloadSlot",
        "LoadPort", "UnloadPort",
    )
    out = {
        f"pm{pm}CurrWafer{name}": base + index
        for pm, base in ((1, 1900), (2, 2000))
        for index, name in enumerate(fields)
    }
    out["pm1CurrWaferSubstId"] = 2212
    out["pm2CurrWaferSubstId"] = 2213
    return out
