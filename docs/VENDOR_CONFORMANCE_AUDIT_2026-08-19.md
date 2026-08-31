# Vendor Conformance Audit — 2026-08-19

**Scope:** every PDF and workbook in `docs/`, and the `eap_middleware` + `gateway` +
`simulator` + `gui` + `deploy` tree, on `feat/nexgen-mg-series-profile`
(b59819a plus ~3,200 uncommitted lines).

**Rule for this pass:** every finding names the vendor document, the section, and
the code that disagrees with it. Where code and manual agree, that is recorded
too, so a later pass does not re-litigate it.

**Method.** All 14 PDFs (1,150 pages, 2,427 embedded images) were rendered page
by page and read as images as well as text, so figure-only content was not
missed — that is how the DaVinci timer defaults (a screenshot on SOM p.127) and
the MG report composition (the figure on p.20) were recovered. Text via
`pdftotext -layout`; the DaVinci workbook via `openpyxl`. Coverage numbers come
from parsing each document's own validity column and diffing it against the
shipped `EventSubscription.json`. Every engineering finding was reproduced
against the live tree.

---

## 0. Status — all findings addressed

Fixed in this branch; regressions pinned by
`tests/test_vendor_conformance_audit_fixes.py` (27 tests). Suite: **543 passed**
(from 516), 4 environment-only `_tkinter` failures on the audit machine.

| | Finding | Fix |
|---|---|---|
| M-1 | 497 documented process values uncollected | 20 new per-chemistry subscription bands; **49% → 95%** of manual-declared pairs delivered, empty reports 129 → 103 |
| M-2 | HCACK=4 read as failure | `HCACK_ACCEPTED = {0, 4}` |
| M-3 | Read-back mismatch logged as ERROR | Downgraded to WARNING, message cites MG §9.1.1.7/8 |
| M-4 | DaVinci spooling defaults undocumented | Commissioning table + warning in QUICKSTART |
| M-5 | `request_online` interlock consequence | Panel field help quotes SOM §9.6; documented in QUICKSTART |
| M-6 | Two slot-map encodings, one inverted | SVID 4306 renamed `SlotMapGem`; DVID 2093 keeps `SlotMap` |
| M-7 | SPTS alarms lack module attribution | `decode_alarm_id()` per §8.3; alarms carry `alarm_source` |
| M-8 | User Manual vs workbook on LP3 | Documented — vendor question, profile follows the workbook |
| M-9 | SPTS process state invisible | CEIDs 100/101 subscribed and mapped |
| M-10 | Simulator SOFTREV 3× the maximum | Now `MG22` / `3.7.0.0`, the manual's own values |
| M-11 | Rejected/lost/skipped wafers produced no event | 6 workbook CEIDs (3220018–23) mapped and subscribed |
| F-1 | Supervisor stalls on a dead share | Mirroring moved to its own thread; batch cap 8, exponential backoff |
| F-2 | No CI for the middleware | `.github/workflows/middleware.yml` — suite on 2 OSes, installer build, ACL check |
| F-3 | Panel cannot save `production.yaml` | `install.ps1` grants Users Modify on `app\config`; asserted in CI |
| F-4 | CSV failure recorded as publish failure | `mark_csv_failed()`; the CSV loop no longer escapes |
| F-5 | `display_name` unvalidated into filenames | Charset + Windows reserved-name validation at config load |
| F-6 | JobTracker maps unbounded | LRU caps (5000 wafers / 500 lots / 500 jobs) |
| F-7 | Diagnostic lists unbounded | Bounded `deque`s |
| F-8 | Mirror copy races itself | Task leasing before the copy |
| F-9 | Saving destroys config comments | Comment header preserved across save |
| F-10 | Timers silently inherited | MG and PTIQ state theirs explicitly |
| F-11 | Dead code | Removed; `ruff --select=F` clean |

