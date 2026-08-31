"""PTIQ SECS-II tables."""
from __future__ import annotations

from typing import Dict


# PTIQ host interface spec is generic - it defines NAMED events and named
# variables but leaves CEID/SVID numbering to the per-equipment EIB model
# export. The numbers below are the Cimetrix CIMConnect conventional defaults
# (which match the SPTS Omega Cimetrix mapping). Customer can override any of
# them per-machine via the SvidList.json admin file using {SVID, Name} entries.
PTIQ_SVIDS: Dict[str, int] = {
    "Clock": 27,
    "ControlState": 28,
    "ControlCurrState": 29,
    # ControlPrevState aligns with PreviousControlState (SPTS Cimetrix convention).
    "ControlPrevState": 36,
    "EventsEnabled": 30,
    "CommState": 31,
    "MDLN": 32,
    "LastCEID": 34,
    "PreviousControlState": 36,
    "SOFTREV": 39,
    "Time": 40,
    "AlarmID": 22,
    "AlarmsEnabled": 23,
    "AlarmsSet": 24,
    "AlarmState": 25,
    "AlarmsCount": 26,
    "ProcessState": 1029,
    "PreviousProcessState": 1030,
    "PPExecName": 1040,
    "PPFormat": 1041,
    "PPError": 1042,
    "AnnotatedEventReport": 4001,
    "SpoolCountActual": 2016,
    "SpoolCountTotal": 2017,
    "SpoolFullTime": 2018,
    "SpoolStartTime": 2019,
}


PTIQ_DVS: Dict[str, int] = {
    "OperatorCommand": 16,
    "PPChangeName": 1043,
    "PPChangeStatus": 1044,
    "ECChanged": 1045,
    "AlarmCode": 22,
    "AlarmText": 23,
    "PPID": 1050,
    "LotID": 1051,
    "RecipeName": 1052,
    "WaferID": 1053,
    "CarrierID": 1054,
    "PortID": 1055,
}
