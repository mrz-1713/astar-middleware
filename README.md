# ASTAR SECS/GEM EAP middleware

ASTAR EAP middleware connects semiconductor equipment to Linkstuffs over
SECS/GEM. It keeps one HSMS session per configured machine, subscribes to the
machine's collection events, maps vendor payloads to a common event model, and
writes both per-lot CSV files and upstream telemetry. The repository also
contains a standalone SECS/GEM simulator and two Tkinter control panels.

The middleware is the production process. The simulator is a test peer that
can run on the same computer, on another computer, or as an in-process peer
when a machine uses `runtime_mode: simulated`.

## Quick start

The commands below assume Python 3.11 and a virtual environment.

```bash
python3.11 -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

python -m eap_middleware list-profiles --json
python -m eap_middleware validate-config --config config/production.yaml
python -m eap_middleware run-service --config config/production.yaml
```

The checked-in production file is a secret-free template with every machine
disabled. Copy it to an ignored file for local work, or edit the installed
copy on the Windows EAP host. Enable a machine only after its address, port,
SECS device ID, profile, output paths, and upstream credentials are correct.

To open the middleware control panel:

```bash
python -m gui.app --config config/production.yaml
```

To run the standalone simulator with the supplied example:

```bash
python -m simulator check-config --config packaging/secsgem_simulator/simulator.yaml
python -m simulator run --config packaging/secsgem_simulator/simulator.yaml
python -m simulator_gui --config packaging/secsgem_simulator/simulator.yaml
```

The simulator GUI owns the simulator runtime. The middleware GUI is a client
of the middleware service and does not own HSMS sessions.

## How the pieces fit

```text
real tool or simulator (equipment role)
        | HSMS/TCP, S1/S2/S5/S6 messages
        v
gateway.GatewayHost and eap_middleware.secs_runtime
        | vendor CEID/DVID/SVID lookup
        v
CanonicalMapper -> JobTracker -> PerLotCsvWriter
        |                         |
        v                         v
SQLite ingress journal       local/network CSV files
        |
        +--> MQTT or HTTPS Linkstuffs publisher
              (SQLite outbox retries while upstream is unavailable)
```

`hsms_mode` is a transport setting, not a GEM role. In the normal deployment
the middleware is an HSMS active client and the tool is passive. A machine may
instead set `hsms_mode: passive` so the middleware listens for an active tool.
Each machine can use a different mode.

The simulator has the same two independent choices:

| Setting | Values | Meaning |
| --- | --- | --- |
| `connection.role` | `equipment`, `host` | Which GEM side the simulator pretends to be |
| `connection.mode` | `passive`, `active` | Which process opens the TCP connection |

Exactly one peer must be equipment and the other host. Exactly one side must
be active and the other passive.

## Repository layout

The following tree names the files that are part of the application. Generated
runtime data, caches, and local configuration are not source inputs.

```text
config/                    Public YAML and JSON configuration templates
deploy/                    Offline Windows deployment payload and installers
docs/vendor/               Vendor manuals, extracted text, images, and tables
eap_middleware/            Production service and data pipeline
gateway/                   secsgem HSMS host and SECS message definitions
gui/                       Middleware control panel
output/<profile>/          Generated subscription and variable tables
packaging/                 Windows build scripts and operator assets
scripts/                   Generators, smoke tests, and release helpers
simulator/                 Standalone equipment/host simulator
simulator_gui/             Standalone simulator control panel
tests/                     Unit, integration, packaging, and protocol tests
pyproject.toml             Pytest, Pyright, Black, Ruff, and Bandit settings
requirements*.txt          Runtime, development, and release dependency pins
```

At the repository root, `.gitignore` excludes virtual environments, caches,
local YAML, generated build folders, and runtime databases. `requirements.txt`
is the runtime install, `requirements-dev.txt` adds pytest and build tools, and
`requirements-release.lock` pins the exact release dependency set used for
offline Windows wheels.

### Production middleware package

#### `eap_middleware/`

- `__init__.py` exposes the package version and public imports.
- `__main__.py` makes `python -m eap_middleware` invoke the CLI.
- `cli.py` implements `validate-config`, `list-profiles`, `init-admin-config`,
  `test-machine`, `test-linkstuffs`, `run-service`, and `outbox-requeue`.
- `config.py` parses YAML, expands `${ENV_VAR}` and `%ENV_VAR%` references,
  validates paths, URLs, machine entries, simulator definitions, and creates
  the typed service configuration.
- `models.py` contains the immutable configuration and pipeline records:
  `MachineConfig`, `ServiceConfig`, path/upstream settings, `EventMapping`,
  and `CanonicalEvent`.
