# NexGen MG Series Machine Profile — Design Spec

**Date:** 2026-08-11
**Status:** Ready for implementation
**Source document:** NWS MG Series SECS/GEM Documentation V1.1.18 (NexGen Wafersystems GmbH, 197pp, dated 01.04.2025)

---

## Problem Statement

A fourth semiconductor tool — a NexGen Wafersystems MG Series wet-processing
platform — needs to be connected to the ASTAR SECS/GEM EAP middleware and start
producing per-lot CSV files and Linkstuffs telemetry, on a committed customer
install date.

The middleware today ships three machine profiles (`spts_fxp_omega`,
`davinci_200_mc4_hc1`, `ptiq_secsgem`). Each was derived from a vendor manual
and then hardened against real hardware — the DaVinci profile in particular
carries corrections that only surfaced on contact with the tool, such as
subscribing to a CEID whose valid-variable list is empty silently deleting the
report link.

For the MG tool, none of that hardening is available:

- **There is no tool access.** No IP, no vendor contact, no captured trace, no
  simulator. The profile must be written entirely from the PDF.
- **The date is committed.** There is no window to connect, observe, correct and
  redeploy. The first time this code sees a real `S6F11` will be in a fab.
- **The manual disclaims its own constants.** Section 2 states that CEIDs, VIDs
  and processing-state numbers "may change without prior notice", and the change
  history shows constants still being added in v1.1.16 (Nov 2024) and v1.1.18
  (Apr 2025).
- **The manual omits the connection parameters entirely.** It cites SEMI E37 in
  the standards table and then never states a TCP port, a device/session ID, an
  active-versus-passive role, or any T3–T8 timer values.

The consequence is that every failure available on install day is a *silent*
one. The engineer sees HSMS Selected, a clean `S1F1`/`S1F2` identity exchange,
`test-machine` printing `secs-ok` — and no events, with nothing on screen
explaining why.

## Solution

Add a `nexgen_mg_series` machine profile covering all three MG platforms
(MG21, MG22, MG22-300) as a single superset, plus an MG equipment simulator so
the profile can be exercised end-to-end before the trip.

The profile is designed around a genuinely favourable property of this tool that
the existing three do not share: **the MG carries the originating load port
inside every process-chamber event.** On SPTS and DaVinci, attributing a chamber
event back to a load port requires the job tracker to hold correlation state
across events. On the MG, process-module events carry a complete identity block
— lot ID, process program ID, carrier ID, job ID, load slot and load port — as
ordinary data variables in the report payload. Every column of the per-lot CSV
contract can therefore be filled directly from a single event, with no stateful
inference and no status-variable polling.

Because the constants cannot be verified before the install, the design's second
concern is **blast-radius containment**: structuring the subscription so that one
wrong number degrades the feed rather than voiding it, and making the remaining
uncertainty visible rather than silent.

## User Stories

1. As a field engineer, I want a `nexgen_mg_series` profile available in the
   profile registry, so that I can connect an MG tool without writing code on
   site.
2. As a field engineer, I want the profile to cover MG21, MG22 and MG22-300
   with one identifier, so that I do not have to know the exact variant before I
   arrive.
3. As a field engineer, I want the profile to work whether or not the tool has
   FOUP/GEM300 support, so that discovering it is a cassette tool does not
   invalidate my configuration.
4. As a field engineer, I want the profile to work whether the tool has two load
   ports or four, so that port count is not a pre-install blocker.
5. As a field engineer, I want sensible HSMS defaults matching the other three
   profiles, so that I have a working starting point when the manual gives me
   none.
6. As a field engineer, I want to be able to correct the HSMS port, device ID
   and active/passive role from configuration alone, so that I can fix a wrong
   guess on site without a rebuild.
7. As a field engineer, I want the middleware to request ON-LINE at connect, so
   that a tool sitting in HOST OFF-LINE does not silently discard my entire
   subscription.
