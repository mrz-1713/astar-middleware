# Vendor Documentation Audit

Every vendor document in `docs/` diffed against what the middleware actually
implements. Method: `pdftotext -layout` / `pandas.read_excel`, then a
programmatic ID-and-name diff against the profile registry and each profile's
`EventSubscription.json`.

Date: 2026-08-17. Re-run the commands in each section to reproduce.

---

## Which documents carry interface data

All 14 PDFs were probed across **every page** (not a sample) for `S<n>F<n>`,
`CEID`, and `SVID` markers.

| Document | Pages | SECS content |
|---|---:|---|
| `NexGen MG Series SECS - V1.1.18.pdf` | 198 | **Yes** — 271 `SxFy`, full CEID/SVID/DVVAL appendix |
| `Omega_SECSII_SPTS fxP 200mm SECSII Manual (Cimetrix).pdf` | 231 | **Yes** — 124 `SxFy`, Table 5 collection events |
| `SECS-Items_MueTec DaVinci 200 MC4_HC1.xlsx` | 10 sheets | **Yes** — `Events`, `SV`, `DV`, `EC`, `Alarms`, `RCMD` |
| `Software Operation Manual_EN.pdf` | 207 | No — the two `SxFy` hits do not survive re-check |
| `DAVINCI 200 - MaintenanceManual V1.7.pdf` | 53 | No |
| `DaVinci 200 - User Manual V1.8.pdf` | 62 | No |
| `DaVinci 200 - Preventive Maintenance Checklist_V1.6.pdf` | 17 | No |
| `DaVinci 200 A-Star_SafetyTest V1.3_EN.pdf` | 12 | No |
| `DaVinci Recovery.pdf` | 6 | No |
| `TC User Documentation EN.pdf` | 154 | No |
| `TheWizard_UM.pdf` | 157 | No |
| `Manual_service_08_23Aug2011.pdf` | 49 | No |
| `Macrium Backup&Restore.pdf` | 11 | No |
| `Macrium Reflect Backup.pdf` | 8 | No |
| `MAC_TO_WINDOWS11_FULL_DEPLOYMENT_GUIDE.pdf` | — | No (our own doc) |

**Three source documents, not fourteen.** The rest are maintenance, safety,
backup and operator manuals with no interface content. `ptiq_secsgem` has no
vendor document at all — its numbers arrive per installation in the EIB model
export, which is why the profile documents named events but no numbers.

---

## Result summary

| Profile | Source | Documented | Implemented | Coverage | Name mismatches |
|---|---|---:|---:|---:|---:|
| `nexgen_mg_series` | 198p PDF | 243 CEID | 243 | **100%** | 0 |
| `davinci_200_mc4_hc1` | xlsx | 282 CEID | 48 | **17%** | 6 |
| `spts_fxp_omega` | 231p PDF | 224 CEID | 94 | **42%** | 0 |
| `ptiq_secsgem` | — | n/a | 6 generic | n/a | n/a |

| Profile | SVID documented | SVID implemented | Mismatches |
|---|---:|---:|---:|
| `nexgen_mg_series` | 255 | 250 | 0 (5 absences deliberate) |
| `davinci_200_mc4_hc1` | 113 | 113 | 1 |
| `spts_fxp_omega` | 158 flat + Appendix E formula | 158 | 0 |

Data variables:

| Profile | Documented | Named |
|---|---:|---:|
| `nexgen_mg_series` | 482 (162 referenced by reports) | **162 / 162 referenced** |
| `davinci_200_mc4_hc1` | 102 | **102 / 102** |
| `spts_fxp_omega` | 28 (§12.5) + 13 (§12.10), 13 shared | **28 / 28** |

Recognition coverage — every documented CEID known by name, extracted from
source and regenerable:

| Profile | Documented | Reference superset | Generator |
|---|---:|---:|---|
| `nexgen_mg_series` | 243 | **243** | `scripts/gen_mg_subscription.py` |
| `davinci_200_mc4_hc1` | 282 | **282** | `scripts/gen_davinci_full_subscription.py` |
| `spts_fxp_omega` | 224 | **224** | `scripts/gen_spts_subscription.py` |
| `ptiq_secsgem` | — | n/a | numbers arrive per installation |

**This inverts the assumption the work started from.** NexGen was believed to
be the weak profile; it is the only complete one. DaVinci and SPTS are the
incomplete ones, and DaVinci — the profile with real hardware behind it — is
the thinnest at 17%.