- `secs_runtime.py` owns one machine's HSMS/GEM session and its reconnect,
  subscription, polling, alarm, and event callbacks.
- `mapper.py` converts S6F11, annotated S6F13, and E40 payloads into canonical
  events and extracts lot, wafer, recipe, port, and chamber values.
- `job_tracker.py` keeps load-port and control-job state used to attribute
  events to the correct lot and chamber.
- `csv_store.py` buffers and atomically writes the required per-lot CSV rows.
- `journal.py` records acknowledged ingress before the acknowledgement is sent
  to the tool, then replays unfinished records after a restart.
- `outbox.py` is the durable SQLite retry queue used by upstream publishers.
- `linkstuffs.py` publishes MQTT connect, telemetry, and disconnect messages.
- `linkstuffs_http.py` publishes Linkstuffs HTTPS telemetry and attributes.
- `legacy_api.py` contains the optional encrypted legacy HTTP publisher.
- `secure_payload.py` implements AES-256-GCM and the explicit legacy CTR
  compatibility codec.
- `alarms.py` rate-limits ordinary alarm-set events while always allowing
  clears and safety alarms through.
- `svid_admin.py` creates and validates per-machine admin JSON files and hot
  reloads collection switches, intervals, recipes, and SVID selections.
- `storage_safety.py` watches free space and moves collection to a safe-stop
  state when configured reserves are crossed.
- `logging_setup.py` configures global and per-machine rotating logs.
- `control.py` provides atomic config writes, revision checks, status snapshots,
  and the local command inbox used by the GUI.
- `probe.py` performs the non-mutating HSMS Select and S1F1/S1F2 identity test.
- `single_instance.py` prevents two service processes from owning the same
  machine sessions and outboxes.
- `release_evidence.py` writes release evidence metadata used by packaging.
- `restore.py` verifies and restores archived operational data.
- `netinfo.py` provides local interface discovery and network diagnostics.
- `spts_module_vids.py` holds SPTS module variable aliases used by mapping.
- `tkwidgets.py` contains shared Tkinter widgets, including scrollable tabs.

#### `eap_middleware/service/`

The service is split into mixins. `core.py` composes them; `state.py` owns the
shared instance state and cross-mixin contract.

- `__init__.py` exports `EapMiddlewareService` and service helpers.
- `core.py` assembles lifecycle, dispatch, upstream, simulator, health, and
  control-plane mixins into the runnable service.
- `state.py` declares and initializes service-wide sessions, queues, writers,
  publishers, trackers, locks, and status state.
- `constants.py` contains logger names, stop limits, and service constants.
- `lifecycle.py` starts, stops, reconciles, and reconnects machine sessions.
- `session.py` supplies generation guards so callbacks from retired sessions
  cannot enter the pipeline.
- `dispatch.py` routes canonical events to CSV, journal, and publishers.
- `wiring.py` resolves a profile, subscription file, admin directory, mapper,
  and per-machine upstream route.
- `alarm_flow.py` handles alarm callbacks and rate limiting.
- `control_plane.py` reads local commands and writes the atomic status snapshot.
- `health.py` runs connection, event-liveness, and upstream health checks.
- `helpers.py` contains reconnect backoff, path resolution, and per-machine
  HTTPS outbox naming helpers.
- `http_outbox.py` wires one HTTPS queue and publisher per machine.
- `simulator_runtime.py` starts and stops an embedded simulator only for a
  machine configured with `runtime_mode: simulated`.
- `errors.py` defines service-specific exceptions, including missing simulator
  and stale-session errors.

#### `eap_middleware/profiles/`

Profiles are immutable data tables plus aliases. They do not open sockets or
write files.

- `__init__.py` re-exports the profile API and all built-in vendor tables.
- `base.py` defines `MachineProfile`, canonical transition tags, and helpers
  that overlay `EventSubscription.json` and CEID overrides.
- `registry.py` builds and registers the built-in profiles.
- `generic.py` supplies the vendor-neutral baseline SVID table.
- `spts.py` contains SPTS fxP/Omega SVID, DVID, CEID, report-layout, load-port,
  and state-transition tables.
- `davinci.py` contains MueTec DaVinci 200 MC4/HC1 tables and E40/E90/E94
  event aliases.
- `ptiq.py` contains PTIQ named event, SVID, and DVID tables. Its CEID values
  come from the individual equipment EIB export, so it uses a subscription
  file or per-machine overrides.
- `nexgen_mg/` is the NexGen MG Series profile split into focused tables:
  `bands.py` defines subscription bands, `ceids.py` maps CEIDs and transitions,
  `events.py` names event aliases, `metrics.py` describes metric groups,
  `reports.py` defines report DVID layouts, and `variables.py` defines SVIDs.