8. As a compliance-conscious operator, I want the ON-LINE request to be the only
   state-changing message the middleware ever sends, so that the read-only
   runtime promise is preserved.
9. As a fab operator, I want the middleware never to take REMOTE control of the
   tool, so that the middleware can never drive material or select recipes.
10. As a fab operator, I want an operator-set EQUIPMENT OFF-LINE switch to remain
    authoritative, so that the middleware cannot override a deliberate lockout.
11. As a field engineer, I want the event subscription split into independent
    bands by CEID family, so that one non-existent CEID cannot reject the whole
    subscription.
12. As a field engineer, I want each subscription band's acknowledgement recorded
    separately, so that I can see exactly which families of events took and which
    were refused.
13. As a field engineer, I want the middleware to read back the tool's list of
    enabled collection events after subscribing, so that I verify what actually
    took rather than trusting an acknowledgement.
14. As a field engineer, I want a health alarm when the tool is connected but
    silent while its last-event counter advances, so that an acked-but-ineffective
    subscription surfaces itself.
15. As a data consumer, I want each processed lot to produce one CSV file per
    load port, so that lots running concurrently on different ports do not
    interleave.
16. As a data consumer, I want the lot file to open when a cassette is placed on
    a port, so that pre-process events are captured.
17. As a data consumer, I want a Lot_Start row when processing starts on a port,
    so that I can measure lot cycle time.
18. As a data consumer, I want a Lot_End row when a port becomes ready to unload,
    so that I know when processing finished.
19. As a data consumer, I want the lot file to close and be written when the
    cassette is physically removed, so that file closure reflects material
    movement rather than a logical state.
20. As a data consumer, I want the LotID column populated from the tool's own lot
    identifier, so that rows can be joined to MES records.
21. As a data consumer, I want the Recipe column populated from the process
    program actually run on that wafer, so that I can attribute results to a
    recipe.
22. As a data consumer, I want the LoadPort column populated from the event
    itself rather than inferred, so that port attribution cannot drift when two
    process modules run concurrently.
23. As a data consumer, I want the Chamber column to distinguish process module 1
    from process module 2, so that module-to-module variation is visible.
24. As a data consumer, I want the WaferID column to contain a real substrate ID
    when the tool provides one, so that GEM300 tools give full traceability.
25. As a data consumer, I want the WaferID column to fall back to the cassette
    slot number when no substrate ID exists, so that cassette tools still
    identify wafers unambiguously within a lot.
26. As a data consumer, I want no synthetic or composite identifiers invented for
    WaferID, so that every value in the column corresponds to something the tool
    actually reported.
27. As a data consumer, I want carrier ID and job ID preserved in the raw event
    column even though they have no dedicated CSV column, so that no reported
    identity is discarded.
28. As a data consumer, I want per-lot summary telemetry (wafer counts, cycle
    times, chemistry min/max/average per lot) published to Linkstuffs, so that
    dashboards show tool performance.
29. As a data consumer, I want recipe-selection events captured with the recipe
    name and the port it was selected for, so that recipe changes are auditable.
30. As a data consumer, I want cassette slot-map results captured, so that I know
    how many wafers were present and whether any were cross- or double-slotted.
31. As an operator, I want alarms forwarded to Linkstuffs with the tool's own
    alarm text and severity code, so that I do not depend on a transcribed table
    that may disagree with the tool.
32. As an operator, I want all alarms enabled at connect without enumerating
    them, so that no alarm is missed because it was absent from a hand-written
    list.
33. As an operator, I want alarm storms rate-limited with a summary event, so
    that a misconfigured tool cannot flood the outbox.
34. As an operator, I want an explicit signal on every reconnect that alarm state
    is unknown, so that I do not trust a stale alarm picture the tool cannot
    resynchronise.
35. As an operator, I want to understand that alarms occurring while the
    middleware is down are permanently lost, so that outage windows are planned
    accordingly.
