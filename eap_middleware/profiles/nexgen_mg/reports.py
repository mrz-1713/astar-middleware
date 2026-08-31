"""NexGen MG data variables and the CEID -> report layout table."""
from __future__ import annotations

from typing import Dict, List, Tuple

from .metrics import NEXGEN_MG_METRIC_DVS, NEXGEN_MG_METRIC_REPORTS
from .variables import (
    MG_PORT1_LOT_CHEMISTRY,
    mg_pm_identity_dvs,
    mg_port_lot_dvs,
)

# --- END GENERATED ---


NEXGEN_MG_DVS: Dict[str, int] = {
    **MG_PORT1_LOT_CHEMISTRY,
    **mg_port_lot_dvs(),
    **mg_pm_identity_dvs(),
    # Per-PM step bookkeeping (PM1 1180-1189, PM2 1380-1389)
    **{
        f"pm{pm}{name}": base + offset
        for pm, base in ((1, 1180), (2, 1380))
        for name, offset in (
            ("StepTypePrevStep", 0), ("StepTypeCurrentStep", 1),
            ("PrevStepElapsedTime", 2), ("TotalProcessTimeWafer", 3),
            ("StepElapsedTime", 4), ("CurrRecpNumOfSteps", 7),
            ("CurrStepNumber", 8), ("PrevStepNumber", 9),
        )
    },
    # Material movement
    "portIdMaterialReceived": 1800,
    "portIdMaterialRemoved": 1801,
    # Last started / finished wafer
    **{
        f"lastStartedWafer{name}": 1810 + offset
        for name, offset in (
            ("PmId", 0), ("JobId", 1), ("LotId", 2), ("LoadSlot", 3),
            ("UnloadSlot", 4), ("LoadPort", 5), ("UnloadPort", 6), ("Cid", 7),
        )
    },
    **{
        f"lastFinishedWafer{name}": 1820 + offset
        for name, offset in (
            ("PmId", 0), ("JobId", 1), ("LotId", 2), ("LoadSlot", 3),
            ("UnloadSlot", 4),
        )
    },
    # Last started / finished lot (valid on the GEM ProcessingStarted /
    # ProcessingCompleted / ProcessingStopped events)
    "jobIdLastFinishedLot": 1830,
    "lotIdLastFinishedLot": 1831,
    "ppLastFinishedLot": 1832,
    "portIdLastFinishedLot": 1833,
    "cidLastFinishedLot": 1834,
    "jobIdLastStartedLot": 1835,
    "lotIdLastStartedLot": 1836,
    "ppLastStartedLot": 1837,
    "portIdLastStartedLot": 1838,
    "cidLastStartedLot": 1839,
    # Recipe / process program
    "ppSelectedName": 1850,
    "ppSelectedPortId": 1851,
    "ppChangeName": 1855,
    "ppChangeStatus": 1856,
    # GEM300 carrier / load port (E87)
    "PortID": 2080,
    "CarrierID": 2081,
    "LocationID": 2082,
    "PortTransferState": 2083,
    "CarrierIDStatus": 2084,
    "CarrierAccessingStatus": 2085,
    "SlotMapStatus": 2086,
    "LpAccessMode": 2087,
    "LpReservationState": 2088,
    "LpAssociationState": 2089,
    "Capacity": 2090,
    "ContentMap": 2091,
    "Reason": 2092,
    "SlotMap": 2093,
    "SubstrateCount": 2094,
    "Usage": 2095,
    # Process job / control job (E40 / E94)
    "PjID": 2100,
    "PjNewState": 2101,
    "PjPrevState": 2102,
    "CjID": 2110,
    "CjNewState": 2111,
    "CjPrevState": 2112,
    # Substrate (E90)
    "SubstDestination": 2200,
    "SubstID": 2201,
    "SubstSubstLocID": 2202,
    "SubsLotID": 2203,
    "SubsMtrlStatus": 2204,
    "SubstProcState": 2205,
    "SubstSource": 2206,
    "SubstState": 2207,
    "SubstLocID": 2208,
    "SubstLocState": 2209,
    "SubstLocSubstID": 2210,
    "SubstHistory": 2211,
    "PreAlignerId": 2900,
    # Process metrics. Named here so the mapper can label the V[] slots rather
    # than dropping them as unknown values; the reports that carry them are in
    # NEXGEN_MG_METRIC_REPORTS.
    **NEXGEN_MG_METRIC_DVS,
}