### `gateway/`

- `__init__.py` exposes gateway classes.
- `host.py` implements the secsgem GEM host, HSMS timers, S6F11/S6F13 alarm
  handling, identity reads, subscription setup, and E40 stream-16 support.
- `event_subscription.py` loads JSON reports and events and sends S2F33,
  S2F35, and S2F37, including optional subscription bands.
- `annotated_reports.py` defines S6F13/S6F14 annotated report codecs.
- `e40.py` defines the custom S16F7/S16F8/S16F9/S16F10 codecs used by DaVinci
  E40 event-report mode.
- `identity.py` provides the extended S1F2 identity response codec.
- `secsgem_compat.py` contains compatibility and cleanup workarounds for the
  pinned secsgem version.

### `simulator/`

- `__init__.py` exposes simulator classes and the simulator version.
- `__main__.py` makes `python -m simulator` invoke the simulator CLI.
- `cli.py` validates simulator YAML, prints summaries, and runs the runner.
- `config.py` validates connection role/mode, simulation values, host actions,
  retry policy, logging, and simulator-specific overrides.
- `runner.py` owns the simulator process lifecycle, signal handling, retry
  backoff, passive listener preflight, and status logging.
- `profile_simulator.py` is the universal profile-driven equipment peer. It
  reads the same CEID/DVID/SVID tables as the middleware and replays a lot.
- `host_simulator.py` is the simulator's host role. It requests ON-LINE,
  defines and enables reports, optionally drains spooled data, enables alarms,
  reads identity SVs, and logs received events.
- `secsgem_equipment.py` provides common GEM equipment handlers and event senders.
- `equipment.py` is the earlier general equipment simulator and data generator
  implementation retained for focused tests.
- `data_generator.py` models lot, wafer, process, alarm, recipe, and chamber
  values for general simulator tests.
- `event_replay.py` replays configured CEIDs and reports.
- `dv_telemetry.py` creates deterministic, self-consistent process telemetry
  for profile report DVIDs whose meaning is known.
- `secs_data_types.py` defines SEMI E5-compatible value types and standard IDs.
- `nexgen_mg_simulator.py` is the focused MG simulator for band refusals,
  two-port concurrency, offline startup, and process-state variants.

### GUIs

- `gui/app.py` is the middleware control panel. It edits YAML, validates and
  saves with a revision check, submits local commands, polls status, and shows
  service and machine logs. It never owns service HSMS threads when an external
  service is running.
- `gui/model.py` contains the panel's pure data, validation, and status helpers.
- `simulator_gui/app.py` is the standalone simulator panel. It owns a
  `SimulatorRunner` thread and shows live wiring and logs.
- `simulator_gui/model.py` contains simulator panel field definitions and
  serialization helpers.
- `simulator_gui/__init__.py` and `simulator_gui/__main__.py` expose the GUI
  version and `python -m simulator_gui` entry point.

### `config/` and `output/`

Configuration files are inputs. Output files are generated from profile tables
and should be regenerated when a source table changes.

- `config/production.yaml` is the public service template. It defines upstream
  transports, filesystem paths, storage safety, timing, and machine entries.
- `config/production.local.yaml` is an ignored local copy for development. Do
  not commit tokens, real IP addresses, or network-share paths.
- `config/EventSubscription.json` is the generic PTIQ/GEM subscription sample.
- `config/DataCollectSwitch.json` defines default SVID collection switches and
  intervals.
- `config/RecipeList.json` defines default recipe selections.
- `config/SvidList.json` defines default SVID names and IDs.
- `config/AlarmConfig.json` defines default alarm entries.
- `config/EquipmentConstant.json` defines default equipment constants.
- `config/release-evidence.example.json` documents the shape of release
  evidence metadata without containing a live release.
- `output/davinci200_mc4_hc1/` contains generated DaVinci event, alarm, SVID,
  DVID, and gateway YAML tables, plus a profile README.
- `output/nexgen_mg_series/` contains the generated MG subscription.
- `output/spts_fxp_omega/` contains generated SPTS event and module-variable
  tables.
- `output/docx/` contains generated Word connection documentation.

### `packaging/`

- `packaging/installer/AstarMiddleware.iss` is the Inno Setup definition for
  the offline middleware installer.
- `packaging/installer/build_installer.ps1` stages the middleware payload,
  verifies wheel and manifest inputs, and invokes Inno Setup.
