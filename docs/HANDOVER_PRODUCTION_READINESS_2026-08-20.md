# Handover — Production-Readiness Hardening (2026-08-20)

Intended for another agent / session picking this up. Everything below is
uncommitted; nothing was committed.

## 0. What "it" is

Repo: `/Volumes/Backup/astar-middleware-main` — "ASTAR SECS/GEM EAP Middleware".

Branch: `feat/nexgen-mg-series-profile`. The working tree already carried ~10k
lines of uncommitted changes **before** this session (the "second pass" fixes
F-7 … F-12 described at the end of `docs/DEEP_AUDIT_2026-08-20.md`). This
session's edits are layered on top of that, also uncommitted.

The ask was: *"make it production grade ready — no mismatch, no bugs,
comprehensive logging"*. Four parallel review agents audited the whole diff;
their findings were triaged and the clear bugs/logging gaps fixed. This
document records what changed, what did not, and what is still open.

## 1. Baseline (before this session's edits)

- `pytest -q` → **643 passed, 1 failed, 1 skipped, 5 deselected** (223 s).
  The one failure is flaky — see §6.
- `ruff --select=F,E9` on production packages: **clean** (12 pre-existing `F541`
  "f-string without placeholders" in `scripts/e2e_davinci_live.py` and
  `scripts/smoke_linkstuffs.py` — not bugs, not touched).
- Full `ruff` (all rules): ~986 findings, overwhelmingly `UP006/UP045/UP035`
  (modern type-annotation style) plus 30× `DTZ005` (naive `datetime.now()`).
  These are style/landmine, **not** fixed — see §5.

## 2. Files changed this session (27)

```
eap_middleware/secs_runtime.py   eap_middleware/service.py
eap_middleware/config.py         eap_middleware/mapper.py
eap_middleware/probe.py          eap_middleware/csv_store.py
eap_middleware/legacy_api.py     eap_middleware/linkstuffs.py
eap_middleware/logging_setup.py  eap_middleware/journal.py
eap_middleware/outbox.py         eap_middleware/job_tracker.py
eap_middleware/tkwidgets.py
gateway/host.py                  gateway/event_subscription.py
simulator/equipment.py           simulator/runner.py
simulator/nexgen_mg_simulator.py simulator/profile_simulator.py
simulator/secsgem_equipment.py
gui/app.py                       gui/model.py
simulator_gui/app.py
scripts/band_subscriptions.py
packaging/mg_simulator/MGSimulator.spec
deploy/install.ps1               deploy/Setup.ps1
```

## 3. What was fixed (this session)

### P1 — real bugs / mismatches

