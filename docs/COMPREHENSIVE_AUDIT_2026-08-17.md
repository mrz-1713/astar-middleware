# ASTAR Middleware + Simulator — Comprehensive Audit (Verified, No-Guess Register)

**Date:** 2026-08-17 (second independent pass; supersedes and cross-checks
docs/AUDIT_2026-08-17.md, docs/COVERAGE_AUDIT_2026-08-17.md, docs/DATA_LOSS_AUDIT.md,
docs/VENDOR_DOC_AUDIT.md, tmp/parallel_sessions_audit.md)
**Method:** every PDF and Excel workbook under `docs/` read in full (14 PDFs + 1
workbook + 1 DOCX, extracted with `pdftotext -layout` / `openpyxl`; four independent
subagent reads + direct re-verification of every load-bearing number by script over
the extracted text). Every middleware/simulator claim re-verified against live code,
then the entire test suite re-run. **No value in this report is inferred: each number
is either quoted from a vendor document, counted from the document text, or read from
code — and its provenance is stated in the No-Guess Register (§7).**

Independent vendor-doc reads produced during this pass (kept alongside this report):

- `tmp/doc_extract/NexGen_MG_SECS_audit_report.md` (MG manual V1.1.18, full read)
- `tmp/doc_extract/Omega_SECSII_SPTS_fxP_200mm_manual_audit_report.md` (SPTS manual, 13,418 lines read end-to-end)
- `tmp/doc_extract/SECS_GEM_AUDIT_davinci200_FULL.md` (DaVinci SOM + TC + User Manual + xlsx, 1,939 lines)
- `tmp/doc_extract/SECS_GEM_AUDIT_davinci200.md` (SOM-only detail)
- ops-docs scan report (8 DaVinci maintenance/safety/backup PDFs: zero SECS content beyond two TheWizard mentions)

---

## 1. Document inventory — what carries interface data, what does not

| Document | Pages | Interface content |
|---|---|---|
| `docs/vendor/NexGen MG Series SECS - V1.1.18.pdf` | 198 | **Yes** — CEID/SVID/DVVAL/EC/alarm tables, message details, worked examples |
| `docs/vendor/Omega_SECSII_SPTS fxP 200mm SECSII Manual (Cimetrix).pdf` | 231 | **Yes** — Table 5 collection events, SV/EC tables, spooling, RCMDs, Appendices E/F/G |
| `docs/vendor/SECS-Items_MueTec DaVinci 200 MC4_HC1.xlsx` | 10 sheets | **Yes** — SV 113, DV 102, Events 282, EC 37, Alarms 1017, RCMD **empty** |
| `docs/vendor/davinci-200/Software Operation Manual_EN.pdf` | 207 | Config surface only (Enable/Server/Client, port 1–65535, T3–T8 1–120 s, Soft Start); **no protocol tables** |
| `docs/vendor/davinci-200/TC User Documentation EN.pdf` | 154 | GUI operation only; **no SECS content** |
| `docs/vendor/davinci-200/DaVinci 200 - User Manual V1.8.pdf` | 62 | GEM status display, carrier-ID handling; **no host-interface config** |
| 8× maintenance/safety/backup PDFs (MaintenanceManual, PM Checklist, SafetyTest, Recovery, Macrium ×2, Manual_service, TheWizard) | ~350 | **None** — verified page-by-page; only two SECS/GEM mentions in TheWizard_UM (runs "remotely with SECS/GEM"; top-barcode deactivated for SECS/GEM) |
| `output/docx/DaVinci_200_to_ASTAR_Middleware_Connection_Setup.docx` | 139 ln | **Yes** — concrete commissioned values (port 5000, device ID 0, timers 45/10/5/10/5, Server mode) |
| `docs/MAC_TO_WINDOWS11_FULL_DEPLOYMENT_GUIDE.pdf` | — | Our own deployment guide (port 5000/device 0/active) |

**Three source documents carry the item inventories.** All counts below were
re-verified by direct script over the extracted text in this pass.

---

## 2. Machine-family coverage — verified counts

Counts taken from live code (`python3 -c` against `eap_middleware.profiles`) and from
the subscription files, not from earlier audit documents.