- `packaging/installer/start-gui.bat` launches the installed middleware GUI.
- `packaging/gui/AstarEapGui.spec` is the PyInstaller spec for the GUI build.
- `packaging/gui/build_windows.ps1` builds the portable GUI directory and ZIP.
- `packaging/gui/requirements-build.txt` pins GUI build-only dependencies.
- `packaging/gui/README_OPERATOR.md` explains the packaged middleware panel.
- `packaging/secsgem_simulator/SecsGemSimulator.spec` and `.iss` define the
  standalone simulator executable and installer.
- `packaging/secsgem_simulator/build_windows.ps1` runs tests, PyInstaller,
  license collection, signing, hashing, and packaging.
- `packaging/secsgem_simulator/entrypoint.py` is the console executable entry.
- `packaging/secsgem_simulator/gui_entrypoint.py` is the packaged GUI entry.
- `packaging/secsgem_simulator/davinci-active.yaml`, `davinci-passive.yaml`,
  `host-example.yaml`, and `simulator.yaml` are role/mode examples.
- `packaging/secsgem_simulator/start-*.bat` launch the console in each example
  mode; `README_OPERATOR.md` is the operator guide; and
  `THIRD_PARTY_NOTICES.txt` contains bundled notices.
- `packaging/secsgem_simulator/smoke_*.py` smoke-test the ZIP and installer.
- `packaging/mg_simulator/MGSimulator.spec` and `.iss` define the focused MG
  executable and installer.
- `packaging/mg_simulator/build_windows.ps1` builds, signs, hashes, and packs
  the MG simulator.
- `packaging/mg_simulator/entrypoint.py` is its executable entry point.
- `packaging/mg_simulator/start-*.bat` provide active, passive, refusal, and
  offline demos; the remaining files are the operator guide, notices, build
  requirements, and smoke test.
- `packaging/sign_artifact.ps1` applies and verifies optional Authenticode
  signatures on release artifacts.

### `deploy/`

`deploy/` is the offline middleware payload. `PYTHON_VERSION.txt` matches the
bundled interpreter to the Windows wheels. `install.ps1` performs manifest
verification and installation, `upgrade.ps1` preserves operator data during an
upgrade, `Setup.ps1` and `SETUP.bat` provide the guided entry point, and the
two text files provide the operator guide and checklist. `python/` contains
the matching Python installer. `wheels/` contains the pinned `win_amd64`
packages and `README.txt` records their provenance.

### `tests/`

Tests are organized by the behavior they protect:

- Pipeline and persistence: `test_mapping_csv_linkstuffs.py`,
  `test_durable_ingress.py`, `test_outbox_maintenance.py`,
  `test_csv_buffer_eviction.py`, `test_csv_pre_lot_ttl.py`,
  `test_machine_log_routing.py`, and storage threshold coverage in
  `test_production_readiness_remediation.py`.
- Profiles and vendor behavior: `test_profile_simulator.py`,
  `test_vendor_doc_coverage.py`, `test_vendor_conformance_audit_fixes.py`,
  the `test_davinci_*` and `test_mg_*` files, `test_subscription_bands.py`,
  and `test_three_vendor_smoke.py`.
- HSMS and protocol: `test_e40.py`, `test_annotated_event_reports.py`,
  `test_hsms_mode_per_machine.py`, `test_secs_passive_loopback.py`,
  `test_secs_simulator_loopback.py`, `test_simulator_protocol_fidelity.py`,
  `test_simulator_secsgem_compat.py`, and `test_mqtt_loopback.py`.
- Simulator and GUI: `test_simulator_config.py`, `test_simulator_runner.py`,
  `test_simulator_gui.py`, `test_simulator_send_log_and_telemetry.py`,
  `test_simulator_clock_timeformat.py`, `test_host_simulator_loopback.py`,
  `test_gui.py`, and `test_gui_log_capture.py`.
- Reliability and lifecycle: `test_job_tracker.py`, `test_event_liveness.py`,
  `test_reconnect_outage_escalation.py`, `test_reliability_remediation.py`,
  `test_parallel_reliability_audit.py`, `test_service_start_unwinds.py`,
  `test_service_stop_is_bounded.py`, `test_orphaned_host_regression.py`,
  and `test_single_instance.py`.
- Configuration, security, and release: `test_production_config.py`,
  `test_svid_admin.py`, `test_secure_payload.py`, `test_deploy_packaging_security.py`,
  `test_production_readiness_remediation.py`, `test_edge_case_fixes.py`,
  `test_cli.py`, `test_guided_setup.py`, and `test_real_hardware_regressions.py`.
- Scale and live gates: `test_twenty_two_machines.py`,
  `test_transport_live.py`, and the live portions of the end-to-end tests.

