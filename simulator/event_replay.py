"""Replay every collection event a profile documents, in CEID order.

The NexGen MG lot script fires 31 of the profile's 243 CEIDs. Whole bands -
`gem300` project/control-job state (80 CEIDs), `metrology_aux` chamber state
(72), most of `core_gem` - never fire at all, so a middleware decode bug in
those reports cannot surface before the tool is on the fab floor.

This module does not hand-code 243 emissions. The profile already carries
every CEID and its report's data-variable list (`ceid_dv_layout`, overlaid
from `EventSubscription.json`), so the sweep walks that data instead. Adding
a CEID to the subscription file adds it to the sweep with no code change.

The sweep is physically incoherent **by design**: `EquipmentOffline` fires
while a lot runs and sequential state transitions arrive out of order. It is a
decode and subscription sweep, not a behaviour model - the lot script remains
the behaviour model.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Sequence, Tuple


# ponytail: the subscription file carries DV names but no SECS types, so type
# is inferred from the name. All 82 distinct names in the MG profile are
# covered by the rules below. If a vendor ever ships explicit DVID types,
# prefer those and delete this - see `event_definitions.dvid_types` in
# production.yaml, which already overrides types for the lot script.
_FLOAT_HINTS = (
    "temp", "flow", "speed", "press", "avr", "rate", "thick", "volt", "curr",
)
_INT_HINTS = (
    "count", "slot", "port", "capacity", "size", "number", "qty", "index",
    "state", "status", "mode", "result", "code", "level",
)


def dv_value(name: str) -> Any:
    """A deterministic, type-plausible value for one data variable.

    Deterministic so a test can assert an exact V[] rather than merely a
    length. `int` becomes U4 and `float` becomes F4 in the simulator's
    `_typed`, which is what the middleware's `_get_int` / `_get_float`
    readers expect - both also tolerate ASCII, so a wrong guess here degrades
    to a readable value rather than a decode failure.
    """
    lowered = name.lower()
    # Chemistry summaries (Med1TempAvrLot, DiFlowMaxLot, ChuckSpeedMinLot...)
    # are the largest family and are always analogue. Check before the integer
    # hints, because "Min"/"Max" names also contain no integer hint at all.
    if any(hint in lowered for hint in _FLOAT_HINTS) or lowered.endswith(
        ("maxlot", "minlot")
    ):
        return round(20.0 + (_stable_hash(name) % 800) / 10.0, 1)
    # "PortID" and "SlotID" end in "id" but the mapper reads them as integers
    # (`load_port_keys = ["PortID", ...]` in mapper.py), so the integer hints
    # deliberately win over the identifier rule below.
    if any(hint in lowered for hint in _INT_HINTS):
        return 1 + _stable_hash(name) % 4
    if lowered.endswith("id") or "name" in lowered:
        return f"{name}-{1 + _stable_hash(name) % 9:02d}"
    if "time" in lowered or "clock" in lowered:
        return "20260817120000"
    return f"{name}-value"


def _stable_hash(text: str) -> int:
    """Hash that does not move between runs.

    `hash()` is salted per process (PYTHONHASHSEED), which would make the
    generated V[] differ run to run and any exact-value assertion flaky.
    """
    total = 0
    for char in text:
        total = (total * 31 + ord(char)) & 0xFFFFFFFF
    return total


def replay_plan(
    profile: Any,
    values_for: Optional[Callable[[int], Sequence[Any]]] = None,
) -> List[Tuple[int, Tuple[Any, ...]]]:
    """Every CEID the profile documents, paired with its report's V[].

    `values_for` lets a caller supply its own builder. `ProfileSimulator`
    passes `_values_for`, which knows the live lot context (real lot ID,
    recipe, carrier, port), so the sweep reuses the same values the lot script
    would send rather than this module's name-based guess. Callers with no
    such context - the standalone MG simulator - fall back to `dv_value`.

    CEIDs whose report list is empty yield an empty V[]. That is not a gap:
    the MG profile deliberately enables events such as `portNCasPlaced`
    without linking a report, because the port is already identified by the
    CEID itself, and an empty RPTID list in S2F35 means "delete this link".
    `_send_s6f11` omits the report block entirely for an empty V[].
    """
    layout = profile.ceid_dv_layout
    value_builder = values_for
    if value_builder is None:
        def default_values_for(ceid: int) -> Sequence[Any]:
            return [dv_value(name) for name in layout.get(ceid, ())]

        value_builder = default_values_for
    return [
        (ceid, tuple(value_builder(ceid)))
        for ceid in sorted(profile.ceid_aliases)
    ]


def replay(
    profile: Any,
    emit: Callable[[int, Sequence[Any]], bool],
    ceids: Sequence[int] = (),
    values_for: Optional[Callable[[int], Sequence[Any]]] = None,
) -> int:
    """Emit the whole plan through `emit`, returning how many were sent.

    Stops early when `emit` returns False, which is how the simulator signals
    a dropped connection or a stop request - a sweep that keeps going after
    the link is down would log 200 spurious failures.
    """
    wanted = set(int(ceid) for ceid in ceids)
    sent = 0
    for ceid, values in replay_plan(profile, values_for):
        if wanted and ceid not in wanted:
            continue
        if not emit(ceid, values):
            break
        sent += 1
    return sent


def demo() -> None:
    """Self-check: run `python -m simulator.event_replay`."""
    from eap_middleware.profiles import (
        ProfileRegistry,
        profile_with_subscription_file,
    )

    base = ProfileRegistry().get("nexgen_mg_series")
    profile = profile_with_subscription_file(
        base, base.event_subscription_path
    )
    plan = replay_plan(profile)

    assert len(plan) == len(profile.ceid_aliases), "plan must cover every CEID"
    assert len(plan) == 243, f"MG profile should document 243 CEIDs, got {len(plan)}"

    with_values = [(c, v) for c, v in plan if v]
    # 114 before the process-metric bands were added; 26 step / metrology /
    # wafer-aggregate events that the manual gives data variables for used to
    # be linked with no report at all. Deliberately pinned: a change here means
    # NEXGEN_MG_REPORTS moved and the subscription needs regenerating.
    assert len(with_values) == 140, (
        f"140 MG CEIDs carry a report, got {len(with_values)}"
    )

    # Every slot filled: an empty V[] slot would be sent as ASCII "" and read
    # back as a silently missing data variable.
    for ceid, values in with_values:
        assert all(v not in (None, "") for v in values), f"CEID {ceid} has a blank slot"

    # CEID 4 (ProcessingStarted) is the shape the CSV path depends on.
    values = dict(plan)[4]
    assert len(values) == 5, f"ProcessingStarted has 5 DVs, got {len(values)}"
    assert isinstance(values[3], int), "PortID must be an integer for the mapper"

    assert dv_value("Med1TempAvrLot") == dv_value("Med1TempAvrLot"), "must be stable"
    assert isinstance(dv_value("Med1TempAvrLot"), float), "chemistry is analogue"
    assert isinstance(dv_value("LotID"), str), "identifiers are ASCII"

    emitted: List[int] = []
    sent = replay(profile, lambda ceid, _values: (emitted.append(ceid), True)[1])
    assert sent == 243 and len(emitted) == 243, f"sent {sent}"

    # A falsy emit must stop the sweep rather than log 242 more failures.
    stopped = replay(profile, lambda ceid, _values: False)
    assert stopped == 0, f"expected an immediate stop, sent {stopped}"

    print(f"OK: {len(plan)} CEIDs, {len(with_values)} with reports")


if __name__ == "__main__":
    demo()
