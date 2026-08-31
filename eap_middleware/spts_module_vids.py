"""Resolve SPTS fxP Appendix E module status-variable IDs for one tool layout.

Appendix E of the SPTS manual publishes a formula, not a table:

    VID = (station * 10000) + (type_offset * 100) + variable_offset + 10000

A flat SVID table therefore cannot express it, which is why the profile carries
only the 158 fixed variables from sections 12.4 and 12.8 and nothing from
Appendix E. Two facts make a static table impossible rather than merely large:

1. The same variable name maps to a different VID at every station, so the
   table would have to be per-tool anyway.
2. **The formula is ambiguous.** Station types are spaced 100 apart, but four
   families exceed 99 variables - Etch (201), DeltaAPM (134), Deposition (108),
   Softetch (103) - so their high offsets overrun the next type's range. At one
   station, 107 VIDs are claimed by two different families. For example
   `Statx_Etch_DSV_BackingPumpAlarm` (Etch, offset 100) and
   `Statx_Deposition_MV_ProcessTime` (Deposition RevB, offset 0) both compute
   to VID 32500 at Process Module 1.

The ambiguity resolves only once you know which module type occupies which
station, because a station holds exactly one type at a time. So this module
takes that layout as input and refuses to guess it.

    >>> names = resolve({"Process Module 1": "Etch"})
    >>> names[32500]
    'Statx_Etch_DSV_BackingPumpAlarm'

Regenerate the offset data from the manual with:

    python -m scripts.gen_spts_module_variables
"""

from __future__ import annotations

import dataclasses
import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, Mapping, Optional, cast

DATA = (
    Path(__file__).resolve().parent.parent
    / "output" / "spts_fxp_omega" / "ModuleVariables.json"
)


class ModuleLayoutError(ValueError):
    """The requested station or module type is not one the manual defines."""


@lru_cache(maxsize=1)
def _data() -> Dict[str, object]:
    return json.loads(DATA.read_text(encoding="utf-8"))


def station_numbers() -> Dict[str, int]:
    return dict(_data()["station_numbers"])  # type: ignore[arg-type]


def type_offsets() -> Dict[str, int]:
    """Station-type families whose type offset the manual states.

    Families the manual leaves unmapped (DeltaAPM, VCE, PreHeat) are excluded
    on purpose: a guessed offset would mislabel every variable in the family
    while looking perfectly healthy.
    """
    return dict(_data()["type_offsets"])  # type: ignore[arg-type]


# Appendix F section 24.1 gives the values the `StationType` status variable
# reports; Appendix E gives the VID-arithmetic offsets. They are DIFFERENT
# numbers for the same hardware, and the manual never states the correspondence,
# so it is asserted here from the type descriptions and nowhere inferred at
# runtime. Reviewed against Appendix E's and F's own wording; verify against a
# real tool before trusting the resulting VIDs.
#
# Deliberately absent:
#   169 Delta APM PM     - reports a value but Appendix E gives it no offset
#   0/255/999            - Not Fitted / Invalid / Dummy: no module present
#   5/12/22/31/100/222   - transports, cassette, coolstation, inliner, buffer:
#                          Appendix E has no variable family for them
# ForceFill has offset 3 but no Appendix F value at all, so no tool can report
# itself as one - it is reachable only by naming the family directly.
STATION_TYPE_VALUE_TO_FAMILY: Dict[int, str] = {
    40: "SDep",               # Sputter Deposition PM
    41: "Deposition",         # Deposition RevB PM
    55: "HSE",                # HSE PM
    57: "Softetch",           # SoftEtch RevB PM
    86: "C3M",                # C3M (Bridge)
    90: "HeatNT",             # Heat PM
    91: "HeatNT",             # Heat RevB PM - shares the Heat offset
    123: "ProCve",            # Pro CVE PM
    221: "PrimaxxMonarch25",  # Primaxx Monarch 25
    # All six Etch variants share Appendix E's single Etch offset (24).
    180: "Etch",              # Etch MORI PM
    181: "Etch",              # Etch PERIE PM
    182: "Etch",              # Etch ICP PM
    183: "Etch",              # Etch ISOPOD PM
    184: "Etch",              # Etch GPE PM
    185: "Etch",              # Etch DSI PM
}