`tests/conftest.py` supplies shared fixtures and marks. `tests/__init__.py`
marks the directory as a package. The test names are intentionally explicit:
when adding a profile or protocol feature, extend the smallest matching group
and add a focused regression test.

## Configuration

Start with `config/production.yaml`. The loader rejects unknown keys and bad
types rather than silently accepting a misspelled setting.

### Upstream sections

- `linkstuffs` enables the MQTT gateway. Use TLS and the broker host/port and
  token supplied by the site.
- `linkstuffs_http` enables the recommended HTTPS publisher. `base_url` is the
  origin only. Map each exact machine `display_name` to a token in
  `device_tokens`.
- `legacy_api` enables the older encrypted HTTP contract. Leave it disabled
  unless a maintained peer still requires it.

Environment references keep secrets out of YAML. For example:

```yaml
linkstuffs_http:
  enabled: true
  base_url: "https://astar-monitoring.example"
  device_tokens:
    DAVINCI200_MC4_HC1_01: "${LINKSTUFFS_DAVINCI_TOKEN}"
```

Set `LINKSTUFFS_DAVINCI_TOKEN` in the service account environment before
starting the service. The config loader expands it and fails if it is missing.

### Machine entries

Each item under `machines:` is one independent session.

| Field | Purpose |
| --- | --- |
| `endpoint_id` | Stable internal identifier used by commands and outbox files |
| `display_name` | Linkstuffs device name and part of default log/CSV paths; use only letters, digits, dot, dash, underscore |
| `machine_profile` | ID returned by `list-profiles` |
| `host`, `port` | Tool address when HSMS is active, or peer address/port when passive |
| `secs_device_id` | HSMS session/device ID, normally `0` |
| `hsms_mode` | `active` to dial, `passive` to listen |
| `enabled` | Whether the service starts this machine |
| `runtime_mode` | `real` for a tool or external simulator, `simulated` for an embedded loopback peer |
| `event_subscription_path` | Optional per-tool JSON file containing actual CEIDs and report layouts |
| `enable_alarms` | Sends S5F3 enable-all after GEM communication when true |
| `request_online` | Sends S1F17 when true; use only when the tool may be OFF-LINE |
| `drain_spool_on_connect` | Sends S6F23 to retrieve tool-side spooled events |
| `reset_subscription_on_connect` | Clears old report links before provisioning; use during commissioning only |
| `hsms_timers` | Optional per-tool T3/T5/T6/T7/T8 overrides |
| `storage` | Machine log, simulator log, CSV, network share, and admin JSON paths |
| `simulator` | Embedded simulator implementation, pacing, alarm, CEID/SVID overrides, and inline definitions |

Minimal real-machine entry:

```yaml
machines:
  - endpoint_id: TOOL_01
    display_name: SPTS_fxP_OMEGA_01
    machine_profile: spts_fxp_omega
    host: 10.10.20.31
    port: 5000
    secs_device_id: 0
    hsms_mode: active
    enabled: true
    runtime_mode: real
    event_subscription_path: config/EventSubscription.json
```

Default CSV output is `D:/MachineData/EAP_<display_name>/csv_in`. Set
`storage.local_csv_path` and `storage.network_csv_path` when the site uses
different drives or shares. The CSV header is:

```text
Datetime,ToolEvent,EAP_ToolName,LoadPort,Chamber,LotID,WaferID,Recipe,SECSGEM_Raw_Event
```

### Per-machine admin JSON

Run this once after adding machines:

```bash
python -m eap_middleware init-admin-config --config config/production.yaml
```

The command creates defaults under each machine's `admin_config_path`.

- `DataCollectSwitch.json` turns collection and individual SVID polling on or
  off and sets intervals.
- `RecipeList.json` controls recipe names included in collection.
- `SvidList.json` selects and names SVIDs for polling.
- `AlarmConfig.json` contains alarm definitions and enable state.
- `EquipmentConstant.json` contains editable equipment constants.
- `EventSubscription.json` contains reports, DVID lists, event-to-report
  links, and DVID names for a machine whose numbers differ from its profile.

The service hot-reloads supported admin files. Keep a backup of the directory
before editing it on a live host.

## Running the middleware

### Command line

```bash
# Show profile IDs and table counts
python -m eap_middleware list-profiles --json

# Parse and validate the complete YAML
python -m eap_middleware validate-config --config config/production.yaml

# Probe one machine or every enabled machine without starting the service
python -m eap_middleware test-machine --config config/production.yaml --endpoint-id TOOL_01
python -m eap_middleware test-machine --config config/production.yaml --endpoint-id ALL

# Check HTTPS tokens and MQTT TCP reachability
python -m eap_middleware test-linkstuffs --config config/production.yaml

# Run the long-lived service
python -m eap_middleware run-service --config config/production.yaml

# Re-queue dead SQLite outbox rows after correcting a token or endpoint
python -m eap_middleware outbox-requeue --config config/production.yaml
python -m eap_middleware outbox-requeue --config config/production.yaml --endpoint-id TOOL_01
```

