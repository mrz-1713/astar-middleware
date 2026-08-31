# Deep Audit — 2026-08-21 (second pass)

Independent re-audit of the middleware **and** the simulator against every page
of `docs/`. Prior audits (`DEEP_AUDIT_2026-08-20.md`,
`DEEP_AUDIT_2026-08-21.md`, `HANDOVER_PRODUCTION_READINESS_2026-08-20.md`) were
treated as claims to re-verify from the vendor documents, not as findings to
repeat.

Suite: **659 passed, 1 skipped, 5 deselected** before → **665 passed, 1 skipped,
5 deselected** after (6 tests added, 1 stale test corrected).
`ruff --select=F,E9` clean on all five production packages.

---

## 0. Document coverage, and how it was established

All 14 PDFs (1,160 pages) plus the DaVinci vendor workbook were processed. Two
methods, because neither alone is sufficient:

1. **Text** — `pdftotext -layout` on all 14, then `pdftotext -bbox-layout` with
   word-level x/y clustering for tables whose cells wrap.
2. **Images** — every page rendered at 150 dpi into labelled 20-up contact
   sheets and read; `pdfimages -list` used to inventory embedded rasters per
   page so screenshot-bearing pages could be re-rendered at 300 dpi.

**Correction to a prior audit's method note.** `DEEP_AUDIT_2026-08-21.md` states
the two SECS manuals are "almost entirely vector-drawn … so their tables have no
structure in extracted text. That is why they were read visually rather than
grepped." That is not correct, and it matters because it discards the only
mechanically checkable source in the repo:

- The NexGen §8.1 CEID table parses from plain `-layout` text at **243/243**.
- The §8.2 variable table wraps *both* the name column and the CEID column, so
  naive line-joining interleaves them — that is what makes it look unparseable.
  With `-bbox-layout` and per-page column detection it yields all **760** VIDs
  with format, description and "valid at CEID" list.
- The **Omega manual contains zero embedded raster images**, so its text layer
  is the entire document; nothing can hide in a screenshot there.

Rendering remains essential for exactly one class of content — values that exist
only inside a screenshot — which is where the DaVinci's real settings live.

| Document | Pages | Pages w/ raster ≥350×250 | Interface content |
|---|---:|---:|---|
| NexGen MG Series SECS V1.1.18 | 197 | 14 (state diagrams) | **full spec** |
| Omega SECS-II (SPTS fxP 200mm) | 231 | **0** | **full spec** |
| DaVinci Software Operation Manual | 204 | 54 | **§9.6 Host Interface only** |
| DaVinci TC User Documentation | 151 | 41 | none — §11 defers to "a separate manual" |
| DaVinci 200 User Manual | 61 | 42 | none — GEM Status colours only |
| DAVINCI 200 Maintenance Manual | 52 | 37 | none — §3.1.2 shows the panel |
| TheWizard_UM | 156 | 106 | none — metrology recipe UI |
| Manual_service_08_23Aug2011 | 48 | 38 | none — NanoStar service macros |
| PM Checklist / SafetyTest / Recovery / Macrium ×2 | 49 | 24 | none |
| MAC_TO_WINDOWS11 guide | 11 | 0 | n/a (our own doc) |

---

## 1. Conformance re-verified from source

Each row was re-derived from the document and diffed against the shipped code by
script, not read off a previous audit.

| Check | Source | Result |
|---|---|---|
| NexGen collection events | MG §8.1 p97–102 | **243 / 243 exact**, names included |
| NexGen variables | MG §8.2 p102–128 | 760 parsed; **all 707 code VIDs present, 0 absent** |
| NexGen name agreement | MG §8.2 | 22 apparent diffs → 18 are PDF line-wrap artifacts; 3 are the deliberate 2159–2161 rename; 1 is VID 3721 letter case |
| SPTS station numbers | Omega §8.3 p27 | **10 / 10 exact** |
| SPTS §8.3 station types | Omega §8.3 p27 | **14 / 14 verbatim** |
| SPTS runtime station types | Omega §24.1 p213 | **25 / 25 exact** |
| SPTS alarm ID / ON-CEID / OFF-CEID decode | Omega §8.3 p27 | formula matches all three forms |
| SPTS HSMS timers 30/5/10/5/6 | Omega Table 3 p13 | match |
| DaVinci HSMS timers 45/10/5/10/5 | SOM Figure 42 p127 **screenshot** | match |
| DaVinci TCP port 5000, Mode = **Server** | SOM Figure 42 p127 **screenshot** | matches `port: 5000`, `hsms_mode: active` |
| Omega `EventReportMsg` 67075/67083/67085 | Omega Table 6 p36 | match |
| Spooling support per profile | MG p9 · Omega p9 · SOM §9.6.2 | matches `health_spool_count_svid` wiring |
| NexGen ECIDs (only 4 and 5 exist) | MG §8.4 p131 | match |
| CSV header contract | README | **exact** |
| Offline installer dependency closure | `requirements.txt` vs `deploy/wheels/` | **every production import has a requirement and a bundled win_amd64 wheel** |

