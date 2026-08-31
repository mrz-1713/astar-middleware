# Deep Audit — 2026-08-21

Middleware **and** simulator, audited against every page of `docs/vendor/`.
Layered on the uncommitted work described in
[HANDOVER_PRODUCTION_READINESS_2026-08-20.md](HANDOVER_PRODUCTION_READINESS_2026-08-20.md);
prior findings were treated as a baseline and re-verified rather than repeated.

Suite: **649 passed** before → **659 passed, 1 skipped, 5 deselected, 0 failed**
after (9 tests added). `ruff --select=F,E9` clean on all five packages.

---

## 0. Document coverage

All 14 PDFs (1,160 pages) were rendered page-by-page and read, images included.
The two SECS manuals are almost entirely **vector-drawn** — 93,342 (Omega) and
14,094 (NexGen) drawing operations — so their tables have no structure in
extracted text. That is why they were read visually rather than grepped.

| Document | Pages | Raster images | Pages mentioning SECS |
|---|---:|---:|---:|
| Omega SECS-II (SPTS fxP 200mm) | 231 | 1 | 231 |
| NexGen MG Series SECS/GEM V1.1.18 | 197 | 33 | 73 |
| DaVinci Software Operation Manual | 204 | 35,700 | 12 |
| DaVinci 200 User Manual V1.8 | 61 | 190 | 6 |
| TheWizard_UM | 156 | 439 | 2 |
| TC User Documentation | 151 | 31,861 | 2 |
| 8 maintenance / safety / backup manuals | 160 | 385 | 0 |

---

## 1. Conformance re-verified against the manuals

Parsed and diffed against the shipped code where the table allowed it.

| Check | Source | Result |
|---|---|---|
| NexGen collection events | MG §8.1 p97–102 | **243 / 243 exact** |
| NexGen status variables | MG §8.2 p119–127 | **251 / 251 names match** |
| SPTS station-type table | Omega §24.1 p213 | **25 / 25 exact** |
| DaVinci HSMS timers 45/10/5/10/5 | SOM p127 **screenshot** | match |
| SPTS HSMS timers 30/5/10/5/6 | Omega Table 3 p13 | match |
| Clock 12- and 16-byte forms | MG ECID 5 · Omega §12.4 | both handled |
| SPTS alarm → module attribution | Omega §8.3 p27 | formula implemented |
| `ONLACK=2` treated as success | Omega §5.2.3.5 | correct |
| NexGen DVIDs 2159–2161 | MG §8.2 p114 | **undocumented rename** |

The DaVinci timer values exist **only inside a screenshot** of the tool's own
Host Interface dialog (Figure 42). A text-only pass would not have found them.

### The one deviation, and why it is probably right

The manual prints `pm1BemFlowMaxPrevStep` at *both* DVID 2144 and DVID 2159,
while its own CEID column assigns 2144 → CEID 519 (`Pm1BemStepFinished`) and
2159 → CEID **521** (`Pm2BemStepFinished`). `profiles.py` follows the CEID
column and names 2159–2161 `pm2Bem*PrevStep`. The block structure confirms it:
each module is 3 `PrevStep` + 12 `Wafer` entries, so 2144–2158 is PM1 and
2159–2173 is PM2. **Nothing in the code says so**, so the next person diffing
against the manual will "fix" it back. Worth a comment at
`eap_middleware/profiles.py:1671`.

---

## 2. Fixed in this pass

Each was reproduced first, and each regression test was confirmed to fail
against the unfixed code.

### F-1 (CRITICAL) The same wafer could be written to the lot CSV twice

`service.py::_on_secs_event` gated re-entry on `dispatch_status` alone. The two
sinks fail independently: a replay pass that wrote the CSV row and then hit
`OutboxFullError` leaves dispatch `PENDING`, because `mark_dispatch_failed`
only increments the attempt counter. The live callback then re-entered with
`csv=True` and appended the same collection event's row a second time.

`_replay_journal` already guarded this with `csv_writer.holds()`; the live path
did not. The window is between `journal.append()` returning and the dispatch
lock being taken — and it widens under exactly the load that fills the outbox.