`test-machine` performs HSMS Select and an S1F1/S1F2 identity exchange. It
does not publish to Linkstuffs. `test-linkstuffs` makes an HTTPS attributes
request and a TCP probe to MQTT when those transports are enabled.

### Middleware GUI

```bash
python -m gui.app --config config/production.yaml
```

The Machines tab edits endpoint settings and submits Start, Stop, Restart,
and Test commands. The Upstream tab edits MQTT, HTTPS, and legacy API
settings. The Service tab edits paths, retention, reconnect, health, and log
settings. The Logs tab tails the selected machine log or the global service
log. Secret fields stay masked unless `Show secrets` is selected.

When no external Windows service is available, `Run service here` starts an
`EapMiddlewareService` in a worker thread. Only one process may hold the
single-instance lock for a given installation directory.

## Running the simulator

### Standalone simulator CLI

Validate without opening a socket:

```bash
python -m simulator check-config --config packaging/secsgem_simulator/simulator.yaml
python -m simulator version
```

Run equipment or host mode:

```bash
python -m simulator run --config packaging/secsgem_simulator/davinci-passive.yaml
```

For a quick profile-only run without YAML, use the profile simulator module:

```bash
python -m simulator.profile_simulator --profile spts_fxp_omega --port 5050 --loop
python -m simulator.profile_simulator --profile ptiq_secsgem --ceid lot_start=4001 --svid 19=TOOL-7
python -m simulator.profile_simulator --profile nexgen_mg_series --port 5051 --replay-all
```

`--replay-all` walks every documented CEID and report for decode coverage. It
does not model a physically ordered lot. The normal lot flow is better for
CSV and lifecycle tests. `--subscription PATH` loads a machine's
`EventSubscription.json`; repeat `--ceid EVENT_TYPE=NUMBER` or `--svid
SVID=VALUE` for installation-specific values.

The example files are:

- `davinci-passive.yaml`: equipment listens; middleware uses `hsms_mode: active`.
- `davinci-active.yaml`: equipment dials; middleware uses `hsms_mode: passive`.
- `host-example.yaml`: simulator acts as the host and talks to a tool without
  the middleware.
- `simulator.yaml`: general GUI-friendly equipment template.

The equipment role replays a lot using the selected profile's CEIDs, reports,
and SVIDs. The host role performs the same opening sequence as the middleware:
optional S1F17, S2F33/S2F35/S2F37 subscription, optional S6F23 spool drain,
optional S5F3 alarm enable, and optional S1F3 identity reads.

`simulation.profile` accepts any ID printed by `list-profiles`. PTIQ has no
fixed CEID table, so provide `simulation.ceid_overrides` or a subscription
file. Example:

```yaml
simulation:
  profile: ptiq_secsgem
  ceid_overrides:
    lot_start: 4001
    lot_end: 4002
  svid_values:
    "19": PTIQ-TOOL-7
```

### Simulator GUI

```bash
python -m simulator_gui --config packaging/secsgem_simulator/simulator.yaml
```

The Link tab makes role and mode explicit and prints the required peer wiring.
The Equipment tab controls tool identity, lot size, pacing, repeated lots, and
alarms. The Host tab controls the opening sequence. Save writes the same YAML
that the CLI reads, and Start runs the simulator in the panel process.

### Embedded simulator

Set a middleware machine to `runtime_mode: simulated` and press Start in the
middleware GUI, or run the service normally. The service starts a loopback
simulator with the opposite HSMS mode and owns both peers. Real and simulated
machines can run together. Give each endpoint its own port.

### Focused NexGen MG simulator

For MG-specific tests, use the flag-driven peer:

```bash
python -m simulator.nexgen_mg_simulator --port 5051 --wafers 3 --loop
python -m simulator.nexgen_mg_simulator --port 5051 --refuse-band gem300
python -m simulator.nexgen_mg_simulator --port 5051 --start-offline
```

The focused simulator covers subscription-band refusal, host-offline startup,
two-port lots, substrate-ID omission, HSMS role selection, and process-state
encoding. It is separate from the profile-driven simulator because those tests
need deliberately narrow failure modes.

## Adding a machine profile

A profile is data plus aliases. Keep protocol behavior in `gateway/` and the
service; keep vendor numbers and names in `eap_middleware/profiles/`.

The smallest useful profile module looks like this:

```python
from .base import MachineProfile, event_mapping

VENDOR_SVIDS = {"MDLN": 32, "SOFTREV": 39}
VENDOR_DVS = {"LotID": 1051, "WaferID": 1053}
VENDOR_EVENTS = {
    "LotStarted": event_mapping("lot_start", "Lot_Start", "LotStarted"),
    "LotEnded": event_mapping("lot_end", "Lot_End", "LotEnded", closes=True),
}
```

The registry entry then connects those tables to the runtime:

```python
MachineProfile(
    profile_id="vendor_model",
    vendor="Vendor",
    model="Model",
    event_aliases=VENDOR_EVENTS,
    ceid_aliases={1002: "LotStarted", 1005: "LotEnded"},
    svids_by_name=VENDOR_SVIDS,
    dvs_by_name=VENDOR_DVS,
    identity_svid_names=["MDLN", "SOFTREV"],
    ceid_dv_layout={1002: ("LotID",), 1005: ("LotID",)},
)
```

Use the real CEID and DVID values and the exact wire order from the vendor
manual. The `closes=True` flag belongs on the physical unload or carrier
removal event that ends the CSV file, not automatically on `lot_end`.

1. Add a vendor module, or a vendor subpackage when the tables are large.
   Define `SVIDS`, `DVS`, CEID aliases, per-CEID DVID order, load-port/chamber
   bindings, and any job-tracker transition tags. Use the vendor manual or an
   approved machine export as the source.
2. Add a `MachineProfile(...)` entry in `built_in_profiles()` in
   `eap_middleware/profiles/registry.py`. Set `profile_id`, vendor/model,
   default port and device ID, table mappings, identity SVID names, optional
   subscription path, health SVIDs, HSMS timers, and a source note.
3. Re-export public tables from `eap_middleware/profiles/__init__.py` if callers
   or tests need them.
4. Add the profile's default `EventSubscription.json` under
   `output/<profile>/` when the vendor publishes stable CEIDs. Generate it with
   a script rather than hand-editing a generated file.
5. If the vendor uses per-installation numbers, leave `ceid_aliases` empty and
   point each machine at its own `event_subscription_path`. That file supplies
   both the subscription and the positional report layout.
6. Add simulator defaults only when the vendor's identity strings, alarm values,
   or value types are documented. The universal simulator picks up a registered
   profile automatically.
7. Add tests for profile registration, subscription loading, event mapping,
   simulator replay, CSV output, and any vendor-specific transport behavior.
8. Run the checks below and confirm the new ID appears in both middleware and
   simulator validation output.

```bash
python -m eap_middleware list-profiles
python -m eap_middleware validate-config --config config/production.yaml
python -m simulator check-config --config packaging/secsgem_simulator/simulator.yaml
python -m pytest -q
```

For a profile with multiple subscription families, use the NexGen MG package
as the model. `bands.py` groups reports, `reports.py` defines DVID layouts,
`ceids.py` holds event numbers and state transitions, and `events.py` keeps
canonical aliases readable.

## Generated data and helper scripts

Files under `output/` are generated artifacts. Do not hand-edit them. The
generators are:

- `scripts/gen_spts_subscription.py` creates the SPTS event subscription.
- `scripts/gen_spts_module_variables.py` creates SPTS module variable data.
- `scripts/gen_davinci_full_subscription.py` creates the DaVinci subscription.
- `scripts/gen_mg_subscription.py` creates the NexGen MG subscription.
- `scripts/band_subscriptions.py` builds and checks MG subscription bands.

Other scripts have operational roles:

- `build_deploy_package.sh` stages the offline middleware ZIP and hashes it.
- `install_service.ps1` registers the middleware as a Windows service helper.
- `e2e_davinci_live.py` and `e2e_lifecycle_telemetry_test.py` run explicit live
  integration flows.
- `smoke_linkstuffs.py` probes upstream connectivity.
- `release_evidence.py` and `eap_middleware/release_evidence.py` collect release
  metadata and evidence.
- `vendor_text.py` extracts text from vendor source documents.
- `verify_restore.py` checks archive and restore behavior.

`output/docx/` contains generated connection documentation. The authoritative
inputs for vendor tables live under `docs/vendor/`, including the SPTS and
NexGen manuals, the DaVinci SECS-Items workbook, extracted text, and reference
images. Treat those files as source material, not runtime configuration.

## Tests and quality checks

The default test configuration in `pyproject.toml` excludes tests marked `live`
and `slow`:

```bash
python -m pytest -q
python -m pytest -q -m live                 # requires EAP_RUN_LIVE_TESTS=1
python -m pytest tests/test_twenty_two_machines.py -m slow
```