### Report layouts are a deliberate superset — and it is protocol-safe

Across **69 CEIDs** the code places a state model's identity VID into reports for
CEIDs the manual's own "valid at" column does not list (e.g. `SubstID` 2201 at
CEIDs 851–858 where the manual names only 850 and 859; `PjID` 2100 at 701–717
where it names 700 and 718). This is correct: the manual lists a representative
first/last transition per state model, and a state-transition event is useless
without knowing *which* carrier/substrate/job transitioned. It is also the fix
for the empty-report problem recorded in
[NEXGEN_MG_PROFILE_NOTES.md](NEXGEN_MG_PROFILE_NOTES.md).

Confirmed safe against the tool's own ack codes: **DRACK** (S2F33) rejects only
"at least one VID does not exist"; **LRACK** (S2F35) rejects only unknown
CEID/RPTID or an already-defined link. Neither validates a VID against a
particular CEID, so the equipment cannot refuse the superset.

---

## 2. Fixed in this pass

### F-1 (MAJOR) The spool alert told operators to do the wrong thing

`_check_event_liveness` raised health `spooled_messages_pending` with:

> "The middleware does not auto-drain the spool (no S6F23), so those events may
> not reach the dashboard. **Disable tool-side spooling** or drain it manually."

Both halves were wrong, and the advice was actively harmful.
`GatewayHost.drain_spool()` sends exactly that S6F23, wired to
`drain_spool_on_connect` and sequenced *after* the subscription rebuild so
retransmitted S6F11/S5F1 land on live report definitions. And the spool is what
preserved those events — disabling it converts a recoverable backlog into real
loss. An operator following this alert would have destroyed data the middleware
could have collected.

The alert now names the setting that fixes it.

Test: `test_event_liveness.py::test_spool_alert_points_at_drain_spool_on_connect`
(verified failing against the old wording).

### F-2 (MAJOR) `drain_spool_on_connect` was undocumented and absent for the two tools that spool

It appeared nowhere in `docs/OPERATIONS.md`, and the SPTS machine block in
`config/production.yaml` did not mention it at all. Only the NexGen block
explained its `false` — correctly, since the MG does not spool.

Both other tools do, from their own manuals: Omega p9 / §9 / ECID 4010
`SpoolEnabled`, and the DaVinci Host Interface panel showing *Spooling State* and
*Spool Full* in **two** separate manuals (SOM §9.6.2, Maintenance §3.1.2). On a
spooling tool nothing but a host S6F23 empties the spool, and
`test_simulator_protocol_fidelity.py` already records where that leads — "the bug
that produced a rig delivering nothing for six lots". The *simulator* was fixed
to self-drain; real equipment will not be.

Now documented in `OPERATIONS.md` with the per-profile evidence table, and
present with rationale in both spooling machines' config blocks.

**Resolved: the drain is now ON for both spooling machines.** The audit
recommended it and the operator accepted, so `SPTS_fxP_OMEGA_01` and
`DAVINCI200_MC4_HC1_01` ship `drain_spool_on_connect: true` in both
`config/production.yaml` and `config/production.local.yaml`. A stranded backlog
is a data-loss bug, not a tuning preference, and the call is safe on a tool that
is not spooling — it answers RSDA=2 and the drain is a no-op.

The **code** default stays `false` (`MachineConfig.drain_spool_on_connect`), so
this remains a per-machine decision. A blanket `true` would reach the NexGen MG,
whose manual documents spooling as unsupported. `NEXGEN_MG_01` stays explicitly
`false`.

Test: `test_mg_packaging_and_config.py::test_shipped_spool_drain_matches_what_each_manual_documents`
pins all three machines plus the code default.

### F-3 (MAJOR, simulator fidelity) The DaVinci simulator contradicted its own equipment constant

It advertised `TimeFormat` (ECID 4010001) `= 1` and then emitted a 12-byte
`yymmddhhmmss` clock regardless, so the constant was decorative and the 16-byte
branch — what a default-configured tool actually sends — was never exercised
end to end against the mapper.

All three vendors define the constant the same way: Omega Table 6 ECID 67
"0 = 12 byte format / 1 = 16 byte format"; NexGen §8.4 ECID 5 "0 = 12-byte /
1 = 16-byte, **default=1**"; DaVinci workbook EC sheet ECID 4010001, U1,
**Default Value = 1**, "12-byte, 16-byte, or Extended format". 16-byte is the
Y2K-compliant form they all default to.

The clock now reads the constant, so flipping the ECID changes the emitted
width. **Caveat, stated plainly:** the DaVinci workbook never spells out the
0/1/2 mapping (nor does its `Data Formats` sheet), so the 16-byte default is an
inference from SEMI E30 plus the two sibling manuals rather than a quote. The
middleware was never at risk either way — `mapper._parse_clock` dispatches on
digit length and handles 12/14/16.

An existing test, `test_davinci_svid_clock_returns_12_byte_format`, pinned the
old behaviour with the rationale "DaVinci default TimeFormat ECID=1 -> 12 bytes".
That restated the code's own comment rather than any vendor source. It has been
replaced with one that pins the **coupling** in both directions, and records the
sourcing and the inference.