def family_for_station_type(value: int) -> str:
    """Appendix E family for an Appendix F `StationType` reading.

    A live tool reports its module type as an Appendix F number, but the VID
    formula needs the Appendix E family. Raises rather than guessing when the
    reading has no documented family - notably Delta APM (169), which reports a
    value the formula cannot use.
    """
    family = STATION_TYPE_VALUE_TO_FAMILY.get(int(value))
    if family is None:
        label = _data()["station_type_values"].get(  # type: ignore[union-attr]
            str(int(value)), "undocumented"
        )
        raise ModuleLayoutError(
            f"StationType {value} ({label}) has no Appendix E variable family, "
            "so its module VIDs cannot be computed"
        )
    return family


def vid_for(station: str, family: str, variable_offset: int) -> int:
    """Absolute VID for one variable, per the Appendix E formula."""
    stations, types = station_numbers(), type_offsets()
    if station not in stations:
        raise ModuleLayoutError(
            f"unknown station {station!r}; expected one of "
            f"{sorted(stations)}"
        )
    if family not in types:
        raise ModuleLayoutError(
            f"station type {family!r} has no documented type offset; "
            f"expected one of {sorted(types)}"
        )
    return (
        stations[station] * 10_000
        + types[family] * 100
        + int(variable_offset)
        + 10_000
    )


def resolve(layout: Mapping[str, str]) -> Dict[int, str]:
    """{VID: variable name} for a tool whose stations hold the given types.

    `layout` maps a station name from the manual's Station Number list to a
    station-type family, e.g. `{"Process Module 1": "Etch"}`.

    Raises `ModuleLayoutError` if two entries in the layout compute the same
    VID. That cannot happen for a physically valid layout - each station has its
    own 10000 block - so a clash means the layout itself is wrong, and silently
    keeping one of the two would attribute a reading to the wrong module.
    """
    families = _data()["families"]
    names: Dict[int, str] = {}
    owner: Dict[int, str] = {}
    for station, family in layout.items():
        offsets = families.get(family)  # type: ignore[union-attr]
        if offsets is None:
            raise ModuleLayoutError(
                f"no Appendix E variables for station type {family!r}"
            )
        for offset, name in offsets.items():
            vid = vid_for(station, family, int(offset))
            if vid in names and owner[vid] != f"{station}/{family}":
                raise ModuleLayoutError(
                    f"VID {vid} claimed by both {owner[vid]} and "
                    f"{station}/{family} - check the layout"
                )
            names[vid] = name
            owner[vid] = f"{station}/{family}"
    return names


def demo() -> None:
    """Self-check: run `python -m eap_middleware.spts_module_vids`."""
    stations, types = station_numbers(), type_offsets()
    assert stations["Process Module 1"] == 2, stations
    assert "Etch" in types and "DeltaAPM" not in types, types

    # The worked example from the module docstring.
    assert vid_for("Process Module 1", "Etch", 100) == 32500
    assert vid_for("Process Module 1", "Deposition", 0) == 32500, (
        "the documented collision must still reproduce - it is the reason "
        "this module exists"
    )

    names = resolve({"Process Module 1": "Etch"})
    assert names[32500] == "Statx_Etch_DSV_BackingPumpAlarm", names[32500]
    assert len(names) == 199, len(names)

    # A real multi-module tool: distinct stations never clash.
    both = resolve({
        "Process Module 1": "Etch",
        "Process Module 2": "Deposition",
        "VCE A": "ForceFill",
    })
    assert len(both) == 199 + 108 + 21, len(both)

    # Appendix F runtime values map onto Appendix E families, never inferred.
    assert family_for_station_type(180) == "Etch"
    assert family_for_station_type(185) == "Etch", "all six variants share one offset"
    assert family_for_station_type(91) == "HeatNT"

    # Unmapped families, unknown stations and undocumented station types must
    # raise, never guess.
    for bad in (
        lambda: vid_for("Process Module 1", "DeltaAPM", 0),
        lambda: vid_for("Process Module 9", "Etch", 0),
        lambda: resolve({"Process Module 1": "DeltaAPM"}),
        lambda: family_for_station_type(169),  # Delta APM: value but no offset
        lambda: family_for_station_type(0),    # Station Not Fitted
    ):
        try:
            bad()
        except ModuleLayoutError:
            pass
        else:
            raise AssertionError("expected ModuleLayoutError")

    families = cast(Mapping[str, Mapping[str, int]], _data()["families"])
    print(
        f"OK: {len(stations)} stations, {len(types)} mapped types, "
        f"{sum(len(v) for v in families.values())} offsets"
    )