One deliberate non-change: the 45 pairs still undelivered (CEIDs 701, 721, 102,
103, 212, 312) sit inside working lifecycle bands. Adding them there would put
the wafer-start and job-state feeds at risk of a `DRACK=4` to gain a handful of
values. They are listed here so the choice is visible, not forgotten.

---

**Baseline:** `python3 -m pytest -q` → **516 passed, 4 failed, 15 skipped**
(123 s). All 19 failures/skips are `ModuleNotFoundError: _tkinter` on the audit
machine — environmental, not product. With a tkinter-capable interpreter 40
further GUI tests pass; the rest block in `tkinter.update()`, which is macOS
behaviour outside an Aqua session. Those tests only ever run on Windows, and no
CI job runs them (F-2).

---

## 1. Headline: event coverage vs. data coverage

The MG manual's §8.2 variable table has a **CEID** column naming, for each of
its 467 data variables, the events at which that variable is valid. Read across
the table it declares **968 valid (variable, event) pairs**. The shipped
subscription delivers 471.

| Profile | Source of truth | Events subscribed | Declared pairs | Delivered | Coverage |
|---|---|---:|---:|---:|---:|
| `nexgen_mg_series` | MG manual §8.2 | 243 / 243 | 968 | 471 | **49%** |
| `davinci_200_mc4_hc1` | SECS-Items workbook | 48 / 282 | 117 | 117 | **100%** |
| `spts_fxp_omega` | Omega manual Table 5 | 94 / 224 | 143 | 143 | **100%** |

Declared pairs are counted within subscribed events only.

The families fail in opposite directions. DaVinci and SPTS are **narrow but
complete** — a subset of events, each fully populated. NexGen is **broad but
hollow** — all 243 events subscribed, 129 of them carrying nothing.

---

## 2. Manual-conformance findings

### M-1 (BLOCKING) — 497 documented process values are never collected

129 of 243 subscribed MG events link an empty report (`"rptids": []`): every
medium, DI, N2-dry and DiwO3 step-finished event, both ATMSi measurement events,
both HPC step events. `pm1WaferFinished` / `pm2WaferFinished` (CEID 213/313) do
carry a report, but only nine identity fields, while the manual declares 61 and
55 further variables valid there.

Lost: per-wafer and per-step N2 chuck flow min/max/avg, N2 dry flow, medium 1–3
temperature and flow, DI flow, chuck speed, step elapsed time, step number.

| CEID | Event | Undelivered | Report |
|---:|---|---:|---|
| 213 / 313 | `pm(1\|2)WaferFinished` | 61 / 55 | 9 identity DVs |
| 225 / 325 | `pm(1\|2)DiStepFinished` | 37 each | **empty** |
| 515 / 517 | `Pm(1\|2)HpcStepFinished` | 30 each | **empty** |
| 510 / 511 | `Atmsi(1\|2)MeasFinished` | 30 each | **empty** |
| 223 / 323 | `pm(1\|2)MediumStepFinished` | 16 each | **empty** |
| 229 / 329 | `pm(1\|2)DiwO3StepFinished` | 16 each | **empty** |
| 227 / 327 | `pm(1\|2)N2DryStepFinished` | 13 each | **empty** |

> **Source** — NWS MG V1.1.18 §8.2 Data Variables, "CEID" column (VIDs
> 1000–1189, 2100–2418); §3.3 "Process step related events (CEIDs) and variables
> (VIDs)"; and the figure on p.20, which names the exact VID set behind CEID
> 223, 225 and 227. That figure is **image-only** — absent from the text layer.

**Fix.** Add the process-metric variables as *new* subscription bands. Band
isolation already works (a refused band leaves the others reporting), so a VID
the tool does not implement cannot take the whole subscription down. Do not
extend existing bands — a `DRACK=4` would then cost the events you already have.

### M-2 (MAJOR) — a successful remote command is read as a failure