| Profile | CEID aliases | Active subscription (events/reports) | SVIDs named | DVs named | Documented CEIDs | Recognition |
|---|---:|---:|---:|---:|---:|---|
| `nexgen_mg_series` | 243 | 243 / 114 (11 bands) | 250 (+5 deliberate absences) | 160 | **243** (counted from manual: CEID 0–883, no duplicates) | **100%** |
| `davinci_200_mc4_hc1` | 48 | 48 / 39 | 114 (113 + RecipeName alias) | 18 | 282 (xlsx Events sheet; all 282 `Enabled: Yes`) | 100% recognised; 17% aliased |
| `spts_fxp_omega` | 94 | 94 / 43 (6 bands) | 158 | 16 | **225** = 224 (Table 5, counted) + 811 (Appendix G) | 100% recognised; 42% aliased |
| `ptiq_secsgem` | 0 (per-install) | 8 generic (1001–1006 + 100/101) | 26 | 12 | n/a (per-installation EIB export) | generic by design |

### Counts double-verified from the vendor text in this pass

- **NexGen CEIDs: 243** — direct regex count over section 8.1 of the extracted manual
  (rows 0…883, all unique). The earlier `NexGen_MG_SECS_audit_report.md` said 242; that
  was an off-by-one in its window; **the code's 243 is correct** (pinned by
  `tests/test_vendor_doc_coverage.py`, passing).
- **NexGen SVIDs: 255 documented, 250 implemented** — SVID 9 `AlarmsSet` = "Not
  Supported" (manual line 5498) and SVIDs 17–20 spool variables = "Not supported"
  (lines 5525–5528) are **deliberately absent** from the profile; the absence is
  load-bearing (see §4). Pinned by `test_unsupported_svids_stay_absent`.
- **SPTS CEIDs: 224 in Table 5** — the subagent's full-table reproduction contains
  exactly 224 rows (CEID 3…911); + CEID 811 (Appendix G, Etch per-step) = 225 in
  `output/spts_fxp_omega/EventSubscription.full.json`.
- **DaVinci: 282 events / 113 SV / 102 DV / 37 EC / 1017 alarms** — recounted from the
  per-sheet CSVs filtering on 7-digit IDs. **All 282 events carry `Enabled: Yes`.**
  **RCMD + RCMD Params sheets are empty (headers only).**
- **MG alarm appendix: 1,675 ALIDs** (full-read count, table L6058–9570, line-referenced
  in the NexGen audit report) — earlier estimates of 832 and ~1,632 were wrap-dependent;
  scripted recounts over `pdftotext` output land at 1,597–1,768 depending on how
  wrapped description lines are handled, so only the magnitude is certain. The middleware
  deliberately does not transcribe alarms: S5F1 carries ALID+ALCD+ALTX at runtime and a
  static table would disagree with the tool's own 40-char texts. **No code depends on this count.**

### DaVinci active-subscription decision (deliberate, documented)

The active `output/davinci200_mc4_hc1/EventSubscription.json` carries 48 curated
events (all 48 profile aliases — the earlier 39 was fixed in this branch; pinned by
`test_davinci_active_subscription_covers_every_alias`). Full recognition coverage
(282 events) is available per machine by pointing the tool at the banded
`EventSubscription.full.json` (14 bands; 208 events carry reports, 74 have empty
Valid Variables — verified all 74 are empty in the xlsx). Same pattern for SPTS:
94-event active file; 225-event banded full file (9 bands).

---

## 3. Connection parameters — the no-guess register