Tests: `test_simulator_clock_timeformat.py` (3, verified failing pre-fix),
`test_davinci_simulator_mirror.py::test_davinci_svid_clock_width_follows_the_timeformat_ecid`.

### F-4 (MINOR) The deliberate DVID 2159–2161 rename had no comment

Independently re-derived: MG §8.2 prints `pm1BemFlowMaxPrevStep` at **both** DVID
2144 and DVID 2159, but the same table's CEID column assigns 2144 → CEID 519
(`Pm1BemStepFinished`) and 2159 → CEID **521** (`Pm2BemStepFinished`). The block
structure agrees — 3 `PrevStep` + 12 `Wafer` per module, so 2144–2158 is PM1 and
2159–2173 is PM2. `profiles.py` follows the CEID column and is right; nothing said
so, so the next person diffing against the name column would "fix" it back. The
prior audit recommended this comment; it had not been added. Now at
`eap_middleware/profiles.py:1671`.

### F-5 (hardening) `journal._pending` interpolated a column name with no whitelist

`_set` guards its interpolated column against a literal set, with a comment
explaining why it is not an `assert`. `_pending` interpolates the same way with
no guard. Both callers pass literals today, so there is no live injection path —
the guard makes that stay true. Now matches `_set`.

---

## 3. Reviewed and deliberately not changed

- **`secure_payload.py` — the HMAC does not cover the IV.** Layout is
  `IV ‖ HMAC(ciphertext) ‖ ciphertext`, so an attacker can flip IV bits without
  invalidating the MAC. Under AES-CTR that randomises the whole plaintext rather
  than allowing targeted manipulation, but it does mean the MAC does not
  authenticate the full message; `MAC(IV ‖ ciphertext)` is the standard form.
  **Not changed**: the module documents this layout as the wire format of an
  external n8n/PHP counterpart, so changing it unilaterally breaks interop with
  a peer not in this repo. The feature is disabled by default. Raise with
  whoever owns the decryptor. Everything else in the module is correct —
  encrypt-then-MAC ordering, per-message `os.urandom(16)` IV, length check
  before slicing, `hmac.compare_digest`, MAC verified before decrypting.
- **VID 3721 case** — manual `Pm2DiwO3Flow` vs code `pm2DiwO3Flow`. Lookups are
  by numeric VID, so this is cosmetic.
- **`DEFAULT_HSMS_TIMERS` is DaVinci-shaped (45/10/5/10/5)** and is what the
  NexGen and PTIQ profiles inherit. Neither manual states timers, so some default
  is required; this one is documented in `production.yaml`. Left as-is.
- **S608 ×4 (SQLite string building)** — all false positives: `journal._set` and
  `_pending` whitelist the column, `purge_old` builds `?` placeholders from
  `_TERMINAL`, `outbox` interpolates a literal constant.
- **Naive `datetime` (DTZ ×23)** — the critical path is already correct and
  deliberate: `mapper._aware()` documents that every SECS clock is bare wall time
  and attaches host-local zone, and `_parse_clock` is intentionally
  `%z`-free. The rest are in the simulator and `gateway/host.py` `received_at`,
  which `_aware()` normalises downstream.
- **`C:/` and `tmp/` at the repo root** — both gitignored, and `.gitignore`
  documents exactly why `C:/` appears (Windows-absolute defaults resolve as
  relative off Windows). Not tracked. Working as designed.

---

## 4. What is genuinely good, and worth not regressing

- **Ingress journal as the durability spine.** Two independent sink status
  columns, symmetric re-derivation under `_dispatch_lock` on both the live and
  replay paths, `holds()` guarding open lot buffers, and journal-backed buffer
  eviction that returns rows to replay rather than dropping them.
- **CSV write path.** `os.replace` over an fsynced temp plus a directory fsync;
  the network mirror is a durable queued task enqueued *before* the journal rows
  are released, and never copied on the S6F11 acknowledgement thread.
- **Offline installer.** `RELEASE_MANIFEST.sha256` is verified before anything
  executes, with a path-escape check and an incompleteness check, covered by 19
  packaging/security tests. Dependency closure is complete.
- **The comments explain *why*.** Most non-obvious code carries the failure it
  was written against. That is why this pass could re-verify rather than rediscover.

---

## 5. Open items

- **Everything is still uncommitted** on `feat/nexgen-mg-series-profile` —
  roughly 10k lines across three sessions plus this one. This is now the single
  largest risk to the work, and it is a process risk, not a code one.
- ~~**Decide `drain_spool_on_connect`**~~ — **done.** On for both spooling
  machines; MG stays off. See F-2.
- **Confirm the DaVinci `TimeFormat` mapping** with MueTec/Kontron if a real tool
  is available (see F-3). A single `S2F13` for ECID 4010001 plus one `S1F3` for
  SVID 1010005 settles it in one round-trip.
- The deferred P2 list in `HANDOVER_PRODUCTION_READINESS_2026-08-20.md` §6 is
  untouched and still accurate.