36. As a developer, I want the MG profile to reuse the existing profile data
    structure without new fields, so that the registry stays uniform across
    vendors.
37. As a developer, I want the MG profile to require no changes to the canonical
    mapper's extraction precedence, so that adding a vendor does not perturb the
    other three.
38. As a developer, I want an MG equipment simulator that speaks HSMS, so that
    the profile can be exercised end-to-end before the install.
39. As a developer, I want the simulator to emit the real report payload shapes
    for the identity and lot-summary events, so that positional decoding is
    proven rather than assumed.
40. As a developer, I want the simulator to support both active and passive
    equipment modes, so that whichever HSMS role the tool turns out to use has
    been tested.
41. As a developer, I want the simulator to be able to reject a subscription
    band, so that band isolation is proven rather than assumed.
42. As a developer, I want the simulator to run concurrent lots on two process
    modules fed from different load ports, so that per-port CSV separation is
    proven under the conditions that would break naive attribution.
43. As a developer, I want the profile's decoding to tolerate the tool reporting
    process state as either an integer or an ASCII string, so that a documented
    contradiction in the manual cannot break the identity poll.
44. As a developer, I want unknown CEIDs to map to a readable fallback event
    rather than being dropped, so that a constant that changed since publication
    is visible in the data instead of invisible.
45. As a deployment engineer, I want the MG machine template present in the
    production configuration, so that enabling the tool is an edit rather than an
    authoring task.
46. As a deployment engineer, I want documentation of the Linkstuffs
    pre-requisite that the device and its token must exist before install, so
    that the middleware does not run green while publishing nothing.
47. As a deployment engineer, I want the MG simulator packaged as a standalone
    Windows application, so that it can be demonstrated and used for
    commissioning without a Python install.
48. As a deployment engineer, I want the MG tool addable without changing the
    Windows service packaging, so that the existing installer remains valid.
49. As a support engineer, I want the profile's provenance recorded, so that a
    future reader knows which manual version the constants came from and that
    they were never hardware-verified.
50. As a support engineer, I want the known manual contradictions recorded, so
    that a future debugging session does not rediscover them.

## Implementation Decisions

### Profile identity

- Profile identifier `nexgen_mg_series`; machine display name `NEXGEN_MG_01`;
  endpoint identifier `TOOL_04`.
- A single superset profile covers MG21, MG22 and MG22-300 rather than one
  profile per variant. The source manual publishes one CEID table and one
  variable table for all three platforms, so per-variant profiles would be
  subsets of identical constants. This also removes variant identification from
  the pre-install critical path — and since the display name is the key into the
  Linkstuffs device-token map, a variant-specific name would force a token
  reissue if the variant guess were wrong.
- Vendor recorded as NexGen Wafersystems; model recorded as MG Series
  (MG21/MG22/MG22-300).
- Provenance note on the profile must state the source manual version (V1.1.18),
  and that constants are documentation-derived and not hardware-verified.

### Connection

- Defaults: port 5000, device ID 0, HSMS active — matching the other three
  profiles. The manual specifies none of these; they are guesses to be corrected
  on site from the tool's own SECS/GEM configuration screen.
- The machine ships with the ON-LINE request enabled. This is a deliberate
  departure from the default-off posture used elsewhere, justified by the
  manual's section 3.2: while OFF-LINE the equipment responds only to messages
  establishing communications or requesting ON-LINE, and *discards* all other
  primaries. Without the request, an OFF-LINE tool yields a green connect and a
  permanently empty feed.
- Only the ON-LINE request is sent. The remote-control command is explicitly not
  sent, despite the manual's own lot-start example using it, because it transfers
  operational control of the tool and falls outside the read-only runtime.
- The ON-LINE request cannot override operator intent: the manual specifies the
  equipment honours it only while HOST OFF-LINE is active, and denies it when the
  operator has physically selected EQUIPMENT OFF-LINE.

### Event subscription