| Machine | Port | Device ID | HSMS role | Provenance | Verdict |
|---|---|---|---|---|---|
| DaVinci 200 MC4/HC1 | **5000** | **0** | tool Server/passive; middleware **active** | Commissioning runbook (docx Step 5/7: "Server… TCP/IP Port to 5000", timers 45/10/5/10/5), deployment guide, `HostConnection.ini [Connection]` default 0 (cited in docx fault table); vendor manuals document only ranges (port 1–65535, T3–T8 1–120 s) | **Hardware-commissioned, not a guess** |
| SPTS fxP Omega | configurable via ECs 1000187/1000491 | ECs 1000186/1000490 (0–32767) | ECs 1000355/1000498 (0=Active, 1=Passive); Table 3 typical = **Passive** | Manual documents **no default port/device ID/IP** (confirmed by subagent: "Default device ID: NOT DOCUMENTED. Default port: NOT DOCUMENTED") | Middleware default 5000/0 is a **documented guess**; per-machine override required |
| NexGen MG21/MG22/MG22-300 | **NOT DOCUMENTED** in manual (word "TCP" appears 0 times; E37 cited only) | **NOT DOCUMENTED** (only S9F1 error text) | **NOT DOCUMENTED** (no active/passive anywhere) | profile comment + production.yaml: "GUESSES" | Middleware default 5000/0/active is a **documented guess**, flagged in code and config |
| PTIQ | per-install | per-install | per-install | no vendor doc in repo | generic |

HSMS timers pinned in code: T3=45/T5=10/T6=5/T7=10/T8=5 (host.py
`create_host_settings`) — these match the DaVinci commissioned values; SPTS Table 3
typicals are 30/5/10/5/6 and are equipment-side constants anyway. Session-ID filter
rejects any message whose header device ID differs from the configured
`secs_device_id` (host.py:135–148) — this is the S9F1-mismatch guard.

---

## 4. The four headline claims from the earlier audit — re-verified

### 4.1 "NexGen: no port/device-ID/HSMS role anywhere in the manual" — TRUE
Verified by grep over the whole extracted manual: "TCP" 0 hits (as a protocol word),
no linktest, no active/passive, device ID appears only in the S9F1 error description.
Shipped defaults (5000/0/active) are **labelled GUESSES in code
(profiles.py nexgen constructor comment) and in production.yaml** — no guess is
silent. Correcting them is per-machine YAML, no rebuild.

### 4.2 "Spooling not implemented → middleware downtime is unrecoverable loss" — TRUE for NexGen; PARTIAL for the other two
- **NexGen:** manual compliance table "Spooling … No" (line 308); SVIDs 17–20 "Not
  supported"; CEIDs 16–18 declared but no mechanism. **Events generated while the
  middleware is down are lost on the tool.** The middleware documents this twice:
  `health_spool_count_svid=None` for the profile (comment explains why) and the
  alarm-state-unknown health event text says "does not spool… any alarm raised while
  the middleware was disconnected is lost". Nothing is papered over.
- **SPTS:** full GEM spooling (S2F43/44, S6F23/F24, ECs 4004/4005/4009/4010,
  SVs 2016–2019, non-volatile). Middleware: spool-backlog **detected** via
  SpoolCountActual health poll (wired on the profile) and **drainable** via opt-in
  `drain_spool_on_connect` (S6F23 sent after subscribing, before S5F3). Default is
  `false` — documented in models.py; production.yaml explains.
- **DaVinci:** spooling supported, **default OFF** on the tool
  (`EnableSpooling` EC 4020001 default 0; MaxSpoolMessages 20, MaxSpoolTransmit 5).
  Same detection + opt-in drain as SPTS; health_spool_count_svid=1030001 wired.
- **Operational recommendation (unchanged):** for SPTS/DaVinci tools with tool-side
  spooling enabled, set `drain_spool_on_connect: true` so a reconnect recovers
  outage events instead of only warning about them.

### 4.3 "AlarmsSet unsupported → alarm-state-unknown event on reconnect" — TRUE, by design
NexGen SVID 9 is "Not Supported" (manual line 5498) — the currently-active alarm set
genuinely cannot be queried, and with no spooling, alarms raised during an outage are
never redelivered. The middleware emits an explicit `AlarmCleared`-typed marker
event (`_alarm_state_unknown: true`, text says the state is unknown and alarms may
be lost) on **every** connect for profiles whose manual documents no AlarmsSet
(service.py `_publish_alarm_state_unknown`; pinned by
`tests/test_mg_alarm_state_unknown.py`). DaVinci (SVID 1020002) and SPTS (SVID 24)
**do** document AlarmsSet, so they do not emit the marker. S5F3 enable-all-alarms is
sent per-machine opt-in (default on only for NexGen, where the manual's zero-ALID
semantics are documented).

