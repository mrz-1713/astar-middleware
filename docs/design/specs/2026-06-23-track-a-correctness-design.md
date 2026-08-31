# v2 Track A — Correctness Fixes

**Date:** 2026-06-23
**Status:** Draft, pending user review
**Scope:** First of three v2 sub-projects. Tracks B (hardening) and C (ops surface) get their own specs after this one ships.

## Goal

Close the four data-correctness gaps from the v1 edge-case audit so the middleware reports the right load_port on every event, doesn't leak memory on orphaned pre-lot events, doesn't flood Linkstuffs during alarm storms, and stops publishing empty SVID samples when a tool's HSMS session is dead.

## Scope

In scope (this spec):

1. **Stateful CtrlJob/Carrier → LP router** — PM-chamber events whose CEID doesn't encode a load port get attributed to the correct LP via in-memory job tracking.
2. **`_pending_pre_lot` TTL** — pre-lot row buffer in `csv_store.py` discards rows older than the configured TTL so a never-arriving `lot_start` can't grow memory unbounded.
3. **Alarm rate limiter** — per-machine throttle on S5F1 storms; once a threshold is exceeded, emit a single `alarm_storm` summary event and silently drop further alarms in the window.
4. **SVID-poll dead-session suppression** — the SVID admin thread refuses to publish empty SVID samples when the session reports `is_connected=False`.

Out of scope (deferred):

- Full SEMI E90 substrate tracker (per-wafer LP attribution). Required only when two LPs run truly concurrent jobs and PM events don't carry CtrlJobID — flagged as v2.B follow-up.
- Persisting job-tracker state across restarts. We accept brief post-restart "NA" routing.
- Ops-surface features (health endpoint, REST admin, hot-reload). That's Track C.

## Architecture

### New module: `eap_middleware/job_tracker.py`

Single class `JobTracker` holding per-machine state:

```python
@dataclass
class _MachineState:
    active_lp: Optional[str] = None       # most recently activated LP
    ctrl_jobs: Dict[str, str] = field(...)# CtrlJobID -> LP
    lp_history: List[str] = field(...)    # stack of activations for fallback

class JobTracker:
    def __init__(self): ...
    def note_event(self, machine_id: str, profile: MachineProfile,
                   ceid: int, data: Mapping[str, Any]) -> None: ...
    def lookup_lp(self, machine_id: str, ceid: int,
                  data: Mapping[str, Any]) -> Optional[str]: ...
```

Thread-safety: one `threading.Lock` per machine_id, acquired around all reads/writes for that machine. Lookups during one event are fast (dict reads); contention is negligible.

State lives in memory only. On middleware restart, all state is empty; the next carrier-arrival event repopulates it. Brief post-restart "NA" routing is acceptable per design call.

### Mapper integration

`CanonicalMapper.from_secs_event()` gains two new steps:

```python
# Existing: V[] decode + raw_event resolve + ceid_load_port fallback
load_port = ...  # current v1 logic

# NEW step 1: feed lifecycle events to the tracker (side effect)
self.tracker.note_event(machine.endpoint_id, self.profile, ceid, data)

# NEW step 2: if still empty AND event is a chamber/PM event, ask tracker
if not load_port and ceid in self.profile.chamber_event_ceids:
    load_port = self.tracker.lookup_lp(machine.endpoint_id, ceid, data) or ""

# Existing: "" -> "NA" bucket downstream
```

`JobTracker` is constructed once in `EapMiddlewareService.__init__` and passed to all `CanonicalMapper` instances via the service's `_mapper()` factory. No new dependency injection plumbing — the existing factory already builds per-event mappers.

### Profile additions

`MachineProfile` gains one new field:

```python
chamber_event_ceids: FrozenSet[int] = field(default_factory=frozenset)
```

DaVinci's set: `{3140002, 3140003, 3140004, 3140005, 3140007}` (all `PM1/*` events). SPTS's set: all `PMxRecipeStart/End`, `PMxRecipeStepStart/End`, `PMxWaferIn/Out` CEIDs from Section 7. PTIQ's set: empty (no specific CEIDs known; generic events already carry port info or fall through to the active_lp pointer).