Everything that *is* implemented matches its source. There are no invented
CEIDs anywhere: `OURS-only` is empty for all three profiles apart from the
six generic GEM CEIDs (1001–1006) that belong to `config/EventSubscription.json`
(the generic/PTIQ file) and the built-in simulator's `GENERAL_CEIDS` —
`SPTS_CEID_ALIASES` is exactly 94 with no 1001–1006 (pinned by
tests/test_vendor_doc_coverage.py).

---

## NexGen MG Series — complete

243 of 243 CEIDs, 0 name mismatches, 0 entries absent from the manual.

Three findings, all resolved:

1. **5 SVIDs absent (9, 17–20).** Correct. The manual marks `AlarmsSet` and
   the four spool variables "Not supported", and their absence is load-bearing:
   no `AlarmsSet` is what raises alarm-state-unknown on reconnect, and no
   `SpoolCountActual` is what disables the spool-backlog check. Pinned by
   `test_unsupported_svids_stay_absent`.

2. **SVID 3721 capitalisation.** The manual is internally inconsistent —
   SVID 3521 is `pm1DiwO3Flow` but 3721 is `Pm2DiwO3Flow`. Normalised to
   lower-case; the name is only a telemetry label and `mapper.py` looks these
   up by SVID number. Recorded in `NEXGEN_MG_PROFILE_NOTES.md`.

3. **DVIDs 4305/4306 had no name.** Fixed. Both are documented in manual §8.2
   (`portIdLastMapped`, `mapResultLastMap`) and both are referenced by
   `cassetteMappedReport` (CEID 145), but neither appeared in `dvid_names`.
   Latent rather than live — the base profile already declared CEID 145's
   layout — but a regenerated subscription would have dropped that layout
   silently, since `_overlay_from_subscription` discards a layout when any DV
   name is blank. Pinned by `test_every_report_dvid_has_a_name`.

---

## DaVinci 200 MC4/HC1 — 17% transcribed, 6 name deviations

Source: `docs/vendor/SECS-Items_MueTec DaVinci 200 MC4_HC1.xlsx`, sheets `Events`
(282 IDs), `SV` (113), `DV` (120).

**SVIDs are complete: 113 of 113.**

**CEIDs are not: 48 of 282.** The 234 absent ones are mostly the `3xxxxxx`
module and state-transition families.

### The 6 name deviations are one pattern

The xlsx embeds a transition index in the name; the profile drops it:

| CEID | xlsx | ours |
|---|---|---|
| 3200001 | `ControlJob:1:NoState-Queued` | `ControlJob:NoState-Queued` |
| 3200002 | `ControlJob:10:Executing-Completed` | `ControlJob:Executing-Completed` |
| 3200003 | `ControlJob:11:Active-Completed` | `ControlJob:Active-Completed` |
| 3200008 | `ControlJob:12:Active-Completed` | `ControlJob:Active-Completed` |
| 3200013 | `ControlJob:13:Completed-NoState` | `ControlJob:Completed-NoState` |
| 3200017 | `ControlJob:5:Selected-Executing` | `ControlJob:Selected-Executing` |

This drops a distinction in one place: **3200003 and 3200008 collapse onto the
same alias.** Both resolve to canonical `lot_end`, which is correct — they are
two transition paths into the same Active→Completed outcome — so no event is
lost or misclassified. It is a deliberate normalisation, not a defect, but it
is the only place in any profile where two CEIDs share an alias, so it should
stay documented rather than be rediscovered as a bug.

### One SVID deviation

| SVID | xlsx | ours |
|---|---|---|
| 1060007 | `PM1/RecipeName` | `RecipeName` |

The module prefix is dropped. Harmless for lookup (by number), but a
multi-module tool would want the prefix back to tell PM1 from PM2.

---

## SPTS fxP Omega — 42% transcribed, 0 deviations

Source: `Omega_SECSII_SPTS fxP 200mm SECSII Manual (Cimetrix).pdf`, Table 5
(224 collection events).

**94 of 224 real CEIDs implemented. Zero name
mismatches** — everything transcribed is correct; the gap is purely absence.
(The six generic GEM CEIDs 1001–1006 are not part of the SPTS profile — they
live in `config/EventSubscription.json` and the simulator's `GENERAL_CEIDS`,
not in `SPTS_CEID_ALIASES`, see the coverage section below.)

The 130 absent CEIDs cluster into whole families:

- **Process-state transitions** (100–206) — the per-VCE state machine, both
  VCE A (`…1`) and VCE B (`…2`) variants
- **Per-module recipe events** (422–490) — PM1–PM6 recipe start/end/step
- **Wafer status** (500–515) — per arm, per PM, aligner, cooler, buffer
- **Mode changes** (610–620)
- **Door / SMIF pod** (701–762)
- **RF on/off** (900–911)

The middleware maps the lifecycle correctly today; what is missing is the
detail a dashboard would show around it.

---

## Parallel operation

`tests/test_twenty_two_machines.py` runs **22 simulators concurrently, all four
profiles interleaved**, each on its own HSMS port, and asserts every machine
connected, every machine completed a lot, and no machine reported a CEID its
own profile does not map.

**Verified passing: 1 passed in 25.76s.**

> It is marked `slow` and `pyproject.toml` sets
> `addopts = "-m 'not live and not slow'"`, so **it does not run in the default
> suite**. Run it deliberately:
>
> ```bash
> python -m pytest -m slow tests/test_twenty_two_machines.py
> ```

`tests/test_davinci_multi_machine_audit.py` (18 tests, in the default suite)
covers the data-loss surface directly: independent load-port state per machine,
identical control-job IDs across machines, concurrent events not corrupting
state, one CSV per machine with no filename collision, an alarm storm on one
machine not throttling another, the same CEID in the same millisecond from two
machines producing distinct events, and duplicate events deduplicating through
the outbox key.

The service holds no cross-machine mutable state: every runtime structure in
`EapService` is a `Dict[str, …]` keyed by `endpoint_id`, with an `RLock` around
reconciliation and a separate lock around alarm state.

---

## Round 2 — the remaining tables

### SPTS status variables: 158 of 158, complete

§12.4 (GEM Specific) has 19 and §12.8 (General Equipment Specific) has 139.
**19 + 139 = 158, exactly what the profile implements. Zero mismatches.**

> A first parse reported "186 missing, 16 name mismatches". That was wrong.
> **Appendix E is not a list of SVIDs — it is a formula:**
>
> ```
> VID = (station number * 10000) + (station type offset * 100)
>       + (variable offset) + 10000
> ```
>
> Its 201 numbered rows are *variable offsets*, not absolute SVIDs, so parsing
> them as SVIDs collided offset 22 with the real SVID 22 (`AlarmID`) and
> produced 16 phantom mismatches. Any future audit of this manual must treat
> Appendix E as generative.

### SPTS Appendix E — the vendor formula is ambiguous

This is the most consequential finding in the audit, and it explains why
Appendix E had never been implemented: **a flat SVID table cannot express it.**

880 variable offsets across 13 station-type families, resolved through:

```
VID = (station * 10000) + (type_offset * 100) + variable_offset + 10000
```

The manual spaces station types **100 apart**, but four families exceed 99
variables:

| Family | Variables | Max offset |
|---|---:|---:|
| `Statx_Etch_*` | 199 | 200 |
| `Statx_DeltaAPM_*` | 134 | 133 |
| `Statx_Deposition_*` | 108 | 107 |
| `Statx_Softetch_*` | 103 | 102 |

Their high offsets **overrun the next type's range**. At Process Module 1 alone,
**107 VIDs are claimed by two different families**:

```
VID 32500 = Statx_Etch_DSV_BackingPumpAlarm    (Etch,           offset 100)
VID 32500 = Statx_Deposition_MV_ProcessTime    (Deposition RevB, offset 0)
```

A backing-pump alarm bit and a process-time measurement are not
interchangeable. A static table would have silently picked one.

The ambiguity resolves only with the tool's layout, because a station holds
exactly one module type at a time. So it is implemented as a **layout-driven
resolver**, not a table:

```python
from eap_middleware.spts_module_vids import resolve
names = resolve({"Process Module 1": "Etch", "Process Module 2": "Deposition"})
```

`eap_middleware/spts_module_vids.py` refuses to guess: an unknown station, an
unmapped family, or a layout whose entries collide all raise
`ModuleLayoutError`.

**Three families stay deliberately unmapped** — `DeltaAPM` (134 variables),
`VCE` (17), `PreHeat` (13). The manual states no station-type offset for them.
A guessed offset would mislabel every variable in the family while looking
perfectly healthy, so they raise instead.

Offset data is extracted by `scripts/gen_spts_module_variables.py` into
`output/spts_fxp_omega/ModuleVariables.json`.

