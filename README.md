# ASTAR SECS/GEM EAP Middleware

Production-oriented Python middleware for connecting semiconductor tools over
SECS/GEM and forwarding normalized equipment data to Linkstuffs Community
Edition while writing the required per-lot CSV files for local processing.

Production code lives in `eap_middleware/`. The thin `gateway/` package holds
the live HSMS host built on the [secsgem](https://github.com/bparzella/secsgem)
library, `simulator/` holds a vendor-realistic equipment simulator (including
DaVinci 200 MC4/HC1 and NexGen MG Series mirrors) used by the integration
tests, and `gui/` is the desktop control panel that drives both.

## Project layout

```
eap_middleware/   service, config, profiles, canonical mapper, outboxes
gateway/          live HSMS host on secsgem; event subscription
simulator/        one universal GEM equipment peer, plus vendor-specific models
gui/              tkinter control panel for the running service
config/           production.yaml template and the shared EventSubscription.json
output/<profile>/ generated per-profile subscription and variable tables
scripts/          deploy packaging and the vendor-document generators
packaging/        Windows build assets (installer, GUI, simulators)
deploy/           offline install payload: bundled Python + win_amd64 wheels
docs/             guides and audits
docs/vendor/      vendor source documents - the audit's inputs
tests/            pytest suite
```

Everything under `output/` is **generated**. Regenerate rather than hand-edit;
each file names its generator in a `_generated_by` field:

```bash
python -m scripts.gen_mg_subscription
python -m scripts.gen_davinci_full_subscription
python -m scripts.gen_spts_subscription
python -m scripts.gen_spts_module_variables
```

`docs/vendor/` holds the three documents every machine table is derived from —
the NexGen MG SECS manual, the SPTS fxP Omega SECS manual, and the DaVinci item
spreadsheet. They are tracked so the audit in
[docs/VENDOR_DOC_AUDIT.md](docs/VENDOR_DOC_AUDIT.md) stays reproducible.

## Production Features

- One isolated SECS/GEM session per configured machine.
- Client-selected machine profiles:
  - `spts_fxp_omega`
  - `davinci_200_mc4_hc1`
  - `ptiq_secsgem`
  - `nexgen_mg_series` (MG21/MG22/MG22-300 — documentation-derived and not
    yet hardware-verified; see
    [docs/NEXGEN_MG_PROFILE_NOTES.md](docs/NEXGEN_MG_PROFILE_NOTES.md))
- One Linkstuffs MQTT Gateway connection for all downstream machines,
  *or* a parallel HTTPS REST publisher with per-device tokens for
  Cloudflare-fronted Linkstuffs deployments (`linkstuffs_http:` section
  in `production.yaml`).
- Automatic Linkstuffs downstream device creation/linking by `display_name`
  (MQTT gateway path).
- Exact per-lot CSV contract:

```csv
Datetime,ToolEvent,EAP_ToolName,LoadPort,Chamber,LotID,WaferID,Recipe,SECSGEM_Raw_Event
```

- Admin-editable SVID files:
  - `DataCollectSwitch.json`
  - `RecipeList.json`
  - `SvidList.json`
- Hot-reloaded SVID on/off, interval, recipe inclusion, and SVID inclusion.
- Durable SQLite outbox with 30-day retention for Linkstuffs outages.
- Optional authenticated AES-256-GCM encrypted HTTPS publisher, with an
  explicit AES-256-CTR legacy compatibility mode
  (`legacy_api:` section, disabled by default, kept for re-enabling if a
  decryptor proxy is reinstated).
- Read-only v1 runtime: no production path sends process-affecting commands.

## Install

```bash
python -m pip install -r requirements.txt
```

The real SECS/GEM runtime requires `secsgem`. Unit tests that do not require
hardware can run without it.

### Offline Windows installer

`AstarMiddleware-Setup-<version>-win-x64.exe` installs the middleware onto an
air-gapped Windows 11 server with no network access: Python 3.11 (silent, all
users, on PATH), every dependency from bundled `win_amd64` wheels, the
middleware, and the desktop control panel with Start Menu and desktop
shortcuts. It needs administrator rights and defaults to `C:\SECSGEM_EAP`.
Re-running it upgrades the code and preserves `production.yaml` and the admin
JSON files.

**It does not contain the simulator.** See
[Two separate deliverables](#two-separate-deliverables) below.

**Build it with one command**, on the Windows machine, from this checkout:

```powershell
packaging\installer\build_installer.ps1
```

That stages the payload and wraps it. The result lands in
`artifacts\installer\` with a `.sha256` beside it. Prerequisite: **Inno Setup
6** — currently `innosetup-6.7.3.exe` from the [jrsoftware GitHub
releases](https://github.com/jrsoftware/issrc/releases) (the older
`jrsoftware.org/download.php/is.exe` link now serves an HTML page, not the
installer).

Staging is done in PowerShell rather than by calling
`scripts/build_deploy_package.sh`, because that script needs `python3`, `shasum`
and `zip` under those exact names and git-bash on Windows provides none of them.
Pass `-PackageDir` to reuse a payload staged elsewhere instead.

`scripts/build_deploy_package.sh` remains the way to produce the **ZIP** for the
no-installer path: extract it on the server and run `install.ps1` directly. That
is fully supported and is what the deployment guides describe.

The installer itself is only a wrapper — it lays the payload down and runs
`deploy/install.ps1`, which does the real work and verifies
`RELEASE_MANIFEST.sha256` before executing anything.

Nothing in the build reaches the network for dependencies. The `win_amd64`
wheels and the Python 3.11 installer are tracked under `deploy/`, which is what
makes the target machine's install work with no internet at all.

## Two separate deliverables

The middleware and the simulator are independent products with independent
installers. Neither contains the other.

| | Middleware | Simulator |
|---|---|---|
| Installer | `AstarMiddleware-Setup-*.exe` | `SecsGemSimulator-Setup-*.exe`, `MGSimulator-Setup-*.exe` |
| Built by | `packaging\installer\build_installer.ps1` | `packaging\secsgem_simulator\build_windows.ps1`, `packaging\mg_simulator\build_windows.ps1` |
| Form | real Python 3.11 + offline wheels | frozen PyInstaller exe, self-contained |
| Rights | administrator (Python for all users, firewall) | per-user, no administrator |
| Installs to | `C:\SECSGEM_EAP` | `%LOCALAPPDATA%\Programs\…` |
| Runs as | Windows service + GUI | operator-launched application |

They are separate because they belong on different machines: an EAP host has no
reason to carry equipment-side code, and the simulator has no reason to need
administrator rights or a system-wide Python.

**How they connect: HSMS over TCP — exactly as the middleware connects to real
equipment.** The simulator listens, the middleware dials out:

```
  equipment side                                    EAP host
 ┌────────────────────┐      HSMS / TCP           ┌──────────────────────┐
 │ SecsGemSimulator   │◄──────────────────────────┤ middleware           │
 │ passive, :5051     │   S1F1/S1F13, S2F33/35/37 │ hsms_mode: "active"  │
 │                    │   S6F11 event reports     │ runtime_mode: "real" │
 └────────────────────┘                           └──────────────────────┘
```

Point the machine's `host` and `port` at the simulator and leave
`runtime_mode: "real"`. Nothing in the middleware distinguishes a simulator from
a tool, which is the point — the path under test is the production path.

`runtime_mode: "simulated"` still exists for development *inside this
repository*, where both packages are present. On a middleware-only install it
fails that one machine with a message naming the simulator package, and every
other machine keeps running.

## Client Configuration

Start from:

```text
config/production.yaml
```

The client normally edits only:

```yaml
endpoint_id: TOOL_01
display_name: SPTS_fxP_OMEGA_01
machine_profile: spts_fxp_omega
host: 192.0.2.31  # TEST-NET placeholder; replace with the equipment IP
port: 5000
secs_device_id: 0
enabled: true
```

A tool whose collection events are numbered per installation — every
`ptiq_secsgem` machine, and any tool that renumbered its events — also needs
its own subscription file:

```yaml
event_subscription_path: C:/SECSGEM_EAP/machines/TOOL_01/EventSubscription.json
```

That one file both drives the S2F33/35/37 subscription and teaches the mapper
what came back: each `events[].name` that the profile knows becomes a CEID
alias, and each linked report's `dvids` become the positional `V[]` layout.
CEIDs the vendor manual already documents keep the manual's meaning.

ThingsBoard HTTPS is the recommended production transport. The tracked
`config/production.yaml` is a disabled, secret-free template. For repository
development, copy it to ignored `config/production.local.yaml`; for deployment,
use the installed configuration outside Git. Set each
`linkstuffs_http.device_tokens[display_name]` value to an environment reference
such as `${LINKSTUFFS_HTTP_DAVINCI_TOKEN}`. Configuration loading expands the
variable and maps the resulting token to the enabled machine whose exact
`display_name` matches the key. MQTT remains an explicit fallback and is
disabled by default.

## Commands

```bash
python -m eap_middleware list-profiles --json
python -m eap_middleware validate-config --config config/production.yaml
python -m eap_middleware init-admin-config --config config/production.yaml
python -m eap_middleware test-linkstuffs --config config/production.yaml
python -m eap_middleware test-machine --config config/production.yaml --endpoint-id ALL
python -m eap_middleware run-service --config config/production.yaml
```

`test-machine` performs HSMS Select plus an S1F1/S1F2 identity exchange.
`test-linkstuffs` performs a non-mutating HTTPS attributes query and probes
MQTT only when MQTT is enabled.

## Desktop Control Panel

`gui/` is a passive tkinter client for the always-running Windows service. It
edits every setting in `production.yaml`, reads the service's atomic status
snapshot, and submits restart/test commands through the local command inbox.
It never owns HSMS or simulator threads, so closing it leaves sessions running.

```bash
python -m gui.app --config config/production.yaml
```

- **Machines** — live per-endpoint HSMS/GEM/HTTPS/simulator state; independent
  Start, Stop, Restart and Test actions; Add/Duplicate/Remove; and structured
  connection, storage, HTTPS and simulator settings.
- **Upstream** — Linkstuffs HTTPS, Linkstuffs MQTT, and the legacy Tool Data
  API. Tokens and keys are masked until *Show secrets* is ticked.
- **Service** — install/log/data/archive paths, outbox databases, log level and
  rotation, retention, reconnect/health/stagger/liveness intervals.
- **Logs** — the selected machine log, or the global service log.

Set a machine's `runtime_mode` to `simulated` and press Start to have the
service own both sides of a loopback pair. The equipment simulator always takes
the opposite HSMS role. `runtime_mode: real` always uses the configured tool.

The Simulator section's `event_definitions` editor accepts the same
`reports`, `events`, and `dvid_names` shape as `EventSubscription.json`, plus
`dvid_types`, `dvid_values`, and `svids` entries with `svid`, `name`, `type`,
and `value`. IDs, references, lifecycle coverage, and SECS value types are
validated before saving. `implementation: davinci_advanced` or
`nexgen_advanced` selects the specialized peer for vendor-specific tests; the
default `profile` implementation remains the universal simulator.

One universal SECS/GEM simulator covers every profile — SPTS, PTIQ, NexGen MG,
DaVinci and anything added later — reading that vendor's own CEIDs, V[] layouts
and SVIDs out of the profile registry, so each machine is simulated end-to-end
through mapped events, per-lot CSVs and upstream telemetry.

Blank number fields mean "unset": the key is removed and the middleware's own
default applies.

Saves use a content-revision check and atomic replacement. If another editor
changed the YAML, the GUI refuses to overwrite it and asks for a reload.

The Windows executable is built from `packaging/gui/AstarEapGui.spec`:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\gui\build_windows.ps1
```

It produces a portable `AstarEapGui` folder and zip under `artifacts\gui\`,
with the config template bundled inside the exe. Operator documentation is
`packaging/gui/README_OPERATOR.md`.

The panel and the Windows service must not run against the same machines
simultaneously — the middleware takes a lockfile in `paths.install_dir` and the
second process refuses to start.

## Required SPTS CSV Paths

The SPTS Omega template preserves the required paths from the project slides:

```text
D:\MachineData\EAP_SPTS_fxP_OMEGA_01\csv_in
\\FILESERVER\EAP_SPTS_fxP_OMEGA_01.csv_in  # replace FILESERVER with the site host
```

## Tests

The default suite excludes external tenant tests:

```bash
python -m pytest -q
```

Live transport tests require both `EAP_RUN_LIVE_TESTS=1` and `-m live`.

The full-scale run — 22 mixed-vendor machines connected at once, each asserted
to deliver correctly mapped events — is excluded by default because it takes
minutes and opens 44 HSMS endpoints:

```bash
python -m pytest tests/test_twenty_two_machines.py -m slow
```

Hardware/simulator integration tests are skipped automatically when `secsgem`
is not installed.

## Standalone Simulator

The simulator ships as its own Windows package — a console executable plus a
control panel (`simulator_gui/`) — and needs no middleware installed to run.

Two independent settings decide which side of the link it is:

| Setting | Values | Question it answers |
|---|---|---|
| `connection.role` | `equipment` / `host` | what it pretends to be |
| `connection.mode` | `passive` / `active` | which end opens the TCP connection |

All four combinations are valid, so neither can be inferred from the other.
`check-config` prints both, plus a sentence stating what the peer must be set
to:

```bash
python -m simulator check-config --config packaging/secsgem_simulator/davinci-active.yaml
python -m simulator run --config packaging/secsgem_simulator/davinci-active.yaml
```

With `role: host` the simulator becomes the EAP side instead: it dials (or
accepts) the tool, requests ON-LINE, subscribes with S2F33/35/37, enables
alarms and logs every S6F11 it receives — see
`packaging/secsgem_simulator/host-example.yaml`. The control panel edits the
same YAML and shows the resulting wiring live:

```bash
python -m simulator_gui.app --config packaging/secsgem_simulator/simulator.yaml
```

`simulation.profile` selects which machine it pretends to be — any id from
`list-profiles`. It is one universal GEM equipment: it sends that profile's own
CEIDs, fills each report from that CEID's documented DV list, and answers S1F3
from that profile's SVID table. Steps a vendor does not have are simply not
sent (the MG documents no clamp event, so no clamp event is sent).

Where the numbers come from, in order:

1. `ceid_overrides` / `svid_values` in the config — always win.
2. The machine's `EventSubscription.json`, if one is given.
3. The profile's own published table (SPTS, DaVinci, NexGen MG).
4. The general GEM/EAP-plan CEIDs — 1001 pod arrived, 1002 lot start, 1003
   wafer start, 1004 wafer end, 1005 lot end, 1006 pod removed — used when a
   profile publishes no numbers at all (`ptiq_secsgem`). These are the same
   numbers named in the shipped `config/EventSubscription.json`, so a host and
   a simulator both left at their defaults understand each other.

```yaml
simulation:
  profile: ptiq_secsgem
  ceid_overrides: {lot_start: 4001, lot_end: 4002}   # canonical step -> CEID
  svid_values: {19: "PTIQ-TOOL-7"}                   # SVID -> S1F3 answer
```

### Full-coverage sweep (`--replay-all`)

The lot flow only fires the canonical lifecycle steps, which is a small
fraction of what each vendor documents. Reports outside that path never reach
the middleware's decoder until the tool is on the fab floor:

| Profile | Documented CEIDs | Lot flow | Sweep |
|---|---:|---:|---:|
| `nexgen_mg_series` | 243 | 10 (4.1%) | 243 (100%) |
| `spts_fxp_omega` | 100 | 11 (11.0%) | 100 (100%) |
| `davinci_200_mc4_hc1` | 48 | 11 (22.9%) | 48 (100%) |
| `ptiq_secsgem` | 6 | 6 (100%) | 6 (100%) |

`--replay-all` emits every CEID the loaded profile documents, in CEID order,
each with the V[] its own report declares:

```bash
python -m simulator.profile_simulator --profile nexgen_mg_series --port 5051 --replay-all
```

It is driven entirely by profile data — adding a CEID to a subscription file
adds it to the sweep with no code change — and values come from the same
context-aware builder the lot flow uses, so DV names carry the live lot ID,
recipe and carrier.

The sweep is **physically incoherent by design**: mutually exclusive states
both fire and transitions arrive out of order. It is a decode and subscription
sweep, not a behaviour model — keep the lot flow for behaviour, CSV and
lifecycle testing.

Or without a config file:

```bash
python -m simulator.profile_simulator --profile spts_fxp_omega --port 5050 --loop
python -m simulator.profile_simulator --profile ptiq_secsgem --ceid lot_start=4001 --svid 19=TOOL-7
```

The two hand-written simulators remain for the narrower things only they model
— `simulator/nexgen_mg_simulator.py` for banded-subscription refusals and
two-port concurrency, `simulator/secsgem_equipment.py` for full E90 substrate
and TestResults payloads.

Use Active equipment mode when the simulator connects into an HSMS-passive
middleware listener. Use Passive equipment mode when the HSMS-active
middleware connects to the simulator. Windows build and operator assets live
under `packaging/secsgem_simulator/`.

The recommended Windows artifact is
`SecsGemSimulator-Setup-1.0.0-win-x64.exe`. It embeds Python 3.11,
`secsgem`, and all runtime dependencies, installs per-user without
administrator rights, and does not use `pip` or download wheels on the target
computer. A self-contained portable ZIP is produced alongside the installer.

## Standalone NexGen MG Simulator

The MG simulator is flag-driven rather than YAML-configured, and runs two
concurrent lots on two process modules fed from two different load ports:

```bash
python -m simulator.nexgen_mg_simulator --port 5051 --wafers 3 --loop
```

It can also refuse one subscription band (`--refuse-band gem300`), start in
HOST OFF-LINE so the S1F17 ON-LINE request is exercised (`--start-offline`),
behave as a cassette tool with no GEM300 substrate IDs (`--no-substrate-ids`),
run in either HSMS role (`--hsms-mode`), and report ProcessState as ASCII or as
an integer (`--process-state-ascii`). Windows build and operator assets live
under `packaging/mg_simulator/`.

## Operations

See [docs/OPERATIONS.md](docs/OPERATIONS.md) for ASTAR middleware Windows
service setup, Linkstuffs gateway requirements, admin SVID files, and field
checks. The packaged standalone DaVinci simulator is an operator-launched
application, not a Windows service.

For the complete field workflow from a Mac to a Windows 11 server, including
remote access, ZIP transfer, installation, preparation, deployment, updates,
and rollback, see
[docs/MAC_TO_WINDOWS11_FULL_DEPLOYMENT_GUIDE.md](docs/MAC_TO_WINDOWS11_FULL_DEPLOYMENT_GUIDE.md).

## Release status

A green repository is a release *candidate*, not a fab approval. Every gate that
can be enforced from source is enforced here — CI runs the suite on Windows and
Linux, strict type checking, Ruff, Bandit, a hashed offline dependency lock, a
dependency audit with SBOM, the 22-machine scale test, and a Windows job that
installs the service, kills it to prove restart, upgrades it and injects a fault
to prove rollback.

What that cannot establish is physical: real HSMS timers and identifiers, safety
interlocks, OEM control-state behaviour, a production Linkstuffs tenant, and
power-loss and reboot behaviour on the target server.

- [docs/PRODUCTION_READINESS_AUDIT_2026-08-31.md](docs/PRODUCTION_READINESS_AUDIT_2026-08-31.md)
  — the findings and their closure state.
- [docs/PRODUCTION_RELEASE_GATES.md](docs/PRODUCTION_RELEASE_GATES.md) — the
  evidence a deployment must retain, and the per-tool commissioning matrix.
- [docs/STORAGE_CAPACITY_AND_RECOVERY.md](docs/STORAGE_CAPACITY_AND_RECOVERY.md)
  — capacity sizing, RPO/RTO, and the restore drill.
- [docs/OEM_SERVICE_ACCOUNT_HARDENING_CHECKLIST.md](docs/OEM_SERVICE_ACCOUNT_HARDENING_CHECKLIST.md)
  — equipment-side accounts and network segmentation.

`nexgen_mg_series` is documentation-derived and has not been observed on a real
tool; its connection parameters are shipped defaults, correctable in
`production.yaml` without a rebuild.