# --- Report definitions -----------------------------------------------------
#
# One table drives BOTH the profile's positional V[] layout and the generated
# EventSubscription.json report VID lists, so the two can never drift apart:
# each entry is (name-the-report-slot-gets, VID-requested-in-S2F33).
#
# The slot names are chosen to line up with the canonical mapper's EXISTING key
# precedence - no mapper changes and no invented composite identifiers:
#   LotID       -> CSV LotID
#   RecipeName  -> CSV Recipe
#   PortID      -> CSV LoadPort (taken from the payload, never inferred)
#   WaferID     -> CSV WaferID, preferred key -> the GEM300 substrate ID
#   SubstID     -> CSV WaferID fallback -> the cassette load slot number
# Everything else (CarrierID, JobID, unload slot/port, counters, chemistry)
# has no dedicated column and survives in the raw event payload.


def _mg_identity_report(pm: int, with_substrate: bool) -> List[Tuple[str, int]]:
    base = 1900 if pm == 1 else 2000
    substrate_vid = 2212 if pm == 1 else 2213
    slots: List[Tuple[str, int]] = []
    if with_substrate:
        # GEM300 tools report a real substrate ID here; cassette tools leave it
        # empty and the mapper falls through to SubstID (the load slot).
        slots.append(("WaferID", substrate_vid))
    slots += [
        ("LotID", base + 1),        # pmNCurrWaferLotId
        ("RecipeName", base + 3),   # pmNCurrWaferPPId
        ("PortID", base + 6),       # pmNCurrWaferLoadPort
        ("SubstID", base + 4),      # pmNCurrWaferLoadSlot (WaferID fallback)
        ("CarrierID", base + 2),    # pmNCurrWaferCId
        ("JobID", base + 0),        # pmNCurrWaferJobId
        ("UnloadSlot", base + 5),
        ("UnloadPort", base + 7),
    ]
    return slots


_MG_LOT_CHEMISTRY_REPORT: List[Tuple[str, int]] = [
    (name[len("port1"):], vid)  # drop the port prefix: the row already names the port
    for name, vid in sorted(MG_PORT1_LOT_CHEMISTRY.items(), key=lambda item: item[1])
]


