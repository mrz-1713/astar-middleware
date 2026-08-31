# NexGen MG Series Simulator — Operator Guide

A standalone Windows build of the MG Series SECS/GEM equipment simulator. No
Python installation is required on the machine that runs it.

It replays a realistic MG lot: cassette placed, mapped, processing started,
per-wafer events from a process module, ready-to-unload, cassette removed —
one lot per load port, the two lots running **concurrently** in two different
process modules.

## Why this exists

The `nexgen_mg_series` machine profile was transcribed entirely from the NWS MG
Series SECS/GEM Documentation V1.1.18. It has never been connected to an MG
tool, and the manual disclaims its own constants. This simulator is how the
profile gets exercised end-to-end before the install rather than during it.

## Start it

Double-click one of:

| Shortcut | What it does |
|---|---|
| `start-passive.bat` | Simulator listens on port 5051. Middleware must use `hsms_mode: active`. |
| `start-active.bat` | Simulator dials the middleware. Middleware must use `hsms_mode: passive`. Edit the IP in the file first. |
| `start-band-refusal-demo.bat` | Refuses the GEM300 subscription band. |
| `start-host-offline-demo.bat` | Tool starts in HOST OFF-LINE. |

Stop it with Ctrl+C.

## Connect the middleware

In `production.yaml`, set the `NEXGEN_MG_01` machine to `enabled: true`, point
`host`/`port` at the simulator, and keep `request_online: true`.

## What each demo should show

**Normal run** — two per-lot CSV files, one per load port, no rows interleaved
between them. Each file has `Loaded → Mapped → Lot_Start → Wfr_Start/Wfr_End …
→ Lot_End → Unloaded`, with `LoadPort` 1 or 2 throughout and `Chamber` showing
`PM1` or `PM2`. This is the property the MG has and the other tools do not: the
originating load port is inside every process-module event, so nothing has to
be inferred.

The run also exercises everything that has no CSV column and only ever shows up
in Linkstuffs telemetry: the cassette slot map (with slot 2 deliberately
reported cross-slotted), the recipe-selection event, and the per-lot summary —
wafer counts, cycle times and the full 30-value per-medium chemistry block on
lot completion. That summary is the largest positional decode in the profile,
so if the dashboard shows its values shifted by one, the report VID order and
the profile layout have drifted apart; regenerate with
`python -m scripts.gen_mg_subscription`.

**Refused band** — the middleware log must show

```
Subscription band 'gem300' was REFUSED (…); remaining bands continue independently
Event subscription partially applied - accepted: [...] | refused: ['gem300']
```

and must still write both lot CSV files. If a single refused band silences the
whole feed, band isolation is broken. The middleware also reads the tool's own
enabled-collection-event list back and logs which requested CEIDs are missing —
the refused band's CEIDs should be listed there, and only those.

**Host off-line** — with `request_online: true` the middleware sends S1F17, the
simulator moves out of HOST OFF-LINE, and events flow. Set `request_online:
false` and re-run to see the silent-failure mode it prevents: HSMS Selected, a
clean identity exchange, and no events at all. Note that an operator-selected
EQUIPMENT OFF-LINE is refused by design — only an operator can clear that.

## Useful flags

Run `MGSimulator.exe --help` for the full list.

| Flag | Purpose |
|---|---|
| `--wafers N` | Wafers per lot (default 3). |
| `--interval S` | Seconds between events (default 0.5). |
| `--loop` | Keep producing new lots. |
| `--refuse-band NAME` | Refuse one band: `core_gem`, `load_port`, `process_module_1`, `process_module_2`, `recipe`, `gem300`, `metrology_aux`. |
| `--start-offline` | Start in HOST OFF-LINE. |
| `--no-substrate-ids` | Behave as a cassette tool with no GEM300 substrate IDs, so `WaferID` falls back to the slot number. |
| `--process-state-ascii` | Report ProcessState as ASCII instead of an integer (the manual specifies both). |
| `--no-alarm` | Do not fire the sample alarm. |

## Limits

This is a functional integration-test tool, not SEMI conformance
certification, and not a substitute for connecting to the real tool. It
implements the constants as transcribed — if the tool disagrees with the
manual, the simulator will agree with the manual and the tool will not.