- Report definition, linking and enabling are issued as **independent bands by
  CEID family** rather than one batch. The driving constraint is that the report
  and link messages are all-or-nothing: the manual states that if an error is
  detected the entire message is rejected, and the link acknowledgement has a
  distinct code for "at least one CEID does not exist". A single GEM300 CEID
  absent from a cassette tool would therefore void the entire subscription.
- Bands: core GEM lifecycle; load-port and cassette handling; process module 1;
  process module 2; recipe and process program; GEM300 job/carrier/substrate;
  metrology and auxiliary modules.
- Each band's acknowledgement is recorded independently so partial success is
  observable.
- After subscribing, the tool's enabled-collection-event list status variable is
  read back and compared against what was requested. This is the authoritative
  confirmation; acknowledgements are not trusted on their own.

### Lot lifecycle and CSV mapping

The MG exposes a port-scoped lifecycle that maps directly onto the existing
per-lot CSV convention, in which file closure is reserved for physical material
removal rather than logical lot end:

| Stage | CEID family | Event type | Closes file |
|---|---|---|---|
| Cassette placed | 130–133 (per port) | loaded | no |
| Cassette mapped | 140–143 (per port) | mapped | no |
| Processing started | 150–153 (per port) | lot_start | no |
| Ready to unload | 124–127 (per port) | lot_end | no |
| Cassette removed | 134–137 (per port) | unloaded | **yes** |

Because each of these CEIDs encodes its own port, load-port attribution for the
lifecycle comes from the per-CEID load-port map, requiring no payload inspection
and no job-tracker state.

### Identity extraction

Process-module events carry a complete identity block as data variables, valid
on CEIDs 212–216 and 221 for module 1 and 312–316 and 321 for module 2:

| Variable (PM1 / PM2) | Meaning | CSV column |
|---|---|---|
| 1901 / 2001 | current wafer lot ID | LotID |
| 1903 / 2003 | current wafer process program ID | Recipe |
| 1904 / 2004 | current wafer load slot | WaferID (fallback) |
| 1906 / 2006 | current wafer load port | LoadPort |
| 1902 / 2002 | current wafer carrier ID | raw event only |
| 1900 / 2000 | current wafer job ID | raw event only |
| 1905 / 2005 | current wafer unload slot | raw event only |
| 1907 / 2007 | current wafer unload port | raw event only |

- Load port for chamber events is taken from the payload variable, not inferred.
  Consequently the MG profile declares **no chamber-event CEIDs requiring
  job-tracker correlation** and **no lot/wafer state transitions**. The job
  tracker is deliberately unused by this profile. This is the principal
  structural difference from the DaVinci profile.
- WaferID resolution relies on the canonical mapper's **existing** key precedence
  rather than new code: the GEM300 substrate ID is exposed under the key the
  mapper already prefers, and the load slot under a lower-priority key. A GEM300
  tool therefore yields a real substrate identifier and a cassette tool degrades
  to the bare slot number, with no branching logic and no invented composite
  identifiers.
- Recipe on selection events additionally comes from the recipe-selection data
  variables (name and the port it was selected for) on the recipe-selected CEID.
- Per-port lot summary variables — wafer counts, output port, start date and
  time, total lot and process time, and per-medium chemistry min/max/average —
  are declared for the lot-completion and abort CEIDs on which the manual marks
  them valid.

### Data variable scope

Transcription covers the identity block, the per-port lot summary block, the
recipe/process-program variables and the slot maps — approximately 120 variables
of the roughly 481 in the appendix. The per-step chemistry blocks (medium, DI,
nitrogen dry, carbon dioxide, ozonated DI water), and the metrology modules
(ATMSi, roughness, endpoint detection, high-pressure clean, backside etch,
low-flow, IRM) are declared as CEIDs but their per-step variable payloads are not
transcribed in this pass. Rationale: they contribute no CSV column, they are the
largest and least verifiable portion of the table, and they can be added once
real traffic confirms the numbering.

### Alarms