### Lifecycle event → state transitions

| CEID family | Transition |
|---|---|
| `LP1/CarrierArrived` (DaVinci 3160001) | `active_lp = "1"`, push to `lp_history` |
| `LP2/CarrierArrived` (DaVinci 3170001) | `active_lp = "2"`, push to `lp_history` |
| `ControlJob:Selected-Executing` (DaVinci 3200017) | extract `CtrlJobID` from V[0]; `ctrl_jobs[id] = active_lp` if `active_lp` is set |
| `ControlJob:Completed-NoState` / `Executing-Completed` | `ctrl_jobs.pop(id, None)`; if popped LP == `active_lp`, demote to next from `lp_history` |
| `LP1/CarrierDeparted` / `LP2/CarrierDeparted` | remove that LP from `lp_history`; if it was `active_lp`, demote |
| SPTS `MBCStart1` (330), `MBCStart2` (331) | `active_lp = "1"` or `"2"` |
| SPTS `MBCComplete1` (336), `MBCComplete2` (337) | demote that LP |

These transitions live in a vendor-specific dispatch dict on the profile (`ceid_state_transitions`) to keep `JobTracker` itself vendor-neutral.

### Lookup priority

`JobTracker.lookup_lp(machine_id, ceid, data)`:

1. If the event payload carries `CtrlJobID` (post-merge from V[] decoder) and that ID is in `ctrl_jobs`, return that LP. Highest confidence.
2. Otherwise return `active_lp`. Best-effort.
3. If neither exists, return `None` (caller routes to "NA").

### CSV pre-lot TTL

`PerLotCsvWriter._pending_pre_lot` becomes a `Dict[key, List[Tuple[datetime, CsvRow]]]`. New config field `pre_lot_ttl_sec: float = 3600.0` (1 hour default). On every `append()` call, before the early-return at line 61, prune expired entries for the current key.

Per-key entry cap: hard limit of 200 rows. If exceeded, drop the oldest and log `WARN`. Prevents pathological growth even within the TTL window.

### Alarm rate limiter

New class `AlarmRateLimiter` in `eap_middleware/alarms.py`:

```python
class AlarmRateLimiter:
    def __init__(self, max_per_sec: int = 50, window_sec: float = 1.0): ...
    def admit(self, machine_id: str) -> bool: ...
    def summary_due(self, machine_id: str) -> Optional[int]: ...
```

`EapMiddlewareService._on_alarm` consults `admit()` before invoking the mapper. When admit returns `False`, drop the alarm. Every `window_sec`, if any machine had drops, emit a synthetic `alarm_storm` canonical event with `event_type="alarm"`, `secs_raw_event="AlarmStormSummary"`, and `raw_payload={"dropped_count": N, "window_sec": W}`. Goes through the normal publisher path.

Default: 50 alarms/sec/machine. Configurable per machine via new optional `alarm_rate_limit` field on `MachineConfig` (None = no limit).

### SVID poll dead-session suppression

[service.py:196](eap_middleware/service.py:196) currently does:

```python
values = session.request_svids([...])
if values:
    self.publisher.queue_event(mapper.svid_event(machine, values))
```

`request_svids` already returns `{}` when `is_connected=False`, and the existing `if values:` guard already prevents the empty publish. **Audit was incorrect on this point.** The remaining concern is that some hosts return `{svid: None}` for partial-failure cases — change the guard to:

```python
values = session.request_svids([...])
clean = {k: v for k, v in values.items() if v is not None}
if clean:
    self.publisher.queue_event(mapper.svid_event(machine, clean))
```

Tiny fix, listed for completeness.

## Data flow

```
S6F11 (CEID=3140002, V=[WaferID,LotID,RecipeName])
        │
        ▼
gateway/host.py._parse_event_data
   - extracts CEID
   - extracts raw V[] as data["_v_raw"]
        │
        ▼
SecsMachineSession.event_callback
        │
        ▼
EapMiddlewareService._on_secs_event
        │
        ▼
CanonicalMapper.from_secs_event
   1. merge V[] using ceid_dv_layout (v1)
   2. resolve raw_event + canonical event_type (v1)
   3. resolve load_port via payload / ceid_load_port (v1)
   4. NEW: tracker.note_event(...)  // updates state
   5. NEW: if no load_port AND ceid in chamber_event_ceids:
            load_port = tracker.lookup_lp(...)
        │
        ▼
CanonicalEvent → csv_store + publisher (unchanged)
```