`gateway/host.py:513` returns `self._decode_ack(response) == 0`. Two manuals
independently document `HCACK = 4` as the normal success reply for an
asynchronous command, and the MG traces show `S2F42 <B 04>` for PPSELECT, MAP
and START. Every documented MG remote command would report as failed.

> **Source** — NWS MG §5.2 HCACK, revised in v1.1.17 expressly for this: "4 =
> Acknowledge, command will be performed with completion signaled later by an
> event". Traces §9.1.1.11, §9.1.1.12, §9.1.1.14. Omega §15.2: "the SPTS fxP
> equipment will then respond via S2,F42 with HCACK = 4 meaning that the command
> 'is going to be performed'."

**Fix.** Accept `{0, 4}`. Latent today (no caller on the read-only runtime),
which makes it cheap to fix before v2 needs it.

### M-3 (MAJOR) — a phantom fault on every MG connect

`verify_enabled_events()` logs at `ERROR` when the `EventsEnabled` read-back
omits a requested CEID. The manual's own capture shows that on healthy hardware:
§9.1.1.7 enables 4, 5, 13, **130, 131**, 140, 141; §9.1.1.8 reads SVID 12 back as
4, 5, 13, **143, 144**, 140, 141.

The check correctly does not gate the subscription, but at `ERROR` it sends a
field engineer after documented behaviour.

> **Source** — NWS MG §9.1.1.7 vs §9.1.1.8. Code: `gateway/host.py:661`.

**Fix.** Log at `WARNING` and say in the message that a read-back difference is
documented on this tool.

### M-4 (MAJOR) — DaVinci spooling cannot survive an outage

`drain_spool_on_connect` is offered as outage recovery. The workbook's own EC
defaults make it close to inert:

| ECID | Constant | Default | Consequence |
|---|---|---:|---|
| 4020001 | `EnableSpooling` | 0 | Spooling off; nothing buffered |
| 4020003 | `MaxSpoolMessages` | 20 | Seconds of events on a busy tool |
| 4020002 | `OverWriteSpool` | TRUE | Oldest messages discarded first |
| 4020004 | `MaxSpoolTransmit` | 5 | Drained five at a time |

> **Source** — `SECS-Items_MueTec DaVinci 200 MC4_HC1.xlsx`, sheet "EC".
> Corroborated by SOM §9.6.2 (p.129), whose Host Interface panel shows
> "Spooling State" and a "Spool Full" indicator.

**Fix.** Add a commissioning step (set `EnableSpooling=1`, raise
`MaxSpoolMessages`) and state in the deployment guide that at vendor defaults a
DaVinci buffers nothing, so a middleware outage is unrecoverable loss.

### M-5 (MAJOR) — "Request ON-LINE" can lock the tool's operators out

The panel presents `request_online` as a bare checkbox. On a DaVinci whose
front-panel switch sits at REMOTE, lifting it out of OFF-LINE lands it in Online
Remote.

> **Source** — DaVinci SOM §9.6: "If the tool operates in control state 'Online
> Remote' internal interlocks for production operation of the tool (jobs) will be
> enabled. This means the user cannot create or modify any control or process
> jobs. Additionally carrier management/handling (e.g. cancel carrier, proceed
> with carrier, dock, undock) cannot be operated locally (only from host)."

**Fix.** The defaults are right (`false` for DaVinci, `true` for MG, the latter
correctly justified in `production.yaml` from MG §3.2). Carry that reasoning into
the panel — the checkbox needs the DaVinci consequence beside it.

### M-6 (MINOR) — two slot-map encodings, one inverted

| Encoding | Where | 1 | 2 | 3 | 4 |
|---|---|---|---|---|---|
| GEM status variable | SVID 3110 / 4306 | FULLSLOT | EMPTYSLOT | **CROSSSLOTTED** | DOUBLESLOTTED |
| E87 carrier attribute | S3F17 CATTRID `SlotMap` | EMPTY | NOT EMPTY | **CORRECTLY OCCUPIED** | DOUBLESLOTTED |

Value `3` means opposite things depending on which VID produced it.