- All alarms are enabled at connect using the enable-all form, rather than from
  an enumerated list. The alarm appendix contains roughly 1,632 distinct alarm
  identifiers across 38 pages.
- The alarm appendix is **not** transcribed. The alarm report message already
  carries identifier, severity code and text on every occurrence, so a static
  table adds nothing at runtime. Moreover the tool's alarm text is capped at 40
  characters while the appendix descriptions are longer, so a transcribed table
  would systematically disagree with what arrives.
- The existing alarm rate limiter applies unchanged.
- On every reconnect the middleware emits a synthetic alarm-state-unknown event.
  This is required because the alarms-set status variable is documented as *not
  supported*, so the currently-active alarm set cannot be queried, and spooling
  is unsupported, so alarms raised during an outage are never redelivered. The
  manual further warns that irrecoverable errors and attention flags may never
  send a clearing message, so no natural resynchronisation can be relied on.

### Health detection

- The last-triggered-collection-event status variable serves as the
  liveness counter, and the enabled-collection-events variable as the
  corroborating signal — the same acked-but-silent detection already used for
  DaVinci.
- No spool-count health variable is declared. The MG documents spooling as
  unsupported and its four spool status variables as not supported. This is
  itself the finding: unlike DaVinci, there is no equipment-side buffer, so any
  middleware downtime is unrecoverable data loss.

### Defensive decoding

Three contradictions in the source manual are handled rather than resolved:

- Process state is described as a one-byte unsigned integer in the state-model
  section but as ASCII in the status-variable table. Decoding accepts both.
- Terminal services are marked unimplemented in the compliance table yet fully
  documented in the message-details section. Not used.
- Spooling is marked unsupported yet spooling-related CEIDs are listed as
  maintained. Those CEIDs are declared but expected never to fire.

Unrecognised CEIDs must resolve to a readable fallback event rather than being
dropped, so that a constant which changed since publication appears in the data.

### Deployment

- A machine template for the MG tool is added to the production configuration
  template, disabled by default, with placeholder documentation addresses.
- The Linkstuffs prerequisite is documented: on the HTTPS transport the device is
  **not** auto-created and an absent device token causes events to be dropped
  silently. The device and token must exist before install.
- The Windows service packaging is unchanged — the profile ships inside the
  wheel and the machine is configuration. The MG simulator requires its own
  standalone Windows package mirroring the DaVinci simulator's.

## Testing Decisions

### What makes a good test here

Tests assert on **externally observable outputs** — the canonical event a mapper
produces, the rows and filenames a CSV writer emits, the payload a publisher
enqueues — never on internal call sequences, private helpers, or the shape of
intermediate state. A test that would fail if the profile were refactored while
still producing identical CSV output is testing the wrong thing.

The specific value being protected is that **~900 constants transcribed from a
PDF actually decode**, so tests must feed realistic positional report payloads
and assert on the resulting CSV columns, rather than asserting that a constant
equals itself.

### Primary seam — profile through CSV

The existing three-vendor smoke test is extended to four vendors. Its own
docstring identifies it as the dry run available without a SECS host or broker,
and it sits at the highest altitude that does not require a socket:

profile registry → canonical mapper → per-lot CSV writer → assert on written CSV

This single seam exercises everything the MG profile decides: CEID-to-event
resolution, positional value-array decoding against the declared layout,
load-port attribution from the payload, LotID/WaferID/Recipe extraction, and
lot-file open and close boundaries.

Cases to cover at this seam:

- Full lot lifecycle on one port: placed → mapped → started → wafer events →
  ready to unload → removed, asserting one file with correct boundaries.
- Two lots on two different ports processed concurrently, asserting two files
  with no row interleaving.
- Wafers from one port processed in process module 2, asserting the row is
  attributed to the originating port and the correct chamber.
- GEM300 substrate ID present, asserting WaferID takes the substrate ID.
- No substrate ID, asserting WaferID falls back to the slot number.
- Unknown CEID, asserting a readable fallback event rather than a drop.
- Process state delivered as integer and as ASCII, asserting both decode.

