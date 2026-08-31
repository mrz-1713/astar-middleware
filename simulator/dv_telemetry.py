"""Plausible values for the process variables a profile's reports carry.

`ProfileSimulator._dv_value` recognises the identity slots of a report - job,
lot, carrier, wafer, slot, recipe, port - and returned a literal ``0`` for
everything else. For the NexGen MG that is most of the payload: report
1000000213 (pm1WaferFinished) has 74 slots, of which 22 are identity and 52
are process telemetry - N2 chuck and dry flows, three medium temperatures,
three medium flows, DI and DIW/O3 flows, HPC flows, four BEM medium flows,
chuck speed and total process time. Every one of those went out as ``<U4 0>``.

That made the simulator useless for the thing the middleware exists to do.
The CSV contract is nine columns; everything else - the whole per-medium
chemistry block - reaches a dashboard only through the telemetry payload, so a
rig running this simulator produced a fully green end-to-end test in which
every published process value was zero, and no operator could tell a mapping
fault from "the simulator sends nothing".

The values here are invented, not measured, and are deliberately
*self-consistent* rather than merely random:

  * A quantity is recognised from its name (flow, temperature, pressure,
    speed, time, ...) and drawn from a range that is credible for that kind.
  * Min/Avr/Max triples that describe one measurement agree, because they are
    all derived from a single underlying sample of that measurement. A tool
    never reports a minimum above its maximum, so neither does this - which
    means a downstream rule that checks the ordering is exercised properly.
  * Everything is a deterministic function of (tool, lot, wafer, measurement),
    so two runs of the same script produce the same numbers and a test can
    assert on them.

Names that match no known quantity still fall back to 0: inventing a number
for a variable whose meaning is unknown would be worse than an obvious blank.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional, Sequence, Tuple

# Words that mark a slot as an aggregate of one underlying measurement.
# Dropped before hashing so pm1Med1TempMinWafer, ...AvrWafer and ...MaxWafer
# all resolve to the same base sample and therefore agree with each other.
_AGGREGATES: dict[str, float] = {
    "min": 0.92,
    "max": 1.08,
    "avr": 1.0,
    "avrg": 1.0,
    "avg": 1.0,
    "mean": 1.0,
}

# Words that only say what the value is scoped to, not what it measures.
_SCOPES = frozenset({"wafer", "lot", "job", "carrier", "batch", "run"})

# (word phrases, low, high, decimals). First match wins, so the more specific
# kinds are listed first. `decimals=0` yields an integer, which reaches the
# wire as U4 rather than F4.
#
# Matching is on whole words, never substrings: "rpm" occurs inside
# lastStartedWafe(rPmI)d, which a substring match happily read as a chuck
# speed and answered with 1308 for a process-module number that can only be
# 1 or 2.
_QUANTITIES: Tuple[Tuple[Tuple[str, ...], float, float, int], ...] = (
    (("roughness",), 0.10, 4.50, 3),
    (("thickness",), 95.0, 1250.0, 2),
    (("temperature", "temp"), 18.0, 65.0, 1),
    (("pressure", "vacuum"), 900.0, 1050.0, 1),
    (("flow",), 0.50, 12.00, 2),
    (("speed", "rpm", "rotation"), 200.0, 2400.0, 0),
    (("process time", "cycle time", "duration", "elapsed"), 20.0, 180.0, 1),
    (("level",), 5.0, 95.0, 1),
    (("voltage",), 0.0, 24.0, 2),
    (("current",), 0.0, 10.0, 2),
    (("power",), 0.0, 1500.0, 1),
    (("percent", "ratio", "humidity"), 0.0, 100.0, 1),
    (("counter", "count", "cycles"), 0.0, 5000.0, 0),
    (("time",), 20.0, 180.0, 1),
)

_SEPARATORS = re.compile(r"[^A-Za-z0-9]+")
_LOWER_TO_UPPER = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_ACRONYM_TO_WORD = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")


def _words(dv_name: str) -> list[str]:
    """Split a DV name into lowercase words.

    Handles the three shapes profiles use interchangeably:
    pm1N2ChuckFlowMinWafer, PM1_N2_CHUCK_FLOW_MIN, pm1/n2-chuck-flow-min.
    """
    spaced = _SEPARATORS.sub(" ", dv_name)
    spaced = _LOWER_TO_UPPER.sub(" ", spaced)
    spaced = _ACRONYM_TO_WORD.sub(" ", spaced)
    return [word for word in spaced.lower().split() if word]


def _unit_sample(seed: str) -> float:
    """A stable value in [0, 1) for this seed.

    blake2b rather than hash(): PYTHONHASHSEED randomises str hashing per
    process, which would make the same lot produce different telemetry on
    every run and defeat the point of being reproducible.
    """
    digest = hashlib.blake2b(seed.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") / float(1 << 64)


def _measurement(words: Sequence[str]) -> Tuple[str, float]:
    """Return (key naming the measurement itself, scale for its aggregate).

    The aggregate and scope words are dropped so the Min/Avr/Max slots of one
    measurement share a key, hash to the same sample, and therefore agree.
    """
    scale = 1.0
    kept = []
    for word in words:
        if word in _AGGREGATES:
            scale = _AGGREGATES[word]
            continue
        if word in _SCOPES:
            continue
        kept.append(word)
    return " ".join(kept), scale


def telemetry_value(
    dv_name: str, seed_parts: Sequence[object] = ()
) -> Optional[float]:
    """A credible value for one process variable, or None if unrecognised.

    None means "this name says nothing about what it measures" and the caller
    should keep its own default; it is never confused with a real zero.
    """
    words = _words(dv_name)
    if not words:
        return None
    phrase = " ".join(words)
    base_key, scale = _measurement(words)
    for fragments, low, high, decimals in _QUANTITIES:
        if not any(_contains_phrase(phrase, fragment) for fragment in fragments):
            continue
        seed = "|".join([str(part) for part in seed_parts] + [base_key])
        value = low + (high - low) * _unit_sample(seed)
        value *= scale
        value = min(max(value, low), high)
        if decimals == 0:
            return int(round(value))
        return round(value, decimals)
    return None


def _contains_phrase(phrase: str, fragment: str) -> bool:
    """Whole-word containment: "flow" matches "chuck flow min", not "airflowy"."""
    return f" {fragment} " in f" {phrase} "