if __name__ == "__main__":
    demo()


# ---------------------------------------------------------------------------
# Alarm identity (manual section 8.3)
#
# An SPTS alarm id is arithmetic, not an opaque number:
#
#     ALID     = station x 10,000,000 + type x 100,000 + offset
#     ON CEID  = station x 10,000,000 + type x 100,000 + 10,000 + offset
#     OFF CEID = ON CEID + 1,000,000,000
#
# Without decoding it, an S5F1 arrives as "22400005" and the operator has to
# know that means Process Module 1, an Etch module. The station numbers come
# from the same table Appendix E uses (already loaded above); the station-type
# list below is section 8.3's own, which is a superset of Appendix E's variable
# families - it names the transport/cassette/inliner types that carry alarms
# but no VID family.
# ---------------------------------------------------------------------------

_ALARM_STATION_STRIDE = 10_000_000
_ALARM_TYPE_STRIDE = 100_000
_ALARM_CEID_BIAS = 10_000
_ALARM_OFF_CEID_BIAS = 1_000_000_000

# Section 8.3 "Station Type" list, verbatim.
ALARM_STATION_TYPES: Dict[int, str] = {
    3: "Forcefill PM",
    4: "Sputter Deposition PM",
    7: "HSE PM",
    9: "Heat PM",
    20: "Level 0/1 Brooks MX Transport",
    21: "Level 0/1 Brooks Coolstation",
    22: "Level 0/1 Brooks MX Cassette",
    23: "Level 0/1 Brooks Inliner",
    24: "Etch PM",
    25: "Deposition RevB PM",
    26: "SoftEtch RevB PM",
    27: "Heat RevB PM",
    34: "Delta APM PM",
    58: "Pro CVE PM",
}


@dataclasses.dataclass(frozen=True)
class AlarmIdentity:
    """What a section 8.3 alarm id says about where the alarm came from."""

    alid: int
    station_number: int
    station_type: int
    offset: int
    station_name: str = ""
    station_type_name: str = ""

    @property
    def label(self) -> str:
        """A short human label, e.g. "Process Module 1 (Etch PM) #5"."""
        where = self.station_name or f"station {self.station_number}"
        what = self.station_type_name or f"type {self.station_type}"
        return f"{where} ({what}) #{self.offset}"


@lru_cache(maxsize=1)
def _station_names_by_number() -> Dict[int, str]:
    return {number: name for name, number in station_numbers().items()}


def decode_alarm_id(alid: object) -> Optional[AlarmIdentity]:
    """Split an SPTS ALID into station, station type and offset.

    Returns None for anything that is not a plausible section 8.3 id - a
    negative number, a non-numeric value, or an id whose station number is not
    one the manual lists. Guessing would be worse than saying nothing: the
    label goes in front of an operator deciding which module to walk to.

    Accepts the ON/OFF collection-event forms too, so the same call works
    whether the id arrived on S5F1 or as an alarm CEID.
    """
    try:
        value = int(str(alid).strip())
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    if value >= _ALARM_OFF_CEID_BIAS:
        value -= _ALARM_OFF_CEID_BIAS
    station_number, remainder = divmod(value, _ALARM_STATION_STRIDE)
    station_type, offset = divmod(remainder, _ALARM_TYPE_STRIDE)
    if offset >= _ALARM_CEID_BIAS:
        # The ON/OFF collection-event forms add 10,000 to the alarm offset.
        offset -= _ALARM_CEID_BIAS
    names = _station_names_by_number()
    if station_number not in names:
        return None
    return AlarmIdentity(
        alid=int(str(alid).strip()),
        station_number=station_number,
        station_type=station_type,
        offset=offset,
        station_name=names[station_number],
        station_type_name=ALARM_STATION_TYPES.get(station_type, ""),
    )
