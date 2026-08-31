# Deep Audit — 2026-08-20

**Scope.** All 14 PDFs and the workbook in `docs/`, read as text *and* as
rendered page images; the full `eap_middleware` + `gateway` + `simulator` +
`simulator_gui` + `gui` + `deploy` tree on `feat/nexgen-mg-series-profile`.

**Relationship to the previous pass.** `VENDOR_CONFORMANCE_AUDIT_2026-08-19.md`
closed 11 vendor findings and 11 engineering findings. This pass does not
re-litigate those. Everything below is new, and every claim was reproduced
against the live tree rather than inferred from reading.

**Baseline.** `pytest -q` → **559 passed, 3 skipped, 5 deselected** (121 s)
before any change. The `slow`-marked 22-machine run passes (113 s).
`ruff --select=F,E9` is clean across all production packages.

---

## 0. Document inventory

1,160 pages, 2,427 embedded images across 14 PDFs plus the DaVinci workbook.
Only four carry interface data; the rest are backup, safety, recovery and
maintenance manuals, confirmed by reading their pages rather than by filename.

| Document | Pages | Interface content |
|---|---:|---|
| NexGen MG Series SECS V1.1.18 | 197 | **Yes** — state models, 243 CEIDs, DVVAL/SV tables, RCMDs, ECs, alarms, two lot-start traces |
| Omega SPTS fxP 200mm SECSII (Cimetrix) | 231 | **Yes** — Table 5 (224 CEIDs), SV/EC/DV tables, spooling, RCMDs, Appendix A control-state dependency |
| SECS-Items MueTec DaVinci 200 MC4/HC1 (xlsx) | — | **Yes** — 282 events, 128 SVs, 121 DVs, 1,035 alarms |
| DaVinci Software Operation Manual EN | 204 | **Partly** — §9.6 Host Interface: parameters, HSMS timer defaults, Offline/Online-Local/Online-Remote semantics |
| TC User Documentation EN | 151 | No — operator/rights/recipe UI |
| TheWizard UM | 156 | No |
| DaVinci User Manual V1.8 | 61 | No |
| DaVinci Maintenance V1.7 / PM Checklist V1.6 / Service 2011 | 116 | No |
| Safety Test V1.3 / Recovery / Macrium ×2 | 33 | No |

Two facts recovered from images that the text layer does not carry:

- **SOM p.127** — the Host Interface parameter screen. Communication Mode
  defaults to `Server` (so the DaVinci is the HSMS passive side and the
  middleware must be active, which is what ships), and the HSMS timers on that
  screen are T3 45 / T5 10 / T6 5 / T7 10 / T8 5 — exactly
  `gateway.host.DEFAULT_HSMS_TIMERS`.
- **SOM p.130** — the three control-state buttons need the
  `Control Fab Host Interface` user right, and **Online Local already gives the
  host every event**; Online Remote additionally disables local carrier and job
  management (p.126). For a read-only middleware, Online Local is the correct
  commissioning state and Online Remote would strand the operator. The DaVinci
  setup guide already says this (`DAVINCI_SECS_GEM_SETUP.md:304`).

---

## 1. Fixed in this pass

### F-1 The MQTT/HTTP outbox never closed its SQLite connections

`SQLiteOutbox` used `with self._connect() as conn:` at all ten call sites.
In Python, `with connection:` is a **transaction** context manager — it
commits and leaves the connection open. `sqlite3.Connection` participates in
reference cycles, so each one survived until the cyclic collector ran.

Measured on the shipped class, 20,000 enqueues:

```
before fix : 156 live sqlite3.Connection objects (4 after a forced gc.collect())
after fix  :   4, flat
```

Each open handle pins that database's `-wal` and `-shm` files and blocks WAL
checkpointing, so the write-ahead log grows instead of being truncated — on a
deployment with one HTTP outbox per machine, across 22 machines. `IngressJournal`
has always used `contextlib.closing`; the outbox now does too.

Regression test: `tests/test_outbox_maintenance.py::test_outbox_does_not_accumulate_open_connections`
(counts live connections *without* forcing a collection — the point is what the
process holds between collections). It fails on the pre-fix code.

### F-2 The simulator ran the wrong HSMS timers, and T5 was the restart backoff

`SimulatorRunner.build_settings()` set exactly one timer:

```python
t5=self.config.recovery.initial_retry_sec,   # default 1
```

Everything else fell to secsgem's library defaults (T3 45, T6 5, **T7 8**, T8 5).
Two consequences:

1. **The rig was permanently mismatched.** secsgem's T7 is 8 s; every shipped
   profile states 10 s (5 s for SPTS). `gateway/host.py` explains why that
   matters — whichever side has the shorter timer declares a communications
   failure while the other still considers the transaction open, and the link
   drops with nothing in either log to point at. The two-VM rig existed to catch
   exactly that class of fault and structurally could not.
2. **Tuning restart backoff silently retuned a protocol timer.** `initial_retry_sec`
   is the runner's own application-level restart interval; its default of 1 s is
   below anything any vendor manual states for T5.

Now: `ConnectionConfig.hsms_timers` (validated 1–120 s per SEMI E37, same rules
as the middleware's own loader), the service passes each simulated machine's own
`hsms_timers` so a loopback pair matches end to end, and the simulator panel
exposes all five as individual fields seeded from the shipped defaults.

Verified end to end — the in-process simulator now mirrors the host side:

```
TOOL_01  spts_fxp_omega       t3 30  t5 5   t6 10  t7 5   t8 6
TOOL_02  davinci_200_mc4_hc1  t3 45  t5 10  t6 5   t7 10  t8 5
TOOL_03  ptiq_secsgem         t3 45  t5 10  t6 5   t7 10  t8 5
TOOL_04  nexgen_mg_series     t3 45  t5 10  t6 5   t7 10  t8 5
```

### F-3 The NexGen simulator acknowledged a subscription reset it never performed

`NexGenMgSimulator` replaces the base class's S2F33/35/37 handlers to add
per-band refusal, and in doing so lost the SEMI E5 rule that a **zero-length
report list deletes every report definition**. It answered `DRACK=0` and kept
everything:

```
report_definitions after "delete all": {1000000004: [11, 12]}   # unchanged
event_links        after "delete all": {4: [1000000004]}        # unchanged
```

The base `EquipmentSimulator` gets this right and has a test for it. This
matters because deleting all reports and links is **steps 2–3 of the MG manual's
own lot-start sequence** (§9.1 and §9.2), so a rig would have signed off a reset
the real tool actually performs. Deleting a single report now also drops the
links that referenced it, rather than leaving a CEID pointing at an RPTID that
no longer exists.

Two regression tests added; both fail on the pre-fix handler.

### F-4 Dead report parsers carrying a DVID map that matches no shipped tool

`gateway/host.py` held 102 unreachable lines — `_parse_v_array`,
`_parse_reports`, `_parse_dvvals` — each with a hardcoded positional field list
(`CLOCK, EQID, LOTID, WAFERID, …`) and a DVID→name map (`1: "CLOCK"`,
`2: "EQID"`, …) invented for the first loopback simulator. No shipped profile
numbers its variables that way, so anything wired back into them would have
relabelled real equipment data silently. Removed, with a note pointing at
`eap_middleware.mapper`, which is where `_reports_raw` is actually decoded.

### F-5 Two smaller correctness items

- `secs_runtime._provision_after_connect` captures `host = self.host` and warns
  in its own docstring that a superseded worker must keep talking to its own
  connection — then read `self.host` for the band-result log, the spool drain and
  the alarm enable. Now consistently the captured handle, with the generation
  re-checked against it.
- `single_instance._process_is_alive` was dead, and `acquire()`'s docstring
  promised a PID liveness check the code never performed. The real mechanism is
  the OS file lock, which is the *better* one — a lock held by a dead process is
  released by the kernel, and a recycled PID cannot lock out a healthy start.
  Function removed, docstring corrected to describe what actually happens.

---

## 2. Found, not changed — these need a decision

Each of these changes on-wire behaviour against commissioned hardware, or is an
operational trade-off rather than a defect. They are measured, not estimated.

### D-1 One rejected VID costs the DaVinci its entire event feed

S2F33 is all-or-nothing: SEMI E5 equipment rejects the whole message when it
detects any error, and `DRACK=4` means "invalid VID". The banding mechanism
exists to contain that. Measured, by rejecting one VID from each report in turn
and taking the worst case:

| Profile | Report bands | Events | Worst case: still enabled | Lost |
|---|---:|---:|---:|---:|
| `davinci200_mc4_hc1` | **1** | 54 | **0 (0%)** | **54 (100%)** |
| `spts_fxp_omega` | **1** | 96 | 42 (43%) | 54 (56%) |
| `nexgen_mg_series` | 31 | 243 | 163 (67%) | 80 (33%) |

The DaVinci — the profile with the full commissioning guide, and the one most
likely to meet real hardware first — has no bands at all, so a single unknown
VID leaves it connected and reporting nothing.

SPTS is a half-measure: its 96 events carry 7 band labels, but **all 43 of its
reports sit in one unnamed band**, so the S2F35 leg is contained and the S2F33
leg is not. Its largest report (CEID 858, `RecipeStepEnd`) carries **172 VIDs**,
165 of them module statistics — the single biggest all-or-nothing message the
middleware sends.

NexGen's `gem300` band is also oversized at 79 reports / 80 events.

The fix is mechanical — label each report with the band of the event that
references it, and split `gem300` — but it changes the message sequence a
commissioned tool has already accepted, so it is your call, not mine.

### D-2 The manual's prescribed opening reset is never sent

Both MG lot-start examples (§9.1 p.170, §9.2 p.183) prescribe the same opening:

```
1. Host establishes communication
2. Host deletes all existing report definitions      <-- not sent
3. Host deletes all existing report links            <-- not sent
4. Host disables all event reports                   <-- not sent
5. Host defines report definitions
6. Host defines report links
7. Host enables events
```

Confirmed against the live subscription manager — on the NexGen profile it sends
31 × (S2F33, S2F35, S2F37), all populated, and **no zero-length reset message at
all**. `disable_all_events()` exists but nothing calls it, and the delete
sequence fires only reactively, on a `DRACK=3` collision.

On a tool that has previously talked to another host — which is the commissioning
case — stale links survive. A CEID left linked to a report the middleware has
just redefined delivers a payload against a layout the mapper no longer expects,
and CEIDs enabled by the previous host keep arriving as `unknown`.

### D-3 SPTS: 14 documented data-carrying events are not subscribed

Diffing each vendor document's own event table against the shipped subscription:

- **DaVinci** — 228 of 282 workbook events unsubscribed, and **every one of them
  declares no Valid Variables**. Nothing is lost. The previous audit's "100% of
  declared pairs" holds.
- **NexGen** — 243 of 243 CEIDs mapped.
- **SPTS** — 128 of 224 Table 5 events unmapped. 114 declare no DVs (door,
  lamp, buzzer, RF on/off, mode and process-state transitions). **14 declare a
  full 7-variable report** and are the only data-carrying events unsubscribed
  anywhere across all three vendor documents:

  | CEIDs | Events | DVs each |
  |---|---|---|
  | 462–467, 470 | `PM1..PM6RecipeStepStart`, `CoolerRecipeStepStart` | 5111, 5113, 5114, 5115, 5116, 5117, 5118 |
  | 482–487, 490 | `PM1..PM6RecipeStepEnd`, `CoolerRecipeStepEnd` | same |

  That is 98 declared (variable, event) pairs. They may be redundant: the
  generic `RecipeStepStart` (857) / `RecipeStepEnd` (858) *are* subscribed with
  the same seven DVs, and 5113 is `StationID`, so module attribution is already
  available. **Worth one question to SPTS**: does the tool fire 857/858, the
  per-module family, or both? If only the per-module family, the SPTS
  recipe-step feed is empty today.

### D-4 SPTS is left OFF-LINE-capable, and its manual says that means silence

Omega Appendix A (p.66) gives the control state required for every host message.
Every message the middleware's opening sequence uses needs **On-line L, R**:

| Message | Required control state |
|---|---|
| S1,F13 Establish communication | Any |
| S1,F17 Request on-line | Off-line |
| S2,F33 / S2,F35 / S2,F37 | **On-line L, R** |
| S5,F3 Enable/disable alarm send | **On-line L, R** |
| S6,F23 Request spooled data | **On-line L, R** |
| S2,F41 Host command send | On-line **R** |

and "if the host sends an unavailable SECS-II message while the equipment is
off-line, then the equipment will respond with a respective **Sx,F0**".

Shipped configuration:

| Machine | `request_online` | `enable_alarms` | `drain_spool_on_connect` |
|---|---|---|---|
| SPTS_fxP_OMEGA_01 | **false** | **false** | false |
| DAVINCI200_MC4_HC1_01 | false | false | false |
| PTIQ_01 | false | false | false |
| NEXGEN_MG_01 | true | true | false |

NexGen is the only machine that lifts itself out of OFF-LINE, yet the Omega
manual documents the same requirement. The exposure is mitigated but not
removed: the liveness watchdog is wired for SPTS (LastCEID 34, EventsEnabled 30,
SpoolCountActual 2016) and raises `no_status_response` after the grace window —
that is detection after the fact, not prevention. S1F17 is a state-changing host
message and the project deliberately made it opt-in, so this is a commissioning
decision, flagged rather than flipped.

### D-5 SPTS spooling is detected but never enabled or drained

The Omega is the one tool with full GEM spooling (§9: S2F43/F44 to enable,
S6F23/F24 to drain, non-volatile ECs), and its manual states the intent
plainly — the host should unload the spool immediately after communications are
re-established. The middleware **never sends S2F43**, so spooling is only ever
active if someone enabled it on the tool, and `drain_spool_on_connect` is
`false`, so even then the backlog is only *reported* (via SpoolCountActual) and
never collected. Any middleware restart is therefore an unrecoverable gap on the
one tool that need not have one.

### D-6 Telemetry and its bearer token travel in clear text

`linkstuffs_http.base_url` ships as `http://astar-monitoring.linkstuffs.com:8080`.
The Linkstuffs/ThingsBoard API carries the device token in the URL path
(`/api/v1/<token>/telemetry`), so on plain HTTP both the token — a write
credential for that device — and every telemetry value are on the wire in clear.
`verify_tls` is irrelevant on an `http://` origin. The code is careful with the
token elsewhere (`_redact()` masks it before logging, and config validation
keeps it out of `base_url`), which makes the transport the weak link.

### D-7 The durable audit trail is writable by every local user

`install.ps1` grants `BUILTIN\Users` Modify with `(OI)(CI)` on `$InstallDir\data`
— which is where `ingress_journal.sqlite3` and all three outboxes live. The
grant is deliberate and well-reasoned (the control panel runs unelevated and the
service fails to start if it cannot open its databases), but the consequence is
that the journal that exists to prove what was received and what happened to it
can be deleted or edited by any interactive user on the box. Worth a decision:
either accept it explicitly, or move the databases under a service-only ACL and
give the panel a narrower path.

### D-8 Saving from the control panel destroys the config's commissioning notes

`save_config_atomic()` preserves only the leading comment block; everything
below goes through `yaml.safe_dump`, which emits data, not the document it came
from. `production.yaml`'s per-machine comments are where the operational
guidance lives — which HSMS timers each profile's manual states and why they
must match the tool, and the `request_online` interlock warning. The first panel
save removes all of it. The code documents this as a known limitation; on a
production template whose comments *are* the safety guidance, it deserves either
a comment-preserving round-trip (`ruamel.yaml`) or a warning in the panel.

### D-9 Two latent items, no live impact

- **Mixed naive and aware datetimes.** `CanonicalEvent.timestamp` is naive local
  time for equipment events and alarms (parsed from `received_at`) and aware UTC
  for health, SVID and connection events. `timestamp_ms()` happens to be correct
  for both — a naive local datetime's `.timestamp()` uses the local zone, an
  aware one is absolute — and nothing in the codebase compares the two, so there
  is no live defect. But `naive < aware` raises `TypeError`, and
  `LotBuffer.start_timestamp` can already hold either kind. It is a landmine for
  the next person who adds a comparison.
- **`secure_payload` does not authenticate the IV.** The HMAC covers the
  ciphertext only. In CTR mode an attacker who can modify the payload can flip
  IV bits and change the decrypted first block while the MAC still verifies.
  Dormant — `legacy_api` is disabled by default — and the layout is fixed by the
  n8n/PHP counterparty, so changing it is a coordinated change, not a local one.

---

## 3. Verified sound

Recorded so a later pass does not re-derive them.

- **The no-loss ordering holds.** `S6F11`/`S5F1` are journaled with
  `synchronous=FULL` *before* `ACKC6=0`/`ACKC5=0` goes back, and any failure to
  store becomes `ACKC6=1` so the tool retries. `S16F9`/`S16F7` have no negative
  confirm, so a store failure is deliberately allowed to abort the transaction
  rather than confirm an event that was never held. Retransmissions collapse on
  `(endpoint, S/F, system bytes, body digest)`.
- **Throughput is not a constraint.** The full ingress critical path — journal
  append + outbox enqueue + journal read-back + mark — measures **534 events/s**
  on this machine, with two `synchronous=FULL` fsyncs per event. Ordinary
  SECS/GEM event rates across 22 tools are two orders of magnitude below that.
  The 22-machine `slow` test passes.
- **Ack decoding is right, and right for the right reason.** `HCACK ∈ {0, 4}`
  (both vendor manuals return 4 for their documented commands), `ONLACK ∈ {0, 2}`,
  and every ack is decoded through the stream-function codec rather than read off
  `response.data`.
- **Coverage.** 243/243 NexGen CEIDs mapped and all 459 DVID names present;
  DaVinci's 228 unsubscribed events genuinely carry no data.
- **Config validation** rejects duplicate endpoints and display names,
  overlapping passive binds including the `0.0.0.0` wildcard case, simulator port
  collisions, out-of-range HSMS timers, and a `base_url` that already carries the
  device path.

---

## 4. Addendum — live rig diagnosis, 20 Aug

Two screenshots from the running rig (EAP control panel + simulator panel).
The feed was empty. **Three independent faults were stacked**; any one alone
produces exactly the observed silence.

### A-1 Config mismatch: the middleware and the simulator are different tools

The EAP log reads `Profile provenance for TOOL_04/ptiq_sec…`, so TOOL_04 is
running `ptiq_secsgem`. The simulator log reads
`nexgen_mg_series lot LOT_SIM_0006 done` and is firing CEIDs 4, 5, 14, 15,
124, 130, 134, 150, 212, 213 plus `ALID 1001`.

Resolved against the shipped profiles:

```
nexgen_mg_series   recognises 10/10   4=ProcessingStarted 5=ProcessingCompleted
                                      14=MaterialReceived 15=MaterialRemoved
                                      124=port1ReadyToUnload 130=port1CasPlaced …
ptiq_secsgem       recognises  0/10
```

`ALID 1001` is the MG manual's §8.5 group-100 "Initialization timeout error".
And the two sets do not even overlap at the subscription layer:

```
ptiq subscription enables CEIDs : [100, 101, 1001, 1002, 1003, 1004, 1005, 1006]
simulator is firing       CEIDs : [4, 5, 14, 15, 124, 130, 134, 150, 212, 213]
```

Zero intersection. Point TOOL_04 at `nexgen_mg_series`, or point the simulator
at a PTIQ profile — but they must agree.

### A-2 Simulator bug: the spool wedges permanently and never drains

Every line in the simulator log is `Spooled S6F11 CEID=…`. Not one is
`-> S6F11`, which is what `_send_or_spool` logs on a successful send. Six lots
completed and nothing was ever delivered.

`simulator/equipment.py::_send_or_spool` decides with a **sticky** test:

```python
backlog = bool(self._spooled_messages)
if backlog or self.communication_state.current != COMMUNICATING:
    return self._queue_spooled(label, message)
```

Once *anything* is in the spool, `backlog` is True forever, so every later
event spools too — even on a perfectly healthy link. Reproduced:

```
link DOWN -> spooled=2  delivered=0
link  UP  -> spooled=8  delivered=0     <-- still zero delivered
```

The only escape is `_schedule_spool_drain()`, called from exactly one place:
the **S6F23 handler**. The middleware only sends S6F23 when
`drain_spool_on_connect: true`, which is `false` for every shipped machine. So
in the default configuration the spool can fill but can never empty; at
`_spool_limit = 1000` it starts overwriting the oldest entry.

The simulator started before the middleware connected at 13:38:53, spooled
while alone, and was still spooling at 13:39:56 — 63 s into a healthy link.

**Fix direction:** drain automatically on entry to COMMUNICATING rather than
only on host request. Real GEM equipment does exactly this when the host has
not disabled spooling, and it is what makes D-5's "the host should unload the
spool immediately after communications are re-established" achievable at all.

### A-3 Shutdown took minutes and could hang — *fixed*

See F-6 below.

### F-6 `stop()` spent a fresh 10 s on every join, and `retire()` could hang

Symptom reported from the panel: stopping takes forever and "Run service here"
stays disabled. Both are one cause. `gui/app.py` clears its busy latch only in
the `finally` after `service.stop()` returns, so the button is disabled for
exactly as long as teardown takes — and teardown was fully sequential with a
fixed 10 s timeout on each of ~7 joins per machine:

```
                        BEFORE                       AFTER
   1 machine(s):    89s (~1.5 min)   ->   <= 20s + CSV flush
   2 machine(s):   129s (~2.1 min)   ->   <= 20s + CSV flush
   4 machine(s):   209s (~3.5 min)   ->   <= 20s + CSV flush
  22 machine(s):   929s (~15.5 min)  ->   <= 20s + CSV flush
```

The joins that actually expire are the ones whose worker is blocked in network
I/O — an SVID poll waits up to T3 = 45 s; an HTTP publish is
`timeout_sec × retry_count` — which on a busy service is most of them.

Worse, `GatewayHost.retire()` called secsgem's `disable()` with **no timeout**,
and every line after it is the fallback for exactly that case, so a wedged
`disable()` made the recovery unreachable and `stop()` never returned at all —
leaving the button disabled permanently until the window was closed.

Fixed:

- `STOP_TIMEOUT_SEC = 20.0`, one deadline shared across every machine and
  worker; each join gets what is *left* of it, never a fresh timeout.
- `stop(timeout=…)` plumbed through `SecsMachineSession`, all three publishers
  and `SQLiteOutbox.stop_maintenance`.

**Not** fixed, and deliberately so. `retire()`'s unbounded `disable()` was
first given a 3 s cap on a worker thread, so a wedged secsgem could not hang
`stop()`. That let `_force_close_socket()` run while `disable()` was still
tearing the connection down, and secsgem 0.3.0 keeps module-level state that
is not safe for it: across a full suite run the lingering workers deadlocked
each other and pytest froze at 0 % CPU partway through the MG loopback tests
— reproducibly, at the same test, while that file passed in isolation. The
change was reverted. A wedged `disable()` can still block one machine's
teardown; that is a theoretical risk, and trading it for a demonstrated
deadlock in the shutdown path is a bad exchange. The service-level budget is
what actually fixed the reported symptom.

Deliberately **outside** the budget, because these are the guarantees the stop
exists to provide: closing each host's socket, flushing open lot buffers to
local CSV, and releasing the single-instance lockfile. A deadline that cut the
CSV flush short would trade a slow stop for a lost lot file. A worker that
outlives the budget is logged by name rather than abandoned silently; it is a
daemon thread and its session generation is already retired, so an expired
join cannot let a stale sample reach the pipeline.

Pinned by `tests/test_service_stop_is_bounded.py` (12 tests).


---

## 5. Second pass — production readiness

### F-7 The subscription bands were not actually applied to two shipped files

D-1 measured that one rejected VID cost the DaVinci **every** collection event.
The cause was not a stale generator, as first suspected: `EventSubscription.full.json`
(282 events, 14 bands) and `EventSubscription.json` (54 events, the curated set
the profile actually loads) are two different files, and only the former was
ever banded. SPTS was a half-measure - its 96 events carried 7 band labels
while all 43 of its reports sat in one unnamed band, so the S2F35 leg was
contained and the S2F33 leg was not.

`scripts/band_subscriptions.py` now assigns bands to both curated files:
events by their profile's own CEID-family rule, reports from the event that
references them (verified 1:1, no shared or orphaned reports). Idempotent.

Worst case, rejecting one VID and taking the worst report per profile:

| Profile | Bands | Events | Before | After |
|---|---:|---:|---:|---:|
| `davinci200_mc4_hc1` | 1 → **10** | 54 | **0 survive (0%)** | 40 survive (74%) |
| `spts_fxp_omega` | 1 → **7** | 96 | 42 survive (43%) | 56 survive (58%) |
| `nexgen_mg_series` | 31 | 243 | 163 survive (67%) | unchanged |

Guarded by three parametrised tests over every shipped subscription: every
report and event carries a band; a report shares the band of the event that
links it (a cross-band link gets LRACK=3, "report does not exist"); and no
single band can take down the whole feed. All four fail on the pre-banding
files.

### F-8 The network mirror ran inside the SECS acknowledgement path

`_write_buffer` is reached from `_handle_s6f11` **before** S6F12 is returned,
and it copied the finished lot file to `csv_network_dir` on that thread. The
tool holds the transaction open meanwhile, and T3 is 30-45s depending on
profile; a copy to an unreachable SMB share blocks for the OS timeout, which
on Windows is longer than that. A sick file share could therefore push the
*equipment* into declaring a communications failure. The tool then
retransmits - the ingress journal collapses the duplicate correctly - but
throughput collapses for a reason that has nothing to do with SECS.

The copy is now queued to `CsvMirrorWorker`, which already owned a durable
journal-backed queue with exponential backoff, and the writer wakes it on
enqueue so deferring costs milliseconds rather than a poll interval. Nothing
is risked: the local CSV is fsynced before the function returns, and the
journal - not the mirror - is what carries the no-loss guarantee.

`mirror_errors`, the writer's "what failed lately" surface that the control
panel reads, was previously populated only by the inline copy; the worker now
records failures there too, so an unreachable share cannot go silently absent
from it.

### F-9 The equipment spool was a one-way door

Found on a live rig that had run six lots and delivered nothing while HSMS
linktests flowed normally in both directions.

`_send_or_spool` refuses to send while a backlog exists - correct, because a
spooled stream has to stay in order - and the only thing that emptied the
backlog was an S6F23 from the host. The middleware sends S6F23 only when
`drain_spool_on_connect: true`, which is false on every shipped machine. So
one event spooled before the host connected made every later event spool too,
for the life of the run. Nothing cleared it on `enable()`, on reconnect, or on
reaching COMMUNICATING.

Now the spool drains on entry to COMMUNICATING, which is what SEMI E5 has real
equipment do once communications are re-established; the drain retries with
capped backoff instead of abandoning on one unacknowledged retransmit; and a
single-worker guard keeps two drains from interleaving the order the spool
exists to preserve. The host's S6F23 still works and is now an explicit
re-request rather than the only escape.

### F-10 Nothing was logged per collection event

The middleware logged alarms and nothing else, so a tool delivering hundreds
of events and a tool delivering none produced identical logs - the exact
silent-failure mode the liveness watchdog exists to catch, which should not
need a watchdog to be visible.

Now, at INFO: the gateway records every accepted S6F11 at the point it is
durably journaled and about to be acknowledged; the service records what each
event mapped to, with lot/wafer/port/chamber/recipe and which sinks took it;
and the simulator names every CEID it sends. `CEID n fired but NOT enabled by
the host` moved from DEBUG to INFO - it is the single most useful line for
diagnosing an empty feed. Report payloads are summarised as
`RPTID <id>x<count>` rather than dumped: one SPTS report carries 172 VIDs and
the journal already holds every byte.

### F-11 Provisioning worker could outlive the session that owned it

`_on_connect` appended the worker to `_provision_threads` under the session
lock but called `start()` outside it. `stop()` snapshots that list under the
same lock, so in the window between append and start it could either join a
thread that had not begun (`RuntimeError`) or return while the worker started
a moment later and went on issuing SECS round-trips against a torn-down
connection. Started inside the lock now.

### F-12 A partial subscription could start a lot

The MG simulator accepted the host's subscription as complete once the enabled
count held for two 50ms polls. The middleware subscribes in 31 bands and the
enabled set grows one burst per band, so any inter-band pause over 100ms read
as "finished" - the lot then started and every CEID the host had not reached
yet was correctly, silently dropped. The settle window is 1.0s of quiet, and
the timeout path now distinguishes "still growing, starting anyway" from
"host never enabled anything".

### One deliberate non-fix

`retire()` still calls secsgem's `disable()` synchronously, so a wedged
shutdown can block one machine's teardown. Bounding it on a worker thread was
tried and reverted: it let `_force_close_socket()` run while `disable()` was
still tearing down, and secsgem 0.3.0 keeps module-level state that is not
safe for that - across a full suite run the lingering workers deadlocked each
other and pytest froze at 0% CPU. A theoretical hang is not worth a
demonstrated deadlock in the shutdown path. The service-level budget
(`STOP_TIMEOUT_SEC`) caps everything else.