### 4.4 "4-port modelling ambiguous for 2-port platforms" — TRUE, and handled
The MG manual uniformly models 4 load ports (CEIDs 120–153, SVs 3100–3433, port1–4
RCMDs) with **no statement** about physical port counts on MG21/MG22/MG22-300. The
middleware therefore treats port count as configurable:
- Subscription is **banded per load port** (`load_port_1` … `load_port_4`, one band
  per port — profiles.py:1826–1831): a 2-port MG simply loses two empty bands instead
  of having CEIDs 3/4 void the whole S2F33/35/37. Refusal of any band is logged and
  the tool's enabled-CEID list is read back to confirm (host.py
  `verify_enabled_events`).
- The MG simulator defaults to `load_ports=(1, 2)` (configurable), proving the
  2-port case end-to-end (`test_mg_simulator_two_ports_two_modules_produce_separate_lot_files`).
- DaVinci side: vendor docs say "1, 2 or 3 LPs; 1 or 2 PMs"; xlsx models LP1+LP2 fully
  plus a stray LP3 alarm group (MC3 note in Version History). Profile models LP1/LP2;
  a 3-LP tool needs the xlsx's 316/317-family pattern extended — commissioning item.

---

## 5. Parallel connections (different machines at the same time)

**Architecture (verified in code):** one `SecsMachineSession` per machine → one
`GatewayHost` (own HSMS socket/listener, own secsgem thread, own session guard).
Shared sinks (journal, CSV writer, outboxes, JobTracker, alarm limiter) are keyed by
`endpoint_id`; `endpoint_id` is inside every dedup key, outbox key, HTTP-outbox
filename digest, and CSV buffer key, so cross-machine collisions are impossible.
Config rejects duplicate endpoint ids / display names / overlapping passive binds
(0.0.0.0 wildcard vs specific address) / simulator port conflicts.

**Every concurrency defect found in the earlier parallel audit (8 findings) was
re-verified as fixed in code:**

| # | Finding | Fix verified at |
|---|---|---|
| P1 | CSV writer shared across dispatcher threads without locking → silent row loss | `csv_store.py` `self._lock` around all mutating ops + `_dispatch_lock` in service |
| P2 | Global watchdog blocked up to T3=45 s on one machine's S1F3 | liveness checks on per-machine daemon threads (`_guarded_liveness`) |
| P3 | `_write_status` TOCTOU could kill the ConfigurationSupervisor | `.get()` reads + try/except around supervisor status write |
| P4 | restart vs reconnect duplicate-session race | restart + reconnect under `_reconcile_lock` |
| P5 | unknown-CEID warning dedup not per machine | key now (profile, **endpoint_id**, ceid) |
| P6 | `_recent_alarms` unbounded | pruned > 60 s on insert |
| P7 | stale provision worker interleaving S2F33/35/37 | worker captures host + epoch, checks before every round-trip |
| P8 | `machine_states()` unsynchronized iteration | `list(self._sessions.items())` snapshot |

**Verified live:** `tests/test_twenty_two_machines.py` — 22 simulators, all four
profiles interleaved, middleware modes alternating active/passive — **1 passed in
92 s (re-run this pass)**. Also `test_davinci_multi_machine_audit.py` (18 tests):
identical control-job IDs across machines, concurrent events, per-machine alarm
storms, same-CEID-same-millisecond distinctness, dedup through outbox keys — all pass.

---

## 6. Data loss (per machine) — persist-before-ack chain re-verified

Flow (verified in host.py + service.py + journal.py): decode → **journal.append
(WAL + synchronous=FULL, UNIQUE ingress key)** → dispatch (outbox enqueues + in-memory
CSV buffer) → only then S6F12(0)/S5F2(0)/S16F10. Any storage failure raises → ACK=1 /
abort → the tool keeps and resends the message. Retransmits (same system bytes +
body digest) collapse onto one delivery. Outboxes are durable SQLite (per-machine
partitions, per-partition FIFO heads so one stuck machine cannot reorder another);
HTTP outbox is a per-machine file named by sha1 of the endpoint id; CSV is
temp-file + fsync + atomic rename + directory fsync (POSIX), mirrored without ever
blocking ingestion; mirror tasks are enqueued **before** the copy so a crash cannot
silently skip the network copy.