## Error handling

- `JobTracker` swallows lookup errors (returns `None`) so a tracker bug can never block event publishing.
- Vendor state-transition dispatch wrapped in try/except; on error, log WARN with CEID + machine, continue. State may be momentarily wrong but events still flow.
- `AlarmRateLimiter` is purely additive — if its internal state corrupts, worst case is more alarms get through, never fewer.
- TTL pruning runs inside `append()` so it can't deadlock or starve.

## Testing

New file `tests/test_job_tracker.py`:

1. `test_carrier_arrival_sets_active_lp` — fire `LP1/CarrierArrived`, lookup returns "1"
2. `test_ctrl_job_id_resolves_to_lp_when_carrier_arrived_first` — full sequence, verify `CtrlJobID` lookup wins over `active_lp`
3. `test_concurrent_carriers_use_ctrl_job_disambiguation` — LP1 carrier + LP2 carrier + two `Selected-Executing` events with distinct `CtrlJobID`s; assert PM events with each `CtrlJobID` route to the correct LP
4. `test_pm_event_falls_back_to_active_lp_when_ctrl_job_unknown` — chamber event with no CtrlJobID gets active_lp
5. `test_carrier_departure_demotes_active_lp` — LP1 then LP2 arrive, LP2 departs, active_lp falls back to LP1
6. `test_post_restart_lookup_returns_none_until_carrier_arrival` — fresh tracker returns None for unknown machine

New file `tests/test_csv_pre_lot_ttl.py`:

7. `test_pre_lot_entries_pruned_after_ttl` — append events without lot_id, advance clock past TTL, append more, verify pruned
8. `test_pre_lot_hard_cap_drops_oldest_and_warns` — push 201 entries, verify entry 1 dropped and WARN logged

New file `tests/test_alarm_rate_limiter.py`:

9. `test_alarms_below_threshold_pass_through`
10. `test_alarms_above_threshold_throttled_and_summarized`
11. `test_per_machine_isolation` — storm on machine A doesn't throttle machine B

Updates to existing tests:

- `test_edge_case_fixes.py::test_concurrent_lots_on_different_load_ports_get_separate_csv_buckets` — extend with a PM-chamber event that routes correctly to its originating LP via the tracker.

Target: keep the 48-passing baseline; add ~11 new tests; full suite stays under 12 seconds.

## Migration / Backwards Compatibility

No schema changes. Profile additions are optional (`chamber_event_ceids` defaults to empty set; `ceid_state_transitions` defaults to empty dict). Existing tests pass without modification because the mapper's new tracker call is a no-op when state is empty.

`MachineConfig.alarm_rate_limit` defaults to `None` → no rate limiting → identical v1 behavior.

`MiddlewarePaths` gains `pre_lot_ttl_sec` default `3600.0` → existing configs work unchanged.

## Known Limitations (intentional)

1. **True multi-LP concurrent processing without `CtrlJobID` in PM events** — falls back to `active_lp` which can only point to one LP. Mitigation: documented; SEMI E90 substrate tracker scoped as v2.B.
2. **Brief post-restart "NA" routing** — accepted per design call; alternative was SQLite persistence which adds complexity for marginal benefit.
3. **State-machine vendor coverage** — DaVinci and SPTS get transition tables in this spec; PTIQ relies on `active_lp` only since the spec is generic. Customer's actual PTIQ EIB may need transition entries added later.

## Open Questions

None — all design decisions have explicit answers from the brainstorming session.

## Acceptance Criteria

- All 48 v1 tests still pass
- 11 new tests added and pass
- `JobTracker` reports correct LP for ≥99% of DaVinci/SPTS PM events in a stress test that runs 1000 mixed events through both profiles
- Alarm rate limiter caps alarms at the configured threshold ±5%
- Memory of the `_pending_pre_lot` buffer stays under 100 KB after running 24 simulated hours with orphan events