Useful focused groups include `test_profile_simulator.py`,
`test_simulator_config.py`, `test_simulator_gui.py`, `test_secs_simulator_loopback.py`,
`test_mapping_csv_linkstuffs.py`, `test_svid_admin.py`, and
`test_production_config.py`.

Static checks use the tools configured in `pyproject.toml`:

```bash
python -m ruff check eap_middleware gateway gui simulator simulator_gui tests
python -m pyright
python -m bandit -r eap_middleware gateway simulator gui simulator_gui
```

## Windows deployment and packaging

The offline deployment payload contains Python 3.11, pinned Windows wheels,
source, and the middleware GUI. The simulator has separate deliverables and
does not need administrator rights.

### Install an offline middleware package

Read `deploy/README_DEPLOY.txt` and fill in `deploy/SETUP_CHECKLIST.txt` first.
The normal sequence is:

1. Verify the ZIP SHA-256 through the site's trusted release channel.
2. Extract it on Windows 11.
3. Run `SETUP.bat`, or run `install.ps1` as Administrator.
4. Set machine addresses and Linkstuffs environment variables.
5. Run `validate-config`, `test-machine`, and `test-linkstuffs`.
6. Register the process with NSSM or Task Scheduler.
7. Start the service and check the rotating logs and CSV directory.

The installed layout is normally `C:\SECSGEM_EAP\app` for source,
`C:\SECSGEM_EAP\logs` for logs, `C:\SECSGEM_EAP\data` for SQLite queues and
journals, and `C:\SECSGEM_EAP\machines\<display_name>\config` for admin files.

Deployment files:

- `deploy/PYTHON_VERSION.txt` records the interpreter version expected by the
  bundled wheels.
- `deploy/install.ps1` performs manifest verification, Python installation,
  offline dependency installation, and source installation.
- `deploy/upgrade.ps1` updates code while preserving operator configuration.
- `deploy/Setup.ps1` and `deploy/SETUP.bat` provide the guided entry point.
- `deploy/README_DEPLOY.txt` is the step-by-step operator guide.
- `deploy/SETUP_CHECKLIST.txt` is the printable pre-install checklist.
- `deploy/python/` contains the matching Python installer.
- `deploy/wheels/` contains the offline dependency wheels and a wheel README.

Build the middleware installer on Windows with Inno Setup 6:

```powershell
packaging\installer\build_installer.ps1
```

The script stages a middleware-only payload and produces a signed or unsigned
installer under `artifacts\installer` depending on the signing options.

### Build simulator packages

On a Windows build machine with 64-bit Python 3.11 and Inno Setup 6:

```powershell
packaging\secsgem_simulator\build_windows.ps1
packaging\mg_simulator\build_windows.ps1
packaging\gui\build_windows.ps1
```

The resulting standalone simulator packages include their own Python runtime,
secsgem, operator YAML files, launch scripts, licenses, and smoke-test helpers.
See the two operator README files in `packaging/secsgem_simulator/`,
`packaging/mg_simulator/`, and `packaging/gui/` for the packaged workflow.

## Troubleshooting

- `validate-config` reports an unknown profile: run `list-profiles` and check
  the spelling of `machine_profile`.
- TCP connects but no events arrive: confirm the tool is ON-LINE, the selected
  profile matches its CEIDs, and S2F33/S2F35/S2F37 were accepted. For annotated
  reports, the gateway also supports S6F13/S6F14. For DaVinci E40 mode, check
  the S16 event log.
- The active side retries forever: the peer must be listening at the exact
  address and port, and its firewall must allow the connection.
- A passive listener cannot start: another process owns the bind address or
  port. Use a dedicated port and check the configured `hsms_bind_address`.
- The simulator reports a missing package in embedded mode: install the
  simulator package in the same environment, or use `runtime_mode: real` and
  point the machine at a standalone simulator.
- CSV files stop while the service remains connected: inspect storage-safety
  state, the ingress journal, and the machine log. Low disk space causes a
  deliberate safe stop rather than silent data loss.
- HTTPS events are dropped: create the Linkstuffs device first, map its exact
  `display_name` to a token, export the referenced environment variable, and
  run `test-linkstuffs`.
- Two service processes refuse to run: close the GUI's locally owned service
  or stop the Windows service. The single-instance lock is intentional.

## Safety and scope

The production path is read-only with respect to process-affecting commands.
The optional S1F17, S5F3, S6F23, and subscription messages are explicit
per-machine switches because they change communication or reporting state.
Confirm the tool owner's expectations before enabling them. The NexGen MG
profile is documentation-derived and must be commissioned against the actual
machine before production use.