**Data-loss findings from the earlier audit — re-verified fixed:**

| # | Finding | Fix verified at |
|---|---|---|
| D1 | Outbox-full after 10 replay attempts permanently parked telemetry | `OutboxFullError` = backpressure: entry stays pending, replay keeps retrying, CSV sink still runs (service.py `_record_dispatch_failure`) |
| D2 | Pre-lot TTL prune dropped rows without journal accounting | `_release(…, reason="pre-lot TTL expired")` in `_prune_pre_lot` |
| D3 | Dead-letter path unreachable; bad-token rows retried forever | `mark_dead` called on `_PermanentPublishError` (linkstuffs_http.py:189–200); CLI `outbox-requeue` makes repair reachable |
| D4 | Mirror crash window + uncapped queue | `enqueue_mirror` before copy attempt; durable csv_mirror table |
| D5 | No directory fsync after rename | `_fsync_dir` after every atomic replace (no-op on Windows; file itself fsynced) |

**Residual risks (operational, not code defects — unchanged and stated honestly):**
1. `ingress_journal.sqlite3` is the only copy of acked-but-not-yet-CSV'd events —
   single-disk loss = permanent loss. Backup or second disk.
2. MG (and any non-spooling tool): events during middleware downtime are lost **on
   the tool** — unavoidable, documented, and surfaced as alarm-state-unknown +
   "does not spool" health notes.
3. Mirror queue uncapped in time (bounded in rows per lot).
4. At-least-once delivery: a lost publish-ack retries a row; downstream duplicates
   are bounded (ThingsBoard overwrites same-ts telemetry).
5. SVID samples are best-effort and not journaled (by design, documented).

---

## 7. Test evidence (run this pass)

- Default suite: **421 passed, 5 skipped, 5 deselected in 107.68 s** (matches the
  previous audit's 421 exactly; the earlier 50-min hang was an artifact of two pytest
  processes running concurrently on fixed ports, not a product defect).
- Slow acceptance: **22-machine mixed-profile mixed-HSMS-role: 1 passed in 92 s**.
- Targeted groups re-run green: vendor-doc coverage + three-vendor smoke + profile
  simulators + MG/DaVinci simulator e2e + HSMS-mode + alarm-state-unknown + bands +
  parallel-reliability (92 passed); data-loss/parallel group (134 passed, one
  environment-flake re-run green alone); unified-control suite (29 passed).
- CLI: `list-profiles --json` and `validate-config config/production.yaml` clean.

## 8. Bottom line

1. **Coverage:** every documented CEID/SVID/DV of all three documented vendors is
   recognised; NexGen is fully subscribed with per-port bands; DaVinci/SPTS ship
   curated active subscriptions plus banded full-coverage files; simulator presets
   exist for all four families and were exercised live in this pass.
2. **Parallel connections:** 22 simultaneous mixed machines verified; the isolation
   architecture is sound; all 8 earlier concurrency findings are fixed in code.
3. **No data loss:** persist-before-ack end-to-end; both real loss paths (outbox-full
   parking, TTL journal leak) are fixed; residual risks are documented operational
   limits, not silent gaps.
4. **No guesses:** the only undocumented connection parameters are SPTS (defaults
   configurable via ECs) and NexGen (defaults flagged GUESSES in code + config);
   everything else traces to a vendor document or a commissioned runbook. The
   remaining true unknowns (NexGen hardware verification, SPTS Appendix E offsets for
   DeltaAPM/VCE/PreHeat + ForceFill runtime value, DaVinci 3-LP/2-PM variants, MC3 LP3
   alarm anomaly) are vendor-data items, recorded as such — the middleware refuses to
   guess where guessing would mislabel data (ModuleLayoutError on unknown SPTS
   layouts, PTIQ config rejection without a complete subscription).