> **Two extractor bugs worth remembering.** Anchoring on the *first* "Appendix
> E" match lands in the table of contents — a 17-line window that parses to
> nothing. Leaving the end unbounded swallows Appendix F, which repeats the same
> names while documenting their enumerated values, and turns page numbers into
> variable offsets (2276 bogus rows, offsets up to 2124). Both happened here.
> `test_spts_module_variable_table_matches_the_manual_counts` pins the counts
> that caught them.

### SPTS collection events and data variables: reference superset built

`scripts/gen_spts_subscription.py` extracts Table 5 and sections 12.5/12.10
into `output/spts_fxp_omega/EventSubscription.full.json`: **225 events
(224 Table 5 + CEID 811 from Appendix G), 28 named data variables.**

Every CEID the manual documents is now recognisable. The profile's alias table
still carries 94 (the six generic GEM fallbacks 1001–1006 live in the generic
`config/EventSubscription.json`, not the SPTS alias table), because an
alias asserts a *canonical lifecycle meaning* — see the coverage section below.

**The active subscription is deliberately not repointed.** `spts_fxp_omega`
keeps its generated `output/spts_fxp_omega/EventSubscription.json` (94 mapped
events, CEIDs 3–891, no 1001–1006). `EventSubscriptionConfig.from_dict`
builds the S2F33/35/37 traffic from whichever file the machine points at, so
swapping in the 225-event superset would change what the middleware asks real
hardware to report — a per-tool commissioning decision, not a transcription fix.

Table 5 documents no per-event report layouts, so every event carries an empty
`rptids` list rather than an invented one. A link S2F35 cannot satisfy makes the
tool reject the whole message.

> **The data-variable count was wrong twice before it was right.** An initial
> parse reported 45 GEM data variables by running the §12.5 window through to
> §12.8, swallowing the equipment-constant tables of §12.6 and §12.7. The real
> figure is **28**. Separately, 13 IDs (5100–5118, 6102) appear in *both* §12.5
> and §12.10; in Rev D they carry identical names, so merging is lossless, but
> the generator now fails on a name conflict rather than silently keeping one
> side — the same failure class as the Appendix E collision.

### DaVinci: `full.json` is already complete for its own scope

`output/davinci200_mc4_hc1/EventSubscription.full.json` holds **282 events**
(208 of them carrying reports, 74 with empty `Valid Variables`) and describes
itself as "every event with valid variables … the active
EventSubscription.json is curated to the events the mapper categorizes".

Verified: the 74 xlsx events absent from it have an **empty `Valid Variables`
column — all 74, with no exceptions**. So 208 of 208 variable-bearing events are
present. The three-tier split is deliberate:

| Tier | Count | Purpose |
|---|---:|---|
| xlsx `Events` | 282 | everything the vendor documents |
| `EventSubscription.full.json` | 208 | every event that carries variables |
| `EventSubscription.json` (active) | 39 | what the mapper categorizes |
| `profile.ceid_aliases` | 48 | CEID → canonical event type |

### DaVinci data variables: 18 of 102

`DV` sheet has 102 numbered IDs, the profile carries 18. Zero name mismatches.

### DaVinci equipment constants and alarms: out of scope by design

`EC` documents 37 constants. The middleware is **read-only v1** — no production
path writes an equipment constant — so there is nothing to implement.

`Alarms` documents 1017 alarm IDs. Alarm text arrives in S5F1 at runtime; the
middleware does not need a local table to forward an alarm.

---

## Coverage: the two meanings, and why they conflict

Raising `ceid_aliases` to 100% is **not** unambiguously an improvement, and this
is the one place where "make everything 100%" needs a decision rather than a
commit.

An alias maps a CEID to a **canonical lifecycle event type** (`lot_start`,
`wafer_end`, …). That is what drives the per-lot CSV and the telemetry
classification. The 234 DaVinci and 130 SPTS CEIDs absent from the alias tables
are overwhelmingly things with **no canonical lifecycle meaning**: per-module
recipe-step start/end, RF on/off, door open/closed, SMIF pod present/absent,
mode changes, wafer-status changes per arm.

Forcing them into the alias table means inventing a canonical classification
for each. A wrong classification is worse than an absence: an absent CEID is
logged as unmapped and visible, while a misclassified one silently writes a
wrong row into a per-lot CSV that a fab reads as fact.

Two coherent targets:

1. **Recognition coverage (safe).** Every documented CEID is *known* — name,
   description, band — so nothing arrives unidentified, but only events with a
   real lifecycle meaning get a canonical type. Implemented by extending the
   subscription files, which changes no subscription traffic:
   `EventSubscriptionConfig.from_dict` builds S2F33/35/37 from the **file**, and
   `_overlay_from_subscription` only creates an alias when `_known_event_name`
   already resolves the name.
2. **Alias coverage (needs vendor input).** Every documented CEID also gets a
   canonical type. Requires deciding, per event, what lifecycle step it means —
   a vendor/process-engineering question, not a transcription one.

Recommendation: target 1 for everything, and target 2 only for events whose
lifecycle meaning is documented.

---

## Round 3 — report layouts and Appendix F

### DaVinci V[] ordering: 208 of 208 exact

The xlsx `Valid Variables` column is an *ordered* list of variable names, which
makes the positional decode checkable. Every one of the 208 events that carries
variables was diffed against the report's `dvids` sequence, resolved through the
`SV` and `DV` sheets:

**208 match, 0 mismatches, 0 unresolvable names.**

### NexGen V[] ordering: not programmatically checkable

The MG manual documents report contents **inside state-model diagrams** (section
3.3), not in a table. `pdftotext` flattens those diagrams into columns and
destroys the row association, so an ordering diff cannot be made reliable from
extracted text. `NEXGEN_MG_REPORTS` was built by a human reading those diagrams
and that remains the only way to re-verify it.

What *is* checkable, and passes: every VID any report references is a documented
variable. 139 report VIDs — 137 in the DVVAL table (§8.2), and
**2 in the Status Variable table**: `portIdLastMapped` (4305) and
`mapResultLastMap` (4306) are listed among the status variables, not the data
variables, which is why a DVVAL-window parse reports them absent. Zero name
mismatches.

### SPTS V[] ordering: nothing to check

Table 5 documents events and descriptions only. No per-event report layouts
exist in the manual, so reports must be defined per tool at commissioning.

### SPTS Appendix F — a second, incompatible station-type numbering

Appendix F §24.1 lists the values the `StationType` status variable reports at
runtime. **They are different numbers from Appendix E's type offsets, for the
same hardware**, and the manual never states the correspondence.

| Family | Appendix E offset (VID maths) | Appendix F runtime value |
|---|---:|---|
| `SDep` | 4 | 40 |
| `HSE` | 7 | 55 |
| `HeatNT` | 9 | 90, 91 |
| `Etch` | 24 | **180, 181, 182, 183, 184, 185** |
| `Deposition` | 25 | 41 |
| `Softetch` | 26 | 57 |
| `C3M` | 46 | 86 |
| `PrimaxxMonarch25` | 53 | 221 |
| `ProCve` | 58 | 123 |
| `ForceFill` | 3 | **no runtime value documented** |
| `DeltaAPM` | **no offset documented** | 169 |

Consequences, all of them load-bearing:

- **Six Etch variants** (MORI, PERIE, ICP, ISOPOD, GPE, DSI) share Appendix E's
  single offset 24. The runtime reading cannot be recovered from a VID.
- **Feeding a live `StationType` reading into the formula computes VIDs for the
  wrong module.** 180 is not 24.
- **Delta APM** reports 169 but has no offset, so its 134 variables are
  unreachable by the formula — the same conclusion the Appendix E parse reached
  from the other direction.
- **ForceFill** has an offset but no runtime value, so no tool can report itself
  as one; it is reachable only by naming the family directly.

`STATION_TYPE_VALUE_TO_FAMILY` in `eap_middleware/spts_module_vids.py` asserts
the mapping explicitly for the 15 values that have one, and
`family_for_station_type()` raises `ModuleLayoutError` for 169, for
Not Fitted/Invalid/Dummy (0/255/999), and for the transports and stations that
have no variable family at all.

---

## The audit is complete

Every table in every source document has been diffed. What remains is not
audit work:

- **Whether an absent CEID matters in practice.** Coverage is measured against
  what the vendor documents, not what a given tool emits. Only a real tool
  answers that.
- **NexGen report ordering** needs a human against the diagrams, or a live
  tool's S2F34/S2F36 acknowledgements read back.
- **Appendix E offsets for `DeltaAPM`, `VCE`, `PreHeat`**, and the runtime value
  for `ForceFill`, are absent from the manual. They need SPTS to supply them.