Reproduced deterministically (`SEQ REFS: {1: 2}`, two rows in the lot buffer).
Fixed by deriving both sinks from fresh journal state plus `holds()`, mirroring
`_replay_journal`: the two paths race in *both* directions, so the guard has to
be symmetric.

Test: `test_parallel_reliability_audit.py::test_live_dispatch_does_not_redo_a_sink_replay_already_applied`

### F-2 (CRITICAL) The shipped config dead-lettered every telemetry publish

`config/production.yaml` shipped `http://astar-monitoring.linkstuffs.com:8080`.
When that origin redirects to HTTPS, `urllib` re-issues the POST as a **GET
with the body dropped** (standard for 301/302/303). The telemetry endpoint is
POST-only → `405` → `_post` classifies any 4xx as permanent → five of those
dead-letter the row. Telemetry destroyed, CSV files still written, and the log
blames the server. Proven against a local redirect server.

Separately: the device token is *in the URL path*, so a plaintext origin puts a
write credential on the wire in clear and `verify_tls` is meaningless.

Three-layer fix — template now ships the `https` origin; config load warns on
any `http://` base URL naming both consequences; and the publisher refuses a
method-downgrading redirect, keeping the row **queued** (`_UndeliverableError`,
not `_PermanentPublishError`) so the backlog drains once `base_url` is fixed.

Test: `test_linkstuffs_http.py::test_redirect_is_refused_instead_of_dead_lettering_telemetry`

### F-3 (MAJOR) A tool set to annotated reports delivered nothing

Which message carries a collection event is a **tool-side setting**:

- Omega equipment constant **4022 `EventReportMsg`** — `67075 = S6F3`,
  `67083 = S6F11`, `67085 = S6F13` (manual Table 6, p36).
- NexGen S2F33 carries a Boolean selecting annotated reports per report
  definition (MG §6.2, §6.5).

secsgem 0.3.0 ships **no S6F13/S6F14 classes** and `gateway/host.py` registered
no handler. A tool on annotated reports connects, has every S2F33/35/37
acknowledged, and then delivers event reports nothing can decode — a green link
with a permanently empty feed.

New `gateway/annotated_reports.py` declares both functions in secsgem's own SML
form; `_handle_s6f13` runs the identical pipeline including the ordering that
makes `ACKC6=0` mean "durably stored". S6F13 carries *more* than S6F11, so the
VID/V pairs are flattened positionally for the mapper and also kept as
`_vid_values`.

Tests: `test_annotated_event_reports.py` (4)

### F-4 (MAJOR) The vendor's prescribed opening reset was never sent

The MG manual's own lot-start sequence (§9.1 p170, wire traces §9.1.1.2–.4)
opens with delete-all-reports, unlink-all, disable-all-events. The middleware
sent none of it. On a tool that previously talked to another host — the
commissioning case — that host's reports and CEID links survive on the
equipment.

Added as opt-in `reset_subscription_on_connect` (default **off**: it changes a
message sequence commissioned tools have already accepted). Runs once before
the first band — never between bands, which would wipe out every band already
accepted — in the documented order S2F37 → S2F35 → S2F33. A refusal is logged
but never aborts the real subscription.

Tests: `test_subscription_bands.py` (3)

### F-5 (MEDIUM) A new setting was silently ignored on hot reload

`_restart_signature` is a hand-maintained tuple; any `MachineConfig` field
missing from it is accepted in `production.yaml`, written back, and never
applied to the running session. `hsms_timers` shipped that way once, and F-4's
flag immediately repeated it.

Beyond adding the field: `_NO_RESTART_FIELDS` now states the exemptions
explicitly, and a test builds two machines differing in one field at a time and
asserts the signature actually changes. Adding a `MachineConfig` field now
forces the decision.

Test: `test_unified_control.py::test_restart_signature_covers_every_connection_affecting_field`

### F-6 (MEDIUM) The F-2 fix leaked the device token into the log

The new redirect error rendered the `Location` header verbatim — and that
header carries the same `/api/v1/<TOKEN>/` path as the request. `_redact` moved
to module scope (the redirect handler has no publisher instance) and the
location is redacted at construction. The regression test asserts the raw token
never appears in captured log output.

---

## 3. Found, not changed

### D-A SPTS is left able to sit OFF-LINE, and its manual says that means silence

