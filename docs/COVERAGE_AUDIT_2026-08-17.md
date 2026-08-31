# SECS/GEM EAP Middleware — Four-Family Coverage Audit

Date: 2026-08-17 (re-run of docs/VENDOR_DOC_AUDIT.md against current tree)
Method: direct inspection of eap_middleware/profiles.py, subscription files, gateway/event_subscription.py, service/mapper/csv_store/secs_runtime, simulator/*, and a live python count of every table. All 48 tests in tests/test_vendor_doc_coverage.py + test_three_vendor_smoke.py + test_profile_simulator.py pass (10.27s).

## 1. Audit-number verification (all audit claims still true unless flagged)

Counts taken from the live code (not the doc):

| Profile | ceid_aliases | active sub events | SVIDs | DVs attached | Audit claim | Verdict |
|---|---:|---:|---:|---:|---|---|
| nexgen_mg_series | 243 | 243 | 250 | 160 | 243/243 CEIDs 100% | **TRUE** |
| davinci_200_mc4_hc1 | 48 | 39 | 114¹ | 18 | 48/282 (17%), 113 SVIDs | **TRUE** (48/282=17.0%) |
| spts_fxp_omega | 94 | 94 | 158 | 0² | 94/224 (42%), 158 SVIDs | **TRUE** (94/224=42.0%) |
| ptiq_secsgem | 0 | 6 (1001–1006) | 26 | 0² | generic, n/a | **TRUE** |

¹ 114 keys = 113 SVIDs + alias "RecipeName"→1060007 (profiles.py:799-801). ² SPTS_DVS (16) and PTIQ_DVS (12) are *defined* (profiles.py:589-606, 1137-1150) but **never attached** — both profile constructors omit dvs_by_name (profiles.py:2191-2209, 2235-2251).

Reference supersets (generated files): nexgen sub = 243 events/114 reports/162 DVIDs; davinci EventSubscription.full.json = 282 events (208 carry reports; 74 empty-Variable events with empty rptids) / 102 DVIDs; spts EventSubscription.full.json = 225 events (224 Table 5 + CEID 811 Appendix G) / 58 reports.

**Stale/incorrect statements in the audit doc to fix:**
1. docs/VENDOR_DOC_AUDIT.md:82 — "six generic GEM CEIDs (1001–1006) that spts_fxp_omega deliberately carries as fallbacks" is **false now**: SPTS_CEID_ALIASES is exactly 94 with no 1001–1006, pinned by tests/test_vendor_doc_coverage.py:199-213. The six generic CEIDs now belong to config/EventSubscription.json (ptiq) and the simulator's GENERAL_CEIDS (simulator/profile_simulator.py:55-68).
2. Audit :293 — "EventSubscription.full.json: 224 events" — file has **225** (224 + 811), asserted at tests/test_vendor_doc_coverage.py:191.
3. Audit :320 — "full.json holds 208 events" — it holds **282** events, of which 208 carry reports.

## 2. Runtime path for an unmapped CEID (recognition vs. delivery)

- The tool only sends CEIDs the middleware subscribed to. S2F33/35/37 traffic is built **entirely from the machine's EventSubscription.json** (gateway/event_subscription.py:75-95 from_file; gateway/host.py:522-591 subscribe_to_events; default file when none configured is the generic config/EventSubscription.json, gateway/host.py:558). → A documented CEID absent from the active file **never arrives**; the gap is invisible at runtime, not logged.
- If a CEID *does* arrive that the profile cannot map: profiles.py:109-127 resolve_event returns EventMapping(event_type="unknown"); mapper.py:250-263 logs a one-time WARN per (profile, ceid); csv_store.py:39-42 includes "unknown" in CSV_EVENT_TYPES so the row is **kept** in the per-lot CSV (visible, unclassified). Nothing is dropped.

## 3. Per-profile coverage tables

### 3.1 nexgen_mg_series — DOCUMENTED | IMPLEMENTED | GAP

| Item | Documented | Implemented | Gap |
|---|---|---|---|
| CEIDs | 243 (manual §8.1) | 243 aliases (profiles.py:1641-1784) + 243 subscribed | none |
| SVIDs | 255 | 250 (profiles.py:1234-1306); 5 absences deliberate (AlarmsSet 9, spool 17-20; profiles.py:1235-1240) | none by design |
| DVs | 482 (162 referenced) | 162 named + 114 reports (profiles.py:1366-1454, 1500-1616) | 320 unreferenced DVs unnamed |
| Report ordering | manual state diagrams only | human-built NEXGEN_MG_REPORTS (profiles.py:1473-1616) | **not programmatically verifiable** |
| Connection params | E37 cited, no port/device-ID/role in manual | defaults port 5000 / dev id 0 / active are **guesses** (profiles.py:2256-2262) | must be set per machine |
| Hardware proof | — | none; manual disclaims its own constants (profiles.py:1159-1162, 2286-2301) | **unverified against hardware** |
| Variants | MG21/MG22/MG22-300 one table | one superset profile + per-port bands (profiles.py:1792-1835) | 2-port MG silently loses load_port_3/4 bands (by design) |
| CEID 145 report | cassetteMapped | status variables in an event report (profiles.py:1600-1610) | sole untested assumption; isolated in slot_map band |
| Health | LastEventID/EventsEnabled | wired (profiles.py:2279-2280); spool health off (unsupported) | none |

### 3.2 davinci_200_mc4_hc1 — DOCUMENTED | IMPLEMENTED | GAP

| Item | Documented | Implemented | Gap |
|---|---|---|---|
| CEIDs | 282 (xlsx Events) | 48 aliases (profiles.py:1008-1066) = 17%; active sub 39 | **234 unaliased; 243 not subscribed** |
| CEID families missing | — | — | 3160/3170 LP carrier 64, 3210 E87 carrier 62, 3190 PRJob 44, 3220 E90 substrate 28, 3200 ControlJob 15, 3030 spooling 3, 3060/3070 PM/TM material 4, 3100 RCMD 2, 3130-3150 state/mode 5, 3230 errors 2 |
| Active sub vs aliases | — | 39/39 recognized | 9 aliases NOT subscribed: 3010001-3 (offline/local/remote), 3160001/5/6 + 3170001/5/6 (LP1/2 CarrierArrived, LoadComplete, UnloadComplete) |
| SVIDs | 113 | 113 (profiles.py:783-898) | name deviation PM1/RecipeName→RecipeName (profiles.py:799-801) |
| DVs | 102 (xlsx DV) | 18 (profiles.py:1071-1090) | 84 unnamed in profile (all 102 named in full.json dvid_names) |
| ECs | 37 (xlsx EC) | 0 — read-only v1 (audit :339-342) | no host write path (by design) |
| Alarms | 1017 (output/davinci200_mc4_hc1/AlarmConfig.json) | runtime S5F1 text only; profile maps 3020001/2 | no local table used (by design) |
| RCMDs | RemoteCommandSuccess/Failure CEIDs (3100xxx) | **no S2F41 sent** (gateway/host.py:493-520 unused) | read-only promise holds |
| E40 | S16F9/F7 event style | ingested (gateway/host.py:105-117, 739-797) with DV labels | coarser data; health alarm e40_mode |
| Health | LastEventID/EventsEnabled/Spool | wired (profiles.py:2229-2231) | none |
| Subscription bands | — | none (single band) | one bad CEID voids the whole sub on real tool |

### 3.3 spts_fxp_omega — DOCUMENTED | IMPLEMENTED | GAP

| Item | Documented | Implemented | Gap |
|---|---|---|---|
| CEIDs | 224 (Table 5) + 811 | 94 aliases (profiles.py:699-780) = 42%; active sub = same 94 (43 with reports, 51 fire without) | **130 unaliased + unsubscribed** |
| CEID families missing | — | — | 1xx process-state transitions 53, 4xx state-change 24, 5xx wafer-status change 13, 9xx RF 12, 6xx mode-change 10, 8xx lamp/stats 8, 2xx 7, 7xx door 4 |
| SVIDs | 158 (§12.4+§12.8) | 158 (profiles.py:466-584) | none |
| Module VIDs (Appendix E) | 880 offsets, 13 families | resolver eap_middleware/spts_module_vids.py; 3 families unmapped (DeltaAPM/VCE/PreHeat) | needs SPTS offsets; ForceFill has no runtime value |
| DVs | 28 (§12.5/12.10) + App G | 16 in SPTS_DVS but **profile.dvs_by_name empty** | no DV-name lookup / E40 labelling |
| Health | LastCEID=34, EventsEnabled=30, SpoolCountActual=2016 exist in SPTS_SVIDS | **none wired** (profile has no health_* svids) | liveness watchdog disabled for SPTS |
| ECs | §12.6/12.7 tables | none (read-only) | by design |
| Subscription bands | — | none (single band) | one bad CEID voids whole sub |
| Report layouts | Table 5 "Valid DVs" | 43 of 94 active events carry reports (profiles.py:616-652) | 51 events subscribed report-less |

### 3.4 ptiq_secsgem — DOCUMENTED | IMPLEMENTED | GAP

| Item | Documented | Implemented | Gap |
|---|---|---|---|
| CEIDs | none (per-installation EIB export) | 0 aliases (profiles.py:2241); active sub = generic 6 events 1001-1006 (config/EventSubscription.json) | real machine MUST ship event_subscription_path — enforced (config.py:513-534) |
| SVIDs | per-EIB; Cimetrix conventions | 26 (profiles.py:1106-1134), overridable via SvidList.json | per-install numbers |
| DVs | — | 12 in PTIQ_DVS but profile.dvs_by_name empty | none in profile |
| Process events | SCHn.*, ProcessingStarted/Completed named | named aliases (profiles.py:2127-2186) work only via subscription-file names | numbers always per-machine |
| Health | — | none | liveness watchdog off |
| Simulator | — | ProfileSimulator falls back to GENERAL_CEIDS (profile_simulator.py:187-198) | **seam gap**: GENERAL_CEIDS fires process CEIDs 100/101 that the shipped sub (1001-1006) neither subscribes nor aliases → "unknown" rows |

## 4. HSMS / connection features

- All four profiles: default_port 5000, default_secs_device_id 0 (profiles.py:2195-96, 2214-15, 2238-39, 2261-62).
- active/passive both supported per machine: config.py:289-294 (validation), models.py:121-125 (hsms_mode/hsms_bind_address), gateway/host.py:1041-1084 (create_host_settings; T3=45/T5=10/T6=5/T7=10/T8=5 pinned; session-ID filtering host.py:135-148). Device ID range 0..32767, port 1..65535 (config.py:299-300). Covered by tests/test_hsms_mode_per_machine.py.
- Only NexGen's parameters are documented guesses (profiles.py:2256-2262).

## 5. Simulator presets per profile

| Profile | preset (production default = "profile") | emits | full-sweep option |
|---|---|---|---|
| spts_fxp_omega | ProfileSimulator (service.py:785-786) | canonical flow, real SPTS CEIDs (profile_simulator.py:111-132, 344-353) | --replay-all / replay_all (profile_simulator.py:355-384) |
| davinci_200_mc4_hc1 | ProfileSimulator, or davinci_advanced→SecsGemEquipment (service.py:775-778) | 10 CEIDs scripted lot (secsgem_equipment.py:480-558) | replay_all via ProfileSimulator only |
| ptiq_secsgem | ProfileSimulator → GENERAL_CEIDS 1001-1006 (profile_simulator.py:55-68, 187-198) | generic GEM lot | sweep of 6 only |
| nexgen_mg_series | ProfileSimulator, or nexgen_advanced→NexGenMgSimulator (service.py:779-784) | 31/243 lot script (nexgen_mg_simulator.py:132) | replay_all → all 243 (nexgen_mg_simulator.py:570-590, event_replay.py) |

Simulator GUI offers every profile id (simulator_gui/app.py:66, 205-217). Verified: tests/test_profile_simulator.py:63-76 (per-profile flow CEIDs), test_twenty_two_machines.py (22 machines, all four profiles, active+passive interleaved), test_mg_simulator_e2e.py, test_davinci_simulator_e2e.py.

## 6. Read-only promise

- No S2F41 (remote command) caller anywhere; gateway/host.py:493-520 defines send_remote_command marked "Unused on the read-only runtime".
- The complete set of host-initiated messages: S2F33/35/37 subscription (secs_runtime.py:166-175), S1F17 request ON-LINE (secs_runtime.py:201-208; opt-in, default **false except nexgen**, config.py:468-469), S6F23 drain spool (secs_runtime.py:226-227; opt-in default false), S5F3 enable alarms (secs_runtime.py:233-237; opt-in, default false except nexgen).
- Only NexGen takes the state-changing S1F17 by default (production.yaml:163-176 documents why; it does not take REMOTE control). No profile takes control (no S2F17/S1F19/S2F41).

## 7. Top 10 prioritized coverage fixes

1. **DaVinci: subscribe the 208 variable-bearing events (recognition tier).** Promote output/davinci200_mc4_hc1/EventSubscription.full.json into a banded active subscription (events keep canonical aliases only where resolve_event maps them; others publish as observed telemetry). Risk: changes S2F33/35/37 traffic to real hardware — commissioning decision per audit :349-381. **L**
2. **SPTS: band the active subscription and repoint to a 224-event superset.** Add MG-style band fields (profiles.py:1792-1835 pattern) so one wrong constant degrades one family instead of voiding the whole S2F33/35/37 (gateway/event_subscription.py:366-444). **M**
3. **SPTS: wire health SVIDs.** Set health_last_event_svid=34 (LastCEID), health_events_enabled_svid=30, health_spool_count_svid=2016 on the profile (all already in SPTS_SVIDS, profiles.py:476-483) — gives SPTS the acked-but-silent watchdog DaVinci/MG have (service.py:1722-1819). **S**
4. **Attach SPTS_DVS and PTIQ_DVS to their profiles** (dvs_by_name=SPTS_DVS / PTIQ_DVS in the constructors, profiles.py:2191-2209, 2235-2251) — enables E40 VID/V labelling (service.py:452) and DV lookups. **S**
5. **PTIQ: align simulator fallback with the shipped subscription.** Either add process CEIDs 100/101 to config/EventSubscription.json or stop ProfileSimulator emitting them for ptiq — currently the sim fires unsubscribed process events the middleware logs "unknown" (profile_simulator.py:55-68 vs config/EventSubscription.json). **S**
6. **DaVinci: subscribe the 6 LP LoadComplete/UnloadComplete aliases (3160005/6, 3170005/6)** that carry DVs; add 3010001-3 control-state events to the curated sub where ack-safe. LP CarrierArrived (3160001/3170001) needs a custom report (empty Valid Variables — audit :966-969). **S**
7. **NexGen: hardware verification pass.** 243 CEIDs + report ordering (NEXGEN_MG_REPORTS built by hand from diagrams, profiles.py:1473-1616) + connection parameters (profiles.py:2256-2262) — the manual disclaims its own constants (profiles.py:1159-1162). Requires a real MG21/MG22/MG22-300. **L**
8. **SPTS Appendix E completion:** get DeltaAPM (134), VCE (17), PreHeat (13) offsets and the ForceFill runtime value from SPTS; wire eap_middleware/spts_module_vids.py into an SVID-poll surface so a tool's live StationType resolves VIDs (currently raises ModuleLayoutError — spts_module_vids.py:63, tests/test_vendor_doc_coverage.py:140-143). **M** (vendor-blocked)
9. **Fix stale audit doc claims** — docs/VENDOR_DOC_AUDIT.md:82 (1001-1006 spts fallback now false), :293 (225 not 224), :320 (282 not 208). **S**
10. **DaVinci multi-module SVID naming:** restore "PM1/RecipeName" as the primary key (currently collapsed to "RecipeName" alias, profiles.py:799-801) so PM1 vs PM2 SVIDs stay distinguishable. **S**