1. **`secs_runtime.py`** — `_provision_after_connect` now captures `host =
   self.host` **once** up front and uses it for the S1F17 ON-LINE request. It
   previously re-read `self.host` in that block, so a stop/start mid-provision
   could issue S1F17 on a retired host (or interleave with the next
   generation's S2F33/35/37).
2. **`service.py`** — `machine.hsms_timers` added to `_restart_signature`.
   Without it, editing the new per-machine `hsms_timers:` in `production.yaml`
   while the service ran was silently ignored (no hot-reload restart).
3. **`csv_store.py`** — the durable mirror task is now enqueued **before**
   `_release()` marks the journal rows CSV-done. The old order left a crash
   window where the local CSV existed, the rows were no longer replayable, and
   the network copy was silently skipped.
4. **`simulator/profile_simulator.py`** — the carrier check no longer treats
   `SubstLocID`/`SubstSubstLocID` (E90 slot locations) and `ECID` (SPTS
   equipment-constant id) as carrier ids; those now route to the location/port
   branch. `endswith("cid")` is kept for the MG `Cid` abbreviation but excludes
   `locid` and `ecid`.
5. **`simulator/nexgen_mg_simulator.py`** — `_handle_s2f37` override now sets
   `_event_reporting_configured` (and maintains `_disabled_events`). Before,
   `_is_event_enabled()` always returned `True`, so the subset-enable filter
   was dead and the "CEID fired but NOT enabled" INFO never fired.
6. **`scripts/band_subscriptions.py`** — banding is now DVID-aware: it calls
   `gen_spts_subscription._band_for(ceid, dvids)` instead of the family-only
   `_spts_band(ceid)`. The old code flattened SPTS CEID 858 (172 DVIDs) back
   from its `cassette_statistics_ceid858` isolation band into the family band.
   Re-run verified idempotent: `0 events, 0 reports rebanded` on both targets.
7. **`gui/app.py`** — `_on_stop_service` keeps `self._service` when
   `service.stop()` raises (previously dropped it unconditionally, orphaning a
   possibly-still-running service with both buttons disabled).
8. **`simulator_gui/app.py`** — address detection now has a pending-rerun guard
   (`_detect_listeners` vs `_detect_running_listeners`), so a network-scan
   result is never overwritten by a slower earlier detection run.

### P2 — logging / robustness / mismatches

9.  **`service.py`** — `_outage_since` / `_outage_escalated` cleared in
    `_stop_machine` (was leaking across stop/start, suppressing escalation and
    producing a bogus "reconnected after Ns down" log).
10. **`service.py`** — CSV-sink failure logging is rate-limited: one full
    traceback per endpoint per 60 s, then a suppressed count
    (`_CSV_FAIL_LOG_INTERVAL_SEC`). Previously one traceback per collection
    event while the sink was down.
11. **`config.py`** — profile-sourced HSMS timers now pass the same name/1..120
    validation as machine overrides (a bad profile value is now a config error
    at load, not a runtime `ValueError` at session start).
12. **`mapper.py`** — `svid_event`'s id→name reverse map is first-wins, so SVID
    4306 keeps its canonical `mapResultLastMap` label instead of the
    report-alias `SlotMapGem`.
13. **`probe.py`** — `host.disable()` teardown failure is logged (was
    `except: pass`); also the panel now resolves probe timers from profile +
    override (see #20) so a "Test connection" proves the same link the service
    opens.
14. **`legacy_api.py`** — permanent HTTP failures are dead-lettered after 5
    attempts (matching `linkstuffs_http`), and per-item failures are logged.
    Previously the head row blocked the machine's whole partition forever and
    was invisible in logs.
15. **`linkstuffs.py`** — QoS0 publish rejection (`info.rc != 0`) is logged.
16. **`logging_setup.py`** — machine-file handler level now `.upper()`s the
    configured level, matching `configure_logging`.
17. **`journal.py`** — `ALTER TABLE` migration checks `PRAGMA table_info` first
    and re-raises genuine failures (was blanket `except: pass`).
18. **`journal.py`** — `mark_csv_done` clears `csv_reason`; removed dead
    `lease_mirror`/`release_mirror`; corrected stale `pending_mirrors` docstring.
19. **`outbox.py`** — backoff ceiling is now reachable
    (`min(300, 2 ** min(attempts, 9))`; the old `min(attempts, 8)` made the
    300 s cap dead code).
20. **`job_tracker.py`** — added `_extract_chamber` and made `note_event` learn
    chamber→port from the payload (falling back to the profile map), fixing the
    asymmetry where a payload-stated chamber blocked the `active_lp` fallback
    but was never learnable.
21. **`gateway/event_subscription.py`** — `setup_subscriptions` sets an
    `abandoned` flag; an aborted (session-superseded) subscription no longer
    logs "completed successfully".
22. **`gateway/host.py`** — the "S6F11 … [ACKC6=0]" INFO now logs only when the
    event callback actually stored the event; the no-callback (probe) case logs
    DEBUG "acknowledged but not stored".
23. **`simulator/runner.py`** — removed the two runtime `setattr(... "t5", …)`
    calls that re-conflated T5 with recovery backoff (the exact conflation
    `build_settings()` was written to eliminate).
24. **`simulator/{secsgem_equipment,nexgen_mg_simulator,profile_simulator}.py`
    mains** + **`packaging/mg_simulator/MGSimulator.spec`** — standalone mains
    now pass `DEFAULT_HSMS_TIMERS` (so T7=10, not secsgem's library 8), with a
    lazy `from gateway.host import DEFAULT_HSMS_TIMERS`; `gateway.host` and its
    submodules added to the MG spec's hidden imports.
25. **`simulator/nexgen_mg_simulator.py`** — S2F35 zero-length DATA now clears
    all links (and a per-event empty RPTID list deletes that link), matching
    the base class and the S2F33 delete-all fix.
26. **`simulator/equipment.py`** — spool-drain race closed two ways: the worker
    clears `_spool_drain_worker` atomically with its empty-spool check, and
    `_queue_spooled` schedules a drain when the spool goes empty→non-empty.
27. **`gui/model.py`** — `ProbeTarget` carries `hsms_timers` resolved from
    profile + override (`_probe_hsms_timers`), so the panel probe does not fall
    back to library defaults.
28. **`gui/app.py`** — same pending-rerun detection guard as simulator_gui; and
    "waiting for service to apply configuration…" is only shown when a service
    is live/owned (no more permanent wait on a fresh install).
29. **`eap_middleware/tkwidgets.py`** — wheel bind/unbind moved from the canvas
    to the whole tab frame (was unbinding over `.body` via NotifyInferior), and
    the key-scroll exempts buttons/checkbuttons/radiobuttons/notebook so those
    keep their arrow/Home/End navigation.
30. **`deploy/install.ps1`** — both Python version checks guard the
    stderr→NativeCommandError trap (`$ErrorActionPreference` toggle); the
    bundled installer's exit code is now checked.
31. **`deploy/Setup.ps1`** — `(Get-Command …).Source` no longer throws under
    `Set-StrictMode -Version Latest` when `pythonw.exe` is not on PATH.

## 4. Verified / re-checked this session

- All 27 changed files `py_compile` clean.
- `ruff --select=F,E9` clean on the changed files.
- `scripts.band_subscriptions` re-run is a no-op on the two committed SPTS /
  DaVinci curated files (bands preserved, isolation intact).
- The 5 shipped `output/*/EventSubscription*.json` files are internally
  band-consistent (no orphaned reports, no cross-band report/event links, no
  duplicate CEID/RPTID) and regenerate byte-identical from their generators.

## 5. Deliberately NOT changed (decisions, not bugs)

- **`gateway/host.py` `retire()` unbounded `disable()`** — explicitly a
  deliberate non-fix (see `docs/DEEP_AUDIT_2026-08-20.md` F-6 revert): bounding
  it on a worker thread caused a reproducible pytest deadlock in secsgem 0.3.0.
  Do not "fix" without reproducing that.
- **Naive `datetime.now()` (`DTZ005` ×30)** — the D-9 "mixed naive/aware" landmine.
  No live defect; `timestamp_ms()` happens to be correct for both. Cross-cutting;
  needs a decision before touching.
- **`UP006/UP045/UP035` etc. type-annotation modernization** — style only, not bugs.
- **SVID 4306 dual name in `profiles.svids_by_name`** — left in place; only the
  telemetry label was made deterministic. The `SlotMapGem` entry is required for
  the CEID-145 report decode path and simulator S1F3 answers.

## 6. Remaining open items (for the next agent)

### Must still be confirmed

- ~~**Re-run the full suite to green.**~~ **Done 2026-08-21** — see §9.
  `647 passed, 1 skipped, 5 deselected` in 138 s, zero failures.

### Real P1 still open

- ~~**`csv_store.py` — unbounded in-memory lot-buffer growth.**~~ **Closed
  2026-08-21** by option (b), journal-backed eviction. See §9.

### Deferred P2s (low value, listed for completeness)

- `service.py` `_start_machine` "Failed to start %s: %s" logs without `exc_info`.
- `service.py` outbox-maintenance stop failure logged at DEBUG.
- `service.py` supervisor/stop race (expired 2 s join can leave a never-stopped
  session) and mirror-worker-outlives-stop 300 s lease — pre-existing shapes.
- `simulator_gui/app.py` Stop button has no timeout (a wedged runner leaves Stop
  disabled forever).
- `gui/app.py` `_on_test_linkstuffs` submits a command file with no
  service-liveness check.
- `scripts/gen_*_subscription.py` generators write non-atomically (only
  `band_subscriptions.py` was made atomic).
- `nexgen_mg_simulator.py` dropped-event path returns `True` (deliberately-dropped
  reads as "sent"); arguable, left as-is.

## 7. Known flaky test

`tests/test_mg_simulator_e2e.py::test_mg_simulator_refused_band_leaves_other_bands_reporting`

- Failed once in the full-suite run with `No response to S2F33` / `No response to
  S2F35` while the box was heavily loaded (4 review subagents + suite).
- Passes in isolation in 3.5 s.
- Root cause is HSMS subscription round-trips timing out under CPU starvation, not
  a logic bug. If it flakes again in CI, run it isolated before treating it as a
  real regression.
- It passed in both clean full runs on 2026-08-21.

## 8. Caveats

- `deploy_out/` is a **stale generated snapshot** — do not edit (its copies still
  call the removed `lease_mirror`, which is expected).
- `output/` is generated; regenerate rather than hand-edit. The re-bander must be
  re-run after any generator change (`python -m scripts.band_subscriptions`).
- Everything remains uncommitted. The next agent should `git diff` the 27 files
  above to see the exact changes, then consider committing per logical group.

---

# Session 2026-08-21 — closing out §6

Still uncommitted, layered on everything above.

## 9.1 The suite is green

`.venv/bin/python -m pytest -q` → **647 passed, 1 skipped, 5 deselected** (138 s).
`ruff --select=F,E9` on `eap_middleware gateway gui simulator simulator_gui`:
**clean**.

§6 was right that the 643/1 baseline predated later edits. It hid **three real
failures**, all fallout from this repo's own second-pass fixes, and all of them
stale *tests* rather than bad production code:

1. `test_simulator_runner.py::test_active_connection_retry_limit_stops_after_retry`
   asserted `settings.timeouts.t5 == 2` — i.e. it pinned exactly the T5/backoff
   conflation that §3 #23 removed. Now asserts T5 equals
   `runner.resolved_hsms_timers()["t5"]`, so it pins the *contract* (a protocol
   timer set once in `build_settings()`, never retuned by restart backoff)
   rather than a number.
2. `test_davinci_packaged_runner_e2e.py::test_active_runner_drains_spool_and_resumes_partial_lot_after_disconnect`
   waited 20 s for an active reconnect. With #23 in place T5 is honestly 10 s
   (it used to be rewritten down to ~1 s), so secsgem's connect-separation wait
   made 20 s a coin flip. The test is about the spool preserving a partial lot,
   not about how long connect separation is, so its simulator now runs
   `hsms_timers={"t5": 1}` — which also gives the per-simulator override its
   first test.
3. `test_simulator_protocol_fidelity.py::test_events_spooled_while_down_are_delivered_once_communication_returns`
   asserted `_spool_drain_worker is not None` after reconnect. §3 #26 made the
   worker clear that handle atomically with its empty-spool check, so with an
   in-process send the drain can finish before the assertion runs. Now waits on
   the outcome (spool empty, both events retransmitted) instead of the handle.

## 9.2 Test isolation bug found on the way

`test_simulator_protocol_fidelity.py::_with_patchable_comm_state` patched
`communication_state` onto the **class** and never put it back, so every
`EquipmentSimulator` built later in the same pytest process inherited a
`SimpleNamespace` and died in `_on_protocol_disconnected` with
`AttributeError: 'types.SimpleNamespace' object has no attribute 'disable'`.

It was invisible only because pytest collects `test_davinci_*` before
`test_simulator_*`; naming a new file after it alphabetically would have broken
it for no visible reason. Now undone by an autouse `_restore_comm_state`
fixture. Verified by running the two files in the order that used to fail.

## 9.3 The P1: bounded lot buffers (`csv_store.py`)

Taken as **option (b), journal-backed eviction** — nothing is dropped.

The pathology: `_write_and_remove` deliberately keeps the buffer when the local
write raises, so while the sink is down the next lot's rows join the same
buffer and every close re-serialises all of them. Unbounded memory plus O(n²)
work.

The fix rests on machinery that already existed, which is why it needs no new
durability story: a row is only marked `csv_status='done'` by `_release()`
*after* the local file exists; `mark_csv_failed` leaves it `pending`;
`purge_old()` refuses to purge anything pending; `holds()` is the only thing
stopping `_replay_journal` from re-adding it. So dropping a buffer from memory
**without** calling `_release` returns its rows to replay, intact and in seq
order.

- `_evict_buffer(key, buffer)` — pops the buffer, decrements `_seq_refs` only,
  and clears any `_seq_dropped` marker for a seq whose last reference just left
  (the seq goes back to being purely the journal's business; leaving the marker
  would later mark a row dropped that replay had in fact written). Rows with no
  journal seq cannot be rebuilt and are counted and named in the log line.
- `_MAX_ROWS_PER_LOT_BUFFER = 20000`, constructor override `max_lot_rows`.
- **Eviction requires a recorded write failure.** Failures are tracked per
  `(endpoint_id, load_port)` in `PerLotCsvWriter._write_failures`, not on
  `LotBuffer` — the buffer object is replaced by eviction and by `lot_changed`
  while the sink stays broken, and a fresh buffer that forgot the failures
  starts accumulating without a ceiling all over again. (That was a real bug in
  the first cut of this fix; the test caught it.)
- Consequence, and the reason for the gate: **a healthy lot is never evicted**,
  however long it runs. No regression for a genuinely large lot.

`tests/test_csv_buffer_eviction.py` (new, 3 tests) pins:

- a healthy long lot keeps every row — eviction needs a failure;
- with the sink broken, memory stays `<= max_lot_rows + 1` across 200 further
  rows **and** every row is still `csv_status='pending'`;
- after recovery, replaying the pending entries in seq order rewrites every row
  to disk and marks them `done`.

Note for whoever touches the tests: `_filename()` derives from the buffer's
first row timestamp, so fixtures that reuse one timestamp make two lot files
collide through `os.replace()`.

### What this does *not* fix

A lot that never closes while the sink is down still grows: no write is
attempted, so no failure is recorded, so the gate never opens. That is the
inherent cost of per-lot files (a healthy system buffers a whole open lot too)
and is bounded by one lot, not by the outage.

## 9.4 Files changed this session (6)

```
eap_middleware/csv_store.py
docs/HANDOVER_PRODUCTION_READINESS_2026-08-20.md
tests/test_csv_buffer_eviction.py          (new)
tests/test_simulator_runner.py
tests/test_simulator_protocol_fidelity.py
tests/test_davinci_packaged_runner_e2e.py
```

Everything from §3 remains uncommitted too. The deferred P2s in §6 are
untouched and still accurate.