> **Source** — NWS MG §8.2 Status Variables (SVID 3110 `port1MapResult`) vs §6.3
> "Carrier Attribute Definition Table" (`SlotMap`: UNDEFINED, EMPTY, NOT EMPTY,
> CORRECTLY OCCUPIED, DOUBLESLOTTED, CROSS SLOTTED).

No live defect — values pass through uninterpreted — but bare integers are
forwarded with nothing recording which encoding applies. Label it on the way out.

### M-7 (MINOR) — SPTS alarms arrive without the module that raised them

`ALID = station × 10,000,000 + type × 100,000 + offset`. ALID 22400005 is Process
Module 1, an Etch module, offset 5. The pipeline treats ALID as a string. The
sibling Appendix E formula is already implemented in
`eap_middleware/spts_module_vids.py`, which carries the station and type tables
this decode needs.

> **Source** — Omega §8.3 "Alarm ID's", with both enumerations (station 0–10,
> type 3–58).

### M-8 (MINOR) — the two DaVinci documents disagree about load ports

The User Manual describes three load ports and documents LP3 alongside LP1/LP2.
The SECS-Items workbook carries 28 SVs and 38 events each for LP1 and LP2 and
**nothing for LP3**. The profile follows the workbook, which is correct. Recorded
so it is not later mistaken for a coverage gap: if a deployed tool physically has
LP3, the question is for MueTec.

> **Source** — DaVinci 200 User Manual V1.8 §3.3.6 vs workbook sheets "SV" and
> "Events" (zero `LP3` rows).

### M-9 (MINOR) — no visibility of the SPTS process state machine

Table 5 publishes 60 CEIDs reporting the cassette state machine (151–176 VCE A,
181–206 VCE B, plus ProcessStateChange / Pausing / Paused / Resumed). None are
subscribed. Not data loss — the CSV lifecycle is covered by the
MBCStart/MBCComplete/MBStart/MBComplete/PM*n*RecipeStart families — but STOPPING,
RESTARTING, ABANDONING and PAUSED never reach the host, so a stalled lot cannot
be diagnosed from events.

> **Source** — Omega §7.3 Table 5 and §15.2 Table 11.

Cheapest useful addition: CEIDs 100 and 101 (`ProcessStateChange1/2`).

### M-10 (MINOR) — simulator SOFTREV is 3× the documented maximum