def _mg_reports() -> Dict[int, Tuple[Tuple[str, int], ...]]:
    reports: Dict[int, List[Tuple[str, int]]] = {
        # --- GEM equipment-level processing ---
        4: [("JobID", 1835), ("LotID", 1836), ("RecipeName", 1837),
            ("PortID", 1838), ("CarrierID", 1839)],
        # --- Recipe / process program ---
        12: [("PPChangeName", 1855), ("PPChangeStatus", 1856)],
        13: [("RecipeName", 1850), ("PortID", 1851)],
        # --- Material movement ---
        14: [("PortID", 1800)],
        15: [("PortID", 1801)],
    }
    for ceid in (5, 6):
        reports[ceid] = [
            ("JobID", 1830), ("LotID", 1831), ("RecipeName", 1832),
            ("PortID", 1833), ("CarrierID", 1834),
        ] + _MG_LOT_CHEMISTRY_REPORT

    # --- Per-load-port lifecycle. CEIDs 120-123 (ReadyToLoad), 130-137
    # (CasPlaced/CasRemoved) and 145 have NO valid data variables in the
    # manual, so they get NO report at all: linking an empty report list is
    # what silently deleted the DaVinci link, and the port is already known
    # from the CEID itself via ceid_load_port.
    for port in range(1, 5):
        base = 100 * (port + 1)
        reports[123 + port] = [        # portNReadyToUnload
            ("WafersFinished", base + 0),
            ("TotalLotTime", base + 13),
            ("TotalProcessTime", base + 14),
        ]
        reports[139 + port] = [("WafersInCassette", base + 2)]   # portNCasMapped
        reports[149 + port] = [                                  # processingStartedPortN
            ("WafersToProcess", base + 1),
            ("OutputPort", base + 10),
            ("StartProcessDate", base + 11),
            ("StartProcessTime", base + 12),
        ]

    # --- Process modules. The identity block is valid on 212-216 and 221
    # (PM1) / 312-316 and 321 (PM2); the substrate ID only on the wafer
    # started/finished pair.
    for pm, ceid_base, step_base in ((1, 200, 1180), (2, 300, 1380)):
        for offset in (12, 13):
            reports[ceid_base + offset] = _mg_identity_report(pm, with_substrate=True)
        for offset in (14, 15):
            reports[ceid_base + offset] = _mg_identity_report(pm, with_substrate=False)
        # pmNProcessAborted also carries the per-lot chemistry summary.
        reports[ceid_base + 16] = (
            _mg_identity_report(pm, with_substrate=False) + _MG_LOT_CHEMISTRY_REPORT
        )
        reports[ceid_base + 20] = [                     # pmNStepStarted
            ("StepType", step_base + 1),
            ("StepNumber", step_base + 8),
        ]
        reports[ceid_base + 21] = _mg_identity_report(pm, with_substrate=False) + [
            ("PrevStepType", step_base + 0),
            ("PrevStepNumber", step_base + 9),
        ]

    # --- GEM300 job / carrier / substrate ---
    for ceid in range(700, 719):
        reports[ceid] = [("PjID", 2100), ("PjNewState", 2101), ("PjPrevState", 2102)]
    for ceid in range(720, 734):
        reports[ceid] = [("CjID", 2110), ("CjNewState", 2111), ("CjPrevState", 2112)]
    for ceid in range(751, 761):
        reports[ceid] = [("PortID", 2080), ("PortTransferState", 2083)]
    # CEID 772 additionally carries the carrier's contents on first ID read.
    reports[772] = [
        ("CarrierID", 2081), ("CarrierIDStatus", 2084), ("Capacity", 2090),
        ("SlotMap", 2093), ("SubstrateCount", 2094), ("ContentMap", 2091),
        ("Usage", 2095),
    ]
    for ceid in (773, 774, 775, 776, 777, 778, 779):
        reports[ceid] = [("CarrierID", 2081), ("CarrierIDStatus", 2084)]
    for ceid in (783, 784, 785, 786):
        reports[ceid] = [("PortID", 2080), ("CarrierID", 2081),
                         ("LocationID", 2082), ("SlotMapStatus", 2086)]
    for ceid in (787, 788, 789):
        reports[ceid] = [("CarrierID", 2081), ("CarrierAccessingStatus", 2085)]
    # CarrierToNoState gets CarrierID only: the manual marks
    # CarrierAccessingStatus (2085) valid on 783 and 787-789, NOT on 790, and
    # one invalid VID would cost the entire gem300 band.
    reports[790] = [("CarrierID", 2081)]
    for ceid in (801, 802, 803):
        reports[ceid] = [("PortID", 2080), ("LpAccessMode", 2087)]
    for ceid in (804, 805):
        reports[ceid] = [("PortID", 2080), ("LpReservationState", 2088)]
    for ceid in (806, 807, 808):
        reports[ceid] = [("PortID", 2080), ("LpAssociationState", 2089)]
    for ceid in range(850, 860):
        reports[ceid] = [
            ("WaferID", 2201), ("LotID", 2203), ("SubstSubstLocID", 2202),
            ("SubstDestination", 2200), ("SubstSource", 2206),
            ("SubsMtrlStatus", 2204), ("SubstProcState", 2205),
            ("SubstState", 2207),
        ]
    for ceid in (860, 861):
        reports[ceid] = [("SubstLocID", 2208), ("SubstLocState", 2209),
                         ("SubstLocSubstID", 2210)]
    # --- Cassette slot map ---
    # cassetteMapped fires for whichever port was just mapped and has no data
    # variables of its own, so the slot map is pulled from the two status
    # variables that carry it. mapResultLastMap is one entry per slot:
    # 1=FULLSLOT, 2=EMPTYSLOT, 3=CROSSSLOTTED, 4=DOUBLESLOTTED - the only place
    # cross- and double-slotting is reported on a non-GEM300 tool.
    #
    # This is the ONLY report in the subscription that asks for status
    # variables rather than data variables. SEMI E5 permits it and the manual
    # says status variables are always valid, but it is the one untested
    # assumption here, which is why this CEID sits alone in its own band.
    #
    # NAMED "SlotMapGem", not "SlotMap", on purpose. The manual defines two
    # incompatible slot-map encodings and value 3 means opposite things in
    # them: in the status variables (SVID 3110/4306) 3 = CROSSSLOTTED, while
    # in the E87 carrier attribute (S3F17 CATTRID SlotMap, section 6.3,
    # enumerated UNDEFINED / EMPTY / NOT EMPTY / CORRECTLY OCCUPIED /
    # DOUBLESLOTTED / CROSS SLOTTED) 3 = CORRECTLY OCCUPIED. DVID 2093 below
    # carries the E87 form and keeps the plain name. Anything downstream that
    # reads a raw slot value therefore knows which enumeration applies from
    # the field name alone, instead of having to know which VID produced it.
    reports[145] = [("PortID", 4305), ("SlotMapGem", 4306)]
    # --- Auxiliary ---
    reports[600] = [("PreAlignerId", 2900)]

    # --- Process metrics (manual section 8.2) ---
    # Merged last so the chemistry variables extend, rather than replace, the
    # identity slots an event already carried. 213/313 keep their identity
    # block in front of the metrics; every other target had no report at all.
    for ceid, slots in NEXGEN_MG_METRIC_REPORTS.items():
        existing = reports.get(ceid, [])
        seen = {vid for _name, vid in existing}
        reports[ceid] = list(existing) + [
            (name, vid) for name, vid in slots if vid not in seen
        ]
    return {ceid: tuple(slots) for ceid, slots in reports.items()}


NEXGEN_MG_REPORTS: Dict[int, Tuple[Tuple[str, int], ...]] = _mg_reports()