Omega Appendix A (p66) requires control state **On-line L, R** for S2F33,
S2F35, S2F37, S5F3 and S6F23; an off-line tool answers `Sx,F0`.
`request_online` is `false` for SPTS, DaVinci and PTIQ. Detection exists (the
liveness watchdog) but that is after the fact. S1F17 is the one state-changing
message the middleware sends, so this stays a commissioning decision.

### D-B SPTS spooling is never enabled or drained

The Omega is the one tool with full GEM spooling and its manual says the host
should unload the spool as soon as communications return. The middleware never
sends S2F43 and never sets **ECID 4010 `SpoolEnabled`**, and
`drain_spool_on_connect` is false. Every restart is an unrecoverable gap on the
one tool that need not have one.

### D-C 14 documented data-carrying SPTS events are unsubscribed

CEIDs 462–467, 470, 482–487, 490 (per-module `RecipeStepStart/End`) each declare
the same seven DVs as the generic 857/858 pair that *is* subscribed, and DV
5113 is `StationID`, so module attribution is already available. Probably
redundant — but confirm with SPTS: does the tool fire 857/858, the per-module
family, or both? If only the per-module family, the SPTS recipe-step feed is
empty today.

### D-D The simulator cannot reproduce the OFF-LINE trap

All four machines in the shipped template use `implementation: "profile"`, and
`ProfileSimulator`/`EquipmentSimulator` model **no control state at all** —
`is_offline` exists only in `nexgen_mg_simulator.py` and is referenced in
exactly one place. So the most likely commissioning failure for SPTS and
DaVinci is the one thing the simulator cannot rehearse.

### D-E The durable audit trail is writable by every local user

Unchanged from 2026-08-20 D-7. `install.ps1` grants `BUILTIN\Users` Modify on
the data directory holding the ingress journal and all three outboxes.

---

## 4. Recommended next, in order

All four are backed by constants read out of the manuals during this audit, so
none requires guessing.

1. **Read the SPTS station layout from the tool.** `spts_module_vids.py`
   deliberately refuses to guess which module type occupies which station (the
   Appendix E VID formula is ambiguous — at one station 107 VIDs are claimed by
   two families), so the layout is hand-entered today. The tool publishes it:
   **ECIDs 1000000–1000017**, `Machine_Config_STATIONS_Station_x_Type`, using
   the §24.1 values this audit verified match `ModuleVariables.json` exactly.
   One S2F13 at connect removes the manual step.
2. **Compare the tool's HSMS timers with ours at connect.** A mismatch drops
   the link intermittently with nothing in either log. The Omega exposes its
   live values as **ECIDs 1000188–1000192** (T3/T5/T6/T7/T8, in **milliseconds**
   against our seconds). Warning on disagreement turns a silent fault into a
   startup diagnostic.
3. **Check `EventReportMsg` at connect.** F-3 means an annotated-report tool now
   works instead of failing silently; reading **ECID 4022** and publishing a
   health event when it is not `67083` would tell the operator which report
   style they are on — the same treatment the DaVinci E40 path already gets.
4. **Teach the profile simulator the control state model.** Both the Omega
   (§5.2) and the MG (§3.2) define it, and `nexgen_mg_simulator.py` already
   implements it. Lifting it into the shared base makes D-D rehearsable.

---

## 5. Note for whoever touches the tests

Two shipped tests pinned the literal `http://astar-monitoring.linkstuffs.com:8080`
— i.e. they pinned the F-2 defect itself. They now assert the *contract*
(origin only, no `/api/v1`, HTTPS) rather than a value, so they still catch a
pasted full endpoint URL without re-freezing the bug.

## 6. Files changed

```
eap_middleware/service.py          eap_middleware/linkstuffs_http.py
eap_middleware/config.py           eap_middleware/models.py
gateway/host.py                    gateway/event_subscription.py
gateway/annotated_reports.py (new) gui/model.py
config/production.yaml

tests/test_annotated_event_reports.py       (new, 4)
tests/test_parallel_reliability_audit.py    (+1)
tests/test_linkstuffs_http.py               (+1)
tests/test_subscription_bands.py            (+3)
tests/test_unified_control.py               (+1)
tests/test_guided_setup.py                  (2 rewritten to pin the contract)
```