### Second seam — simulator end-to-end

Mirrors the existing DaVinci simulator end-to-end test. Required because the
primary seam cannot reach HSMS and therefore cannot prove the paths that
represent the actual install-day risk:

- Subscription band isolation: a simulator refusing one band must leave the
  remaining bands live and reporting.
- Enabled-event read-back reflecting the refused band.
- The ON-LINE request path, including a tool that starts in host-off-line.
- Both HSMS roles, since the real tool's role is unknown.
- End-to-end lot on two process modules producing correct per-port CSV files.

### Prior art

The existing suite already establishes every pattern needed: the three-vendor
smoke test for the profile-to-CSV seam; the mapper/CSV/Linkstuffs test for
publisher assertions; the DaVinci simulator end-to-end and mirror tests for
simulator-driven coverage; the HSMS-mode-per-machine test for role coverage; the
event-liveness test for acked-but-silent detection; and the real-hardware
regressions test as the home for anything the install teaches us.

### Explicitly not tested

Alarm enumeration correctness — the appendix is not transcribed, so there is
nothing to assert. Alarm handling is tested only for enable-all issuance, rate
limiting, and emission of the state-unknown event on reconnect.

## Out of Scope

- **Any write path beyond the ON-LINE request.** No remote commands, no recipe
  selection, no mapping or start commands, no process or control job creation, no
  carrier binding. The read-only runtime is unchanged.
- **A discovery command.** Dumping the tool's full variable namelist, equipment
  constants and alarm list at connect was considered and deliberately deferred,
  in favour of transcribing the profile from the manual. The underlying namelist
  primitive already exists in the gateway if this is revisited.
- **Equipment-constant namelist and list-alarms host methods.** Neither exists
  today and neither is added.
- **Transcription of the ~1,632 alarm identifiers.**
- **Transcription of the per-step chemistry and metrology variable payloads**
  (~360 variables).
- **GEM300 job and carrier orchestration.** Process job, control job and carrier
  CEIDs are subscribed and reported; the middleware never creates, binds or
  commands them.
- **Windows service packaging changes.** Only the simulator needs new packaging.
- **Linkstuffs device provisioning.** Documented as a prerequisite, not
  automated.
- **Multi-tool concurrency changes.** The MG runs as a fourth isolated session
  under the existing per-machine model.
- **Resolving the manual's contradictions with the vendor.** Handled
  defensively in code and recorded.

## Further Notes

**This profile is unverified and should be treated as such until it has run
against hardware.** Every constant came from a document that disclaims its own
constants, and the install is the first contact. The banded subscription limits
how much a wrong number can break; it cannot make a wrong number right. The
highest-value follow-up after install is to capture real traffic and diff it
against the transcribed constants, and to record any corrections in the
hardware-regressions test — which is exactly how the DaVinci profile's known
quirks were captured.

**Two silent-failure modes are worth rehearsing before the trip**, because both
present as a healthy system:

1. Tool in HOST OFF-LINE — mitigated by the ON-LINE request, but if the tool is
   in EQUIPMENT OFF-LINE only an operator can clear it.
2. Missing Linkstuffs device token — the publisher drops events without error.
   CSV files would still be written, which makes this one easy to misdiagnose as
   a Linkstuffs-side problem.

**Deliberate deviations from the manual's own example flows.** The manual's
lot-start walkthroughs show a host driving the tool: requesting REMOTE,
selecting a process program, mapping ports and issuing START. None of that is
implemented. The middleware is an observer, and the example flows are useful only
as a reference for what the equipment will emit when the real factory host does
those things.

**The favourable finding worth carrying forward.** That the MG reports load port
inside chamber events removes an entire class of correlation bug that the SPTS
and DaVinci profiles had to be hardened against. Future vendor evaluations should
check for this property early — it is the difference between a stateless profile
and a stateful one.
