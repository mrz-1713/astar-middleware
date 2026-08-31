# NexGen MG Series — profile provenance and known manual contradictions

Support notes for the `nexgen_mg_series` machine profile. Read this before
debugging an MG install; most of the surprises below are already known.

## Provenance

| | |
|---|---|
| Source | NWS MG Series SECS/GEM Documentation **V1.1.18**, NexGen Wafersystems GmbH, 01.04.2025 (197 pp) |
| Sections used | 8.1 Collection Events, 8.2 Data Variables / Status Variables, 3.2 Control State, 3.3 Process State |
| Hardware verified | **No.** Not one constant has been observed on a real tool. |
| Covers | MG21, MG22, MG22-300 — one superset profile |
| Generated artefact | `output/nexgen_mg_series/EventSubscription.json`, produced by `scripts/gen_mg_subscription.py` |

The manual publishes a single CEID table and a single variable table for all
three platforms, so per-variant profiles would have been subsets of identical
constants. Using one variant-neutral display name (`NEXGEN_MG_01`) also keeps
variant identification off the pre-install critical path — see the Linkstuffs
note in [LINKSTUFFS_SETUP.md](LINKSTUFFS_SETUP.md) for why the name matters.

## The manual disclaims its own constants

Section 2 states that CEIDs, VIDs and processing-state numbers "may change
without prior notice", and the change history shows constants still being added
in v1.1.16 (Nov 2024) and v1.1.18 (Apr 2025). Treat every number in the profile
as a best-effort transcription.

**The highest-value follow-up after the install is to capture real traffic and
diff it against the transcribed constants**, then record any corrections in
`tests/test_real_hardware_regressions.py` — which is exactly how the DaVinci
profile's known quirks were captured.

## Known contradictions, and how each is handled

| Contradiction | Handling |
|---|---|
| **ProcessState** is a one-byte unsigned integer in the state-model section (3.3) but ASCII in the status-variable table (8.2, SVID 15). | Neither is assumed. SVID values pass through the pipeline untouched, so both decode. Covered by `test_mg_process_state_decodes_as_integer_and_as_ascii`; the simulator can emit either via `--process-state-ascii`. |
| **Terminal services** are marked unimplemented in the compliance table yet fully documented in the message-details section. | Not used. |
| **Spooling** is marked unsupported, and all four spool status variables (SVIDs 17–20) are "Not supported", yet spooling CEIDs 16–18 are listed as maintained. | The CEIDs are declared but expected never to fire. `health_spool_count_svid` is `None`, disabling the spool-backlog check. |
| **AlarmsSet** (SVID 9) is listed but "Not Supported". | Omitted from the profile entirely. Its absence is what triggers the alarm-state-unknown event on every reconnect. |
| **Per-lot chemistry summary** data variables (VIDs 100–162) exist for load port 1 only; there is no port 2/3/4 equivalent in the appendix. | Transcribed as documented. This is a documentation asymmetry, not a transcription gap. |
| **EPD CEID stride.** The HPC/BEM/LowFlow families use a +2 stride between PM1 and PM2, but endpoint detection does not: PM1 owns 533–535 and PM2 owns 536–538, each with a third `EndpointDetected` CEID. | Handled explicitly in `_mg_ceid_aliases()`; do not "simplify" it back to a +2 stride. |
| **SVID name capitalisation is inconsistent in the appendix itself.** SVID 3521 is `pm1DiwO3Flow` (lower-case `pm`) but its PM2 twin, SVID 3721, is `Pm2DiwO3Flow` (capital `P`). Every other `pmN*` variable in the table is lower-case. | Normalised to lower-case: `_mg_svids()` generates both from one `f"pm{pm}DiwO3Flow"` loop. Adding a capitalisation special case would mirror a typo into the code. The name is only ever a telemetry label — `mapper.py` looks these up **by SVID number**, never by name — so the deviation cannot affect decoding. Do not "fix" this to match the manual byte-for-byte. |

## What the manual never says

It cites SEMI E37 in the standards table and then states **no TCP port, no
device/session ID, no active-versus-passive role and no T3–T8 timer values**.
The profile's port 5000 / device ID 0 / HSMS active are guesses matching the
other three tools. All three are per-machine configuration in
`production.yaml`, so correcting them on site never needs a rebuild — read the
real values off the tool's own SECS/GEM configuration screen.

## Design decisions worth knowing