The MG simulator answers with `["MG22", "MG Series V1.1.18"]`. MDLN matches the
manual's capture; SOFTREV is 18 characters against a documented 6-byte maximum
and the 7-character value in the trace. The host is deliberately tolerant
(`SecsS01F02Extended` exists because DaVinci's SOFTREV is 24 chars), so the suite
only ever exercises the long case.

> **Source** — NWS MG §5.2 ("SOFTREV, Software Revision Code, 6 bytes maximum");
> §9.1.1.1 shows `<A[7] '3.7.0.0'>`.

### M-11 (MINOR) — rejected wafers are indistinguishable from unmapped events

DaVinci material states include Aborted, Stopped, Rejected, Lost and Skipped.
None has a canonical CSV event type, so all land as `unknown` — kept in the lot
file, correctly, but indistinguishable from an unrecognised CEID.

> **Source** — TC User Documentation EN, Table 33 "Material State" (and Tables
> 34–36 for arrival, transport, substrate-reading states).

---

## 3. Confirmed correct against the documents

| Check | Manual | Result |
|---|---|---|
| MG collection-event table | NWS §8.1 | 243 / 243 CEIDs, zero name mismatches |
| DaVinci event & SV names | SECS-Items workbook | Faithful — 6 cosmetic ControlJob name shortenings, 1 documented SV alias (`1060007 PM1/RecipeName` → `RecipeName`) |
| DaVinci HSMS timers | SOM p.127, Figure 42 | T3 45 · T5 10 · T6 5 · T7 10 · T8 5 — exact match; values exist **only** in the screenshot |
| Omega HSMS timers | Omega §4.4 Table 3 | T3 30 · T5 5 · T6 10 · T7 5 · T8 6 — exact match |
| MG ProcessState / ControlState | NWS §3.2, §3.3, SVID 11 & 15 | Transcribed exactly, including the gaps at 6 and 11 |
| Alarm code bits | NWS §5.2 ALCD, SEMI E5 | Correct — bit 8 set/clear, bits 7–1 category |
| MG spooling disabled | NWS §2.1, SVIDs 17–20 | Correct — `health_spool_count_svid=None`; no equipment-side buffer exists |
| Link liveness at `HeartbeatInterval=0` | Workbook EC 4010002 | Not a risk — secsgem sends an HSMS linktest every 30 s |

---

## 4. Engineering findings

All reproduced against the live tree.

### F-1 (BLOCKING) — an unreachable CSV share wedges the supervisor thread

`retry_mirrors()` runs on the thread that reloads config, drains the command
inbox, replays the journal and writes `runtime_status.json` — about once a
second. It walks up to 200 tasks copying each synchronously, and
`pending_mirrors()` applies no attempt or backoff filter.

**Reproduced:** 12 tasks against a share failing after 0.5 s blocked the caller
6.12 s; an immediate second call retried all 12 (attempts 1 → 2). With a real SMB
timeout the supervisor stalls for tens of minutes — config changes missed, panel
commands unprocessed, journal not replayed, status file stale past
`SERVICE_STALE_AFTER_SEC = 120` so the panel reports the service dead.

**Fix.** `next_attempt_at` with exponential backoff, cap work per tick, move
mirroring off the supervisor thread.

### F-2 (BLOCKING) — the middleware has no CI

Both workflows are simulator-only, `pull_request` + `workflow_dispatch` only,
path-filtered to `simulator/**`, `simulator_gui/**`, `gateway/**`,
`eap_middleware/profiles.py`, `packaging/**`. A change to `service.py`,
`config.py`, `csv_store.py`, `journal.py`, `outbox.py`, `mapper.py`,
`job_tracker.py`, `gui/**` or `deploy/**` runs **zero tests**. Nothing builds or
smoke-tests the middleware installer. Nothing runs on push to `main`.

### F-3 (BLOCKING) — the control panel cannot save the file it exists to edit

`install.ps1` grants `BUILTIN\Users` Modify on `logs`, `data`, `archive`,
`machines` and (non-inheriting) the install root, deliberately excluding `app\`.
`production.yaml` lives in `app\config`, so it stays read-only for a standard
user. The desktop shortcut runs `pythonw -m gui.app` unelevated and
`save_config_atomic()` writes its temp file into that directory. The operator
gets "Save failed — Access is denied", while the installer's closing line reads
*"Everything from here is done in the control panel."* No test covers the ACL.

**Fix.** Grant Users Modify on `app\config` only, or move the live config to
`%PROGRAMDATA%\ASTAR EAP\config` — already the second entry in
`candidate_config_paths()`.

### F-4 (MAJOR) — a CSV failure is recorded against the wrong sink

In `_dispatch_event` the publish path runs first and calls `mark_dispatched(seq)`
on success. The CSV append then runs unguarded; if it raises, `_dispatch_entry`
catches it and calls `mark_dispatch_failed(seq)` — overwriting a successful
publish, and after ten attempts marking the entry dropped, claiming a publish
that did happen never did. The journal keeps separate `dispatch_status` and
`csv_status` columns precisely so the sinks stay independent; the exception path
collapses them.

### F-5 (MAJOR) — a display name can silently disable a machine's CSV output

`display_name` is validated only as "non-empty string", then interpolated into
the lot filename `f"{display_name}_Lot_{…}_LP{port_safe}{suffix}"` — only the
port is sanitised.

**Reproduced:** `TOOL:1`, `LINE\TOOL`, `TOOL*1`, `TOOL|1` and `..` are all
accepted by the config loader; `LINE-A/TOOL_1` already fails on POSIX, and every
one is illegal in a Windows filename. The write raises, the lot buffer is
retained, dispatch is marked failed, and after ten replays the entry is parked —
that machine produces no CSV at all, with a stack trace per event as the only
signal. `display_name` is a first-class identifier (uniqueness is enforced) and
also seeds default log, data and admin directory names.

**Fix.** Validate against `[A-Za-z0-9._-]` at config load.

### F-6 (MAJOR) — JobTracker grows without bound on PTIQ

`wafer_ports` / `lot_ports` gain an entry per wafer and lot, pruned only by
`deactivate(lp)`. `ptiq_secsgem` defines **zero** state transitions, so nothing
prunes. **Reproduced:** 5,000 events → 5,000 retained entries. The other three
profiles prune correctly and their deactivate CEIDs are all subscribed — but on
MG those CEIDs (134–137, `portNCasRemoved`) sit in the `load_port_1…4` bands, so
a refused load-port band stops pruning on that machine too.

**Fix.** Bound both maps per machine (LRU, a few thousand entries).

### F-7 (MAJOR) — two lists grow for the life of the process

`PerLotCsvWriter.written_files` and `mirror_errors` are appended on every lot
write and mirror failure and never read or trimmed by production code — only by a
test. One `Path` leaked per lot file, permanently.

### F-8 (MINOR) — the mirror copy can race itself

`_write_buffer()` enqueues the mirror task before copying, so the supervisor's
next tick can pick it up mid-copy. Both use the same `dest.csv.tmp` path.
Corruption is unlikely (same source bytes) but the loser's `os.replace` raises
`FileNotFoundError`, recorded as a failure on a copy that succeeded. Claim the
row before copying.

### F-9 (MINOR) — saving from the panel destroys the config's comments

`save_config_atomic` rewrites through `yaml.safe_dump(dict(raw))`. The shipped
`production.yaml` is heavily annotated, including the manual citations justifying
each machine's settings. One save and all of it is gone.

### F-10 (MINOR) — two profiles silently inherit another vendor's timers

`nexgen_mg_series` and `ptiq_secsgem` leave `hsms_timers` empty, falling back to
the DaVinci values. For MG that is deliberate and documented (the manual states
no timers), but nothing tells the operator the tool is running another vendor's
numbers. Surface "using defaults (DaVinci values)" in the panel.

### F-11 (MINOR) — dead code

`simulator/secs_data_types.py:17–18` imports `datetime` twice;
`simulator_gui/model.py:206` computes an unused `plural`;
`simulator/secsgem_equipment.py:471` assigns an unused `pr_job_id`; 15 unused
imports across `simulator/`, `scripts/`, `eap_middleware/`. Separately,
`journal._set()` guards its column whitelist with a bare `assert`, stripped by
`python -O` — theoretical only, all callers internal.

---

## 5. Suggested order of work

1. **F-2** — middleware CI. Everything below is safer once a test run gates it.
2. **F-3** — config write access. A first install currently dead-ends at the panel.
3. **F-1** — mirror backoff, off-thread. One unreachable share takes the supervisor down.
4. **F-5, F-6, F-7, F-4** — small, self-contained, each with a clear test.
5. **M-1** — MG process-metric bands. Largest functional gain; needs tool time, so start the vendor conversation early.
6. **M-2, M-3, M-4, M-5** — correctness and commissioning documentation.
7. **M-6 – M-11, F-8 – F-11** — hygiene and fidelity.

> **Caution on M-1.** Subscribing to a variable the tool does not implement
> returns `DRACK=4` and rejects the entire S2F33. The MG manual disclaims its own
> constants in §2 — "CEIDs, Variable IDs, and the numbers assigned to equipment
> processing states may change" — and the profile is documentation-derived, not
> hardware-verified. Add new variables in their own bands and validate against
> real hardware before promoting them.