**Load port comes from the payload, never from inference.** Process-module
events carry a complete identity block — lot ID, process program ID, carrier
ID, job ID, load slot and load port — as ordinary data variables. The profile
therefore declares no `chamber_event_ceids` and no `ceid_state_transitions`,
and the JobTracker is deliberately unused. This removes an entire class of
correlation bug that the SPTS and DaVinci profiles had to be hardened against.
**Future vendor evaluations should check for this property early** — it is the
difference between a stateless profile and a stateful one.

**WaferID uses the mapper's existing key precedence, not new code.** The GEM300
substrate ID occupies the key the canonical mapper already prefers (`WaferID`)
and the cassette load slot a lower-priority one (`SubstID`). A GEM300 tool
yields a real substrate identifier; a cassette tool degrades to the bare slot
number. There is no branching logic and no invented composite identifier.

**The subscription is banded.** `S2F33` and `S2F35` are all-or-nothing per
message, and `S2F36` has a dedicated ack code for "at least one CEID does not
exist", so one CEID a given MG variant does not implement would otherwise void
the whole subscription. The middleware issues one define/link/enable cycle per
band (`core_gem`, `load_port_1`..`load_port_4` — one band *per port*, so a
2-port MG simply loses two empty bands instead of taking every port down with
it — `slot_map`, `process_module_1`, `process_module_2`, `recipe`, `gem300`,
`metrology_aux`) and records each band's outcome separately. `slot_map`
(CEID 145) is isolated because it is the only report carrying status
variables in an event report. After subscribing it reads the tool's own enabled-collection-event
list back and logs any requested CEID that is missing — the acknowledgement
alone is not trusted.

**CEIDs with no valid data variables are enabled but not linked.** An empty
RPTID list in `S2F35` means "delete this link" — the failure that silently
voided an early DaVinci subscription. Events such as `portNCasPlaced` are
enabled without a report; the port is already known from the CEID.

**Only the ON-LINE request is ever sent.** The manual's own lot-start
walkthroughs show a host requesting REMOTE, selecting a process program,
mapping ports and issuing START. None of that is implemented — the middleware
is an observer, and those flows are useful only as a reference for what the
equipment emits when the real factory host drives it.

## Two silent-failure modes to rehearse before the trip

Both present as a completely healthy system: HSMS Selected, a clean
`S1F1`/`S1F2` identity exchange, `test-machine` printing `secs-ok`, and no
events, with nothing on screen explaining why.

1. **Tool in HOST OFF-LINE.** While OFF-LINE the equipment responds only to
   messages establishing communications or requesting ON-LINE, and *discards*
   all other primaries. Mitigated by `request_online: true`, which is on by
   default for this machine. If the tool is in **EQUIPMENT OFF-LINE** the
   request is denied by design and only an operator can clear it.
2. **Missing Linkstuffs device token.** The publisher drops events without
   error while CSV files keep being written, which makes it easy to
   misdiagnose as a Linkstuffs-side problem.

Rehearse both against the simulator before travelling:

```bash
python -m simulator.nexgen_mg_simulator --port 5051 --start-offline --loop
```

```bash
python -m simulator.nexgen_mg_simulator --port 5051 --refuse-band gem300 --loop
```

## Alarms

All alarms are enabled at connect using the enable-all form (`S5F3` with a
zero-length ALID list) rather than an enumerated list — the alarm appendix
contains roughly 1,632 identifiers across 38 pages, and any hand-written list
would miss some.

**The alarm appendix is deliberately not transcribed.** The `S5F1` alarm report
already carries identifier, severity code and text on every occurrence, so a
static table adds nothing at runtime; worse, the tool's alarm text is capped at
40 characters while the appendix descriptions are longer, so a transcribed
table would systematically disagree with what actually arrives.

Because there is no equipment-side spool, **alarms raised while the middleware
is down are permanently lost.** Plan outage windows accordingly.

## Regenerating the subscription

The profile's positional `V[]` layout and the report VID lists in
`EventSubscription.json` are two views of one table
(`NEXGEN_MG_REPORTS` in `eap_middleware/profiles/nexgen_mg/reports.py`). If they drift, every
value in the affected report decodes into the wrong column. After editing that
table:

```bash
python -m scripts.gen_mg_subscription
```

`test_subscription_file_matches_the_profile_it_was_generated_from` fails if you
forget.
