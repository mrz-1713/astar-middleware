# Unified Multi-Machine Middleware and Simulator Control - Design

**Date:** 2026-08-12

**Status:** Approved in conversation; pending written-spec review

**Primary scope:** Four real-machine profiles, 22 simultaneous sessions, one GUI,
and a configurable profile-driven simulator

**Priority profile:** NexGen Wafersystems MG Series

## Objective

Run one Windows middleware service that can maintain 22 simultaneous, isolated
HSMS/SECS/GEM sessions in any mixture of these four supported machine families:

- SPTS fxP Omega;
- MueTec DaVinci 200 MC4/HC1;
- PTIQ SECS/GEM; and
- NexGen Wafersystems MG21, MG22, and MG22-300.

Each real machine uses a tailored profile for its model, message structure,
collection events, status variables, subscriptions, alarms, and lifecycle
mapping. The generic, user-configurable event model belongs only to the built-in
equipment simulator. It must never silently replace a tailored production
profile.

One GUI controls middleware and simulator operation, edits the complete YAML
configuration through structured settings, monitors each individual machine,
and starts, stops, or restarts one session without interrupting the others.

## Source Review and Current Baseline

The design was derived after inventorying every file under `docs/`, including
the Markdown deployment, operations, simulator, reliability, and profile specs;
all 14 PDFs; and the DaVinci SECS item workbook. The protocol-relevant findings
are:

- the four tools do not share CEID, SVID, report, payload, or lifecycle
  definitions;
- HSMS Active/Passive direction and device ID must remain configurable per
  machine;
- DaVinci needs its E30/E40, E90, report-subscription, and optional spool
  behavior;
- PTIQ collection-event numbers may be installation-specific and therefore
  come from its event-subscription definition instead of invented constants;
- NexGen requires subscription-band isolation, enabled-event readback, an
  S1F17 ON-LINE request, two/four-port tolerance, and explicit handling of its
  unsupported spool/alarm-state query behavior; and
- HTTPS uses the ThingsBoard Community Edition device API under the Linkstuffs
  branding and requires one device token per machine.

At design time, the uncommitted implementation baseline passes:

```text
316 passed, 2 skipped, 5 deselected
22-machine real-socket test: 1 passed
```

That baseline proves useful existing behavior but is not sufficient acceptance:
the 22-machine test currently needs stronger mixed-role, hot-reconfiguration,
storage, HTTPS, and failure-isolation assertions.

## Requirements

### Runtime

1. One always-running Windows service owns all real and simulated sessions.
2. It supports at least 22 configured sessions in any mixture of the four
   scoped profiles at the same time. Acceptance proves exactly 22; larger
   deployments are not rejected artificially but require their own capacity
   test.
3. Each machine has an isolated connection, retry loop, profile, mapper,
   subscription state, SVID polling, CSV state, logs, simulator, and HTTPS
   publisher.
4. A failure in one machine must not terminate, reconnect, delay, or corrupt
   another machine.
5. Machine settings can be added or changed while the service runs. Unchanged
   sessions remain connected.

### GUI

1. There is one GUI for middleware and simulator operation.
2. Operators use structured fields and file/directory pickers; they do not need
   to locate or hand-edit `production.yaml`.
3. Each machine can be started, stopped, restarted, tested, enabled, disabled,
   duplicated, added, or removed independently.
4. The GUI exposes each machine's connection, tailored profile, storage,
   Linkstuffs, simulator, and status settings.
5. Closing the GUI does not stop the Windows service or its active sessions.

### Simulator

1. The simulator always has the GEM Equipment role and supports either HSMS
   direction.
2. The four tailored profiles provide simulator presets using their own CEIDs,
   SVIDs, report layouts, and payload shapes.
3. Simulator-only overrides allow configurable model identity, CEIDs, SVIDs,
   report/DVID layouts, values, alarms, lot size, and timing.
4. Simulator overrides never mutate a real-machine profile.

## Architecture

### Always-running service and passive GUI

The Windows service is the runtime owner. The GUI is a configuration and
monitoring client that reads and writes files; it does not own HSMS threads.
This preserves all sessions when an operator closes the window.

The GUI locates the installed configuration automatically. It loads that file
into forms, validates a candidate in memory, writes a temporary file beside the
target, flushes it, and replaces the YAML atomically with `os.replace`. Before
saving, it compares the file's current content hash with the hash it loaded. If
an external editor changed the file, it asks the operator to reload instead of
overwriting someone else's work.

The service watches the configuration file for atomic replacements. It parses
and validates each candidate independently. An invalid candidate is reported
but never replaces the last valid runtime configuration.

The service writes an atomic status snapshot under the configured data
directory. The GUI polls this snapshot and never needs a new network-facing
admin API. The snapshot contains a configuration revision plus per-machine
runtime state, last transition time, HSMS/GEM state, subscription outcome, last
event, last error, retry count, HTTPS queue counts, and current lot summary. It
contains no credentials or raw secret values.

One-shot actions that are not configuration changes (`Restart` and
`Test Connection`) use an atomic local command inbox under the data directory.
The GUI writes one uniquely named JSON request through temporary-file rename;
the service consumes each request once and publishes its request ID and result
in the status snapshot. Start and Stop remain durable configuration changes to
`enabled`. This avoids both a listening admin port and an ambiguous
configuration nonce, while ensuring rapid repeated commands cannot overwrite
one another.

### Reconciliation supervisor

On each valid configuration revision, a supervisor compares machines by stable
`endpoint_id` and classifies the difference:

| Change | Runtime action |
|---|---|
| New enabled machine | Start only the new runtime |
| Enabled to disabled or removed | Stop only that runtime |
| Host, port, device ID, HSMS role, profile, or subscription | Restart only that runtime |
| Log destination | Rotate that machine's handler without reconnecting |
| CSV destination | Use the new destination for the next lot |
| Linkstuffs HTTPS settings | Reload only that machine's publisher |
| Simulator values or timing | Restart only that simulator when simulated mode is active |
| Unchanged machine | No action; its existing session object remains live |

The supervisor serializes changes for one endpoint but can reconcile different
endpoints independently. Repeated application of the same revision is a no-op.
Start, stop, and cleanup operations are idempotent and bounded by timeouts.

### Per-machine runtime boundary

Each `MachineRuntime` owns the resources that can fail independently:

- the selected tailored `MachineProfile`;
- one `SecsMachineSession` and reconnect state;
- mapper and machine-scoped attribution state;
- report subscription and readback state;
- SVID/admin polling;
- alarm limiting and alarm-state tracking;
- per-lot CSV writer and destinations;
- machine-filtered logging handlers;
- durable Linkstuffs HTTPS publisher/outbox; and
- optional profile-driven equipment simulator.

The top-level service owns only the registry, configuration supervisor, runtime
map, global operational log, and status snapshot. No runtime reads another
runtime's mutable profile, connection, lot, or publisher state.

## Unified Machine Configuration

The existing flat machine fields remain accepted. New machine-scoped sections
hold independent storage, HTTPS, and simulator configuration. The following
field names and semantics form the configuration contract:

```yaml
machines:
  - endpoint_id: TOOL_04
    display_name: NEXGEN_MG_01
    enabled: true
    runtime_mode: real          # real | simulated
    offline_test_mode: false
    machine_profile: nexgen_mg_series
    host: 192.0.2.34
    port: 5000
    secs_device_id: 0
    hsms_mode: active

    storage:
      log_dir: C:/SECSGEM_EAP/logs/NEXGEN_MG_01
      simulator_log_dir: C:/SECSGEM_EAP/logs/NEXGEN_MG_01/simulator
      local_csv_path: D:/MachineData/EAP_NEXGEN_MG_01/csv_in
      network_csv_path: "\\\\FILESERVER\\EAP_NEXGEN_MG_01.csv_in"
      admin_config_path: C:/SECSGEM_EAP/machines/NEXGEN_MG_01/config

    linkstuffs_http:
      enabled: true
      base_url: https://astar-monitoring.linkstuffs.com
      device_token: "${LINKSTUFFS_HTTP_NEXGEN_TOKEN}"
      verify_tls: true
      timeout_sec: 10
      retry_count: 3
      retry_delay_sec: 1

    simulator:
      mdln: MG Series
      softrev: NWS MG 1.1.18
      wafer_count: 3
      event_interval_sec: 0.5
      repeat_lots: true
      emit_alarm: true
      ceid_overrides: {}
      svid_values: {}
```

`enabled` is the persistent state controlled by the GUI Start and Stop buttons.
`runtime_mode` selects the real endpoint or a built-in loopback equipment peer.
Switching modes requires explicit confirmation and restarts only that machine.
`offline_test_mode` is disabled by default and is the explicit exception that
allows a controlled simulator/CSV test without a Linkstuffs route.

For backward compatibility, the loader continues to accept the current global
`linkstuffs_http` block and `device_tokens` map as defaults. A machine-scoped
value wins. The GUI displays effective values and writes explicit per-machine
settings when an operator edits them; it does not silently discard the old
configuration. Existing flat per-machine CSV/admin path fields are treated as
the corresponding `storage` values during migration.

## GUI Design

The main table contains one row per machine with endpoint, display name,
profile, real/simulated mode, address, enabled state, HSMS/GEM state,
Linkstuffs state, and simulator state.

Selecting a row opens one detail area with these sections:

### Connection

- tailored profile;
- real or simulated mode;
- host/address, port, device ID, and HSMS role;
- passive bind address;
- subscription, SVID, alarm, ON-LINE, and spool switches; and
- Start, Stop, Restart, and Test Connection actions.

The table also provides Start All and Stop All. Both show the exact affected
endpoints and require confirmation before atomically changing their enabled
states.

### Storage

- middleware log directory;
- simulator log directory;
- local CSV directory;
- optional network CSV mirror; and
- admin/SVID directory.

All directory fields include a browse button. The GUI performs safe writability
probes where possible without deleting or overwriting operator data. An
unavailable network share is a warning because local CSV remains authoritative;
an unusable local destination always blocks Start. Offline test mode waives only
the Linkstuffs route requirement, not local persistence.

### Linkstuffs

- enable HTTPS;
- base URL;
- masked ThingsBoard device token or environment reference;
- TLS verification;
- timeout and retry settings;
- Test Linkstuffs action; and
- last HTTP status, queued count, and dead-letter count.

The transport is the ThingsBoard Community Edition device API rebranded by
Linkstuffs:

```text
POST /api/v1/{device-token}/telemetry
POST /api/v1/{device-token}/attributes
```

The connectivity test uses a non-mutating authenticated request. Linkstuffs
devices are not auto-created over HTTPS, so validation requires a non-empty
token for every enabled non-offline machine.

### Simulator

- model and software revision;
- wafer count, event interval, repeated lots, and alarm generation;
- editable CEID-to-canonical-event overrides;
- editable SVID ID/name/type/value rows;
- import and edit of event/report/DVID definitions; and
- simulator start/stop state.

The GUI starts the simulator only through the service reconciliation path. In
simulated mode the middleware and equipment peer take opposite HSMS roles and
use a collision-free local endpoint. A simulator cannot be enabled against the
same runtime's real equipment address accidentally. The machine's Start and
Stop actions control the complete simulated pair; there is no second hidden
simulator process with an independent lifecycle.

### Status and logs

The status view shows `Starting`, `Connecting`, `Selected`, `Communicating`,
`Running`, `Stopping`, `Stopped`, `Retrying`, or `Error`, along with tailored
profile diagnostics. NexGen also shows each subscription band's result and the
enabled-event readback difference.

The log view can filter the global operational log or one machine's log. Secret
tokens, credential-bearing URLs, and encrypted keys are always redacted.

## Start, Stop, Restart, and Test Semantics

### Start

Start validates the latest form, atomically saves `enabled: true`, and waits for
the status revision that applied it. It starts only the selected real session or
simulated loopback pair. A failure moves that runtime to `Retrying` or `Error`
without rolling back or restarting other machines.

### Stop

Stop atomically saves `enabled: false`, stops new event acceptance for that
runtime, stops its simulator and SVID polling, flushes an active lot as a
CSV whose filename ends in `.partial.csv`, closes its HSMS session, and
preserves queued HTTPS telemetry. It does not discard durable outbox rows.

Removing a machine has the same runtime stop behavior and preserves its logs,
CSV files, admin files, and outbox. The GUI never deletes machine data as a side
effect of removing configuration.

### Restart

Restart performs an idempotent stop/start for only that endpoint using the most
recently applied valid settings.

### Test Connection

For a stopped real machine, Test Connection performs TCP/HSMS Select and the
S1F1/S1F2 identity exchange, then disconnects. It does not configure reports,
send production-affecting commands, publish telemetry, or take REMOTE control.
For a running machine, the GUI reports its current verified identity instead of
opening a second HSMS connection.

## Per-machine Storage Behavior

Every machine writes `middleware.log` in its configured log directory and
`simulator.log` in its simulator log directory. A global service log remains for
startup, reconciliation, and failures that cannot yet be attributed to an
endpoint.

Changing a log directory rotates the machine handler immediately. Changing a
CSV destination does not split a lot: every open lot buffer retains the
destination captured when that lot began, and the next lot uses the new path.

Completed local CSV files are written through a temporary file and atomic
rename. Network mirroring happens only after the local file is durable. A
network failure is recorded and retryable but never removes or invalidates the
local file.

## Tailored Real-machine Profiles

### SPTS fxP Omega

Use the documented Cimetrix-based SPTS event, status, alarm, process, and
subscription definitions. Connection direction, port, device ID, timers, and
site-specific paths remain per-machine configuration.

### DaVinci 200 MC4/HC1

Preserve its E30 collection-event path, E40 notification compatibility, E90
substrate structures, multiple-report preservation, collision-safe report
definition, optional S1F17 request, optional spool drain, alarm behavior, and
workbook-derived variable layouts.

### PTIQ

Preserve its named canonical lifecycle mappings while loading installation
CEIDs, report IDs, DVID order, and names from the selected event-subscription
definition. A missing or malformed definition must fail validation for an
enabled real PTIQ session rather than silently inventing production constants.

### NexGen MG Series

NexGen is the priority profile and one superset covers MG21, MG22, and MG22-300.
It must preserve these manual-derived safeguards:

- configurable port, device ID, and HSMS direction because the manual omits
  them;
- S1F17 ON-LINE request at connection, without taking REMOTE control;
- independent bands for core GEM, individual load ports, slot mapping, process
  modules, recipe, GEM300, and metrology/auxiliary events;
- per-band acknowledgments and enabled-event list readback;
- no empty report link for CEIDs with no valid variables;
- two- and four-load-port tolerance;
- direct load-port attribution from process-event payloads;
- integer or ASCII process-state decoding;
- GEM300 substrate ID with cassette-slot fallback;
- unknown CEID preservation;
- enable-all alarm reporting plus rate limiting;
- alarm-state-unknown event after reconnect; and
- explicit no-spool/data-loss status because the tool does not support
  equipment-side spooling.

The profile remains documentation-derived until real MG traffic verifies it.
The GUI and logs must display that provenance instead of claiming hardware
conformance.

## Configurable Simulator Model

The profile registry is the simulator's default source of CEIDs, ordered report
variables, SVIDs, identity, and canonical lifecycle events. This avoids a second
copy of each vendor table.

Simulator CEID/SVID overrides are scoped to one machine. An imported event
definition can supply installation-specific CEIDs, report IDs, ordered DVIDs,
names, supported SECS data types, and simulated values. The GUI validates IDs,
duplicates, report references, type/value compatibility, and lifecycle
completeness before applying it.

The two specialized simulators remain available behind the unified controls for
behavior the generic replay cannot express economically: DaVinci E90/TestResult
structures and NexGen band refusal, HOST OFF-LINE behavior, and concurrent
two-module/two-port lots. They are implementation details selected by a test or
advanced simulator option, not separate operator applications.

## Linkstuffs Delivery

Each machine has independent HTTPS configuration and a machine-scoped durable
SQLite outbox. A token or server failure therefore cannot hold another
machine's queue behind it. Outbox records store payload and endpoint identity,
not the secret token. Rotating a token or changing a base URL causes retained
events to retry through the newly applied route.

HTTP 408, 425, 429, and 5xx responses are retryable with bounded backoff and
`Retry-After` support. Authentication, authorization, and structurally invalid
payload failures are recorded in that machine's dead-letter state with secrets
redacted.

## Error Handling and Isolation

- Duplicate endpoint IDs, display names, conflicting passive binds, invalid
  ports/device IDs, incomplete profiles, and conflicting simulator endpoints
  fail configuration validation.
- Externally written invalid YAML never replaces the last valid runtime.
- Unknown CEIDs become readable `unknown` events and retain raw identifiers.
- Malformed vendor payloads are logged with bounded data and receive a failure
  acknowledgment when the protocol supports retry.
- Event callbacks do not acknowledge successful ingestion after persistence
  failed.
- Reconnect uses bounded exponential backoff with jitter so 22 disconnected
  tools do not retry in lockstep.
- Each machine catches and reports its own worker failures. The supervisor
  remains alive and continues reconciling other endpoints.
- Shutdown and cleanup are idempotent and bounded.
- Status and logs make partial success visible, especially NexGen subscription
  bands and Linkstuffs dead letters.

## Verification and Acceptance

### Default checks

- `git diff --check` reports no whitespace errors.
- `compileall` succeeds for middleware, gateway, GUI, simulator, and tests.
- Every available repository static checker passes.
- The complete non-live, non-slow test suite passes.
- GUI model tests prove every accepted configuration field has a control and
  round-trips without YAML type loss.

### Hot-reconfiguration tests

- Add, enable, stop, restart, modify, disable, and remove a machine while other
  machines are communicating.
- Assert unchanged runtimes retain the same session/connection generation and
  receive events throughout reconciliation.
- Assert storage and HTTPS changes do not reconnect HSMS.
- Assert a connection/profile change restarts only its endpoint.
- Assert invalid and stale GUI revisions cannot replace active valid state.
- Assert a CSV path change during a lot finishes that lot in its original
  directory and writes the next lot in the new directory.

### Twenty-two-machine acceptance

Run 22 profile-driven equipment simulators and 22 middleware peers over real
HSMS sockets, interleaving all four profiles and both HSMS directions. For every
endpoint, assert:

- HSMS Selected and GEM Communicating;
- the correct tailored profile and identity;
- subscription success or an expected visible partial result;
- mapped lot/wafer lifecycle without unknown CEIDs for the configured flow;
- an isolated local CSV and machine log;
- delivery to its configured mock Linkstuffs URL/token; and
- clean independent shutdown.

Inject connection loss, bind failure, invalid payload, local path failure,
network mirror failure, and HTTPS authentication/outage faults into selected
machines and prove the remaining sessions continue delivering.

### NexGen priority acceptance

- MG21/MG22/MG22-300 superset selection.
- Two- and four-port configurations.
- Concurrent lots from separate ports through both process modules.
- One refused subscription band while the others remain live.
- Enabled-event readback identifies the refused band.
- HOST OFF-LINE followed by the S1F17 path.
- Active and Passive HSMS roles.
- Integer and ASCII ProcessState.
- Substrate-ID and cassette-slot WaferID paths.
- Unknown CEID fallback and alarm-state-unknown reconnect event.
- Repeated lots through CSV, HTTPS, disconnect, and reconnect paths.

### Soak and packaging

A slow soak repeatedly operates all 22 sessions while checking bounded thread
growth, no stale or duplicate runtimes, no cross-machine event/CSV/log leakage,
bounded queues, and clean shutdown. Windows packaging tests build and launch the
actual GUI/service artifact and exercise configuration discovery, save,
reconciliation, and one simulated session.

## Success Criteria

1. One service sustains 22 mixed-profile, mixed-HSMS-role sessions.
2. Operators can configure and control every session from one GUI without
   locating YAML.
3. Live changes affect only the intended machine.
4. Each machine independently controls its logs, CSV locations, simulator, and
   Linkstuffs HTTPS route.
5. The NexGen safeguards and simulator scenarios pass their focused acceptance
   suite.
6. No known syntax, test, static-check, or configuration-validation defect
   remains at handoff.

These checks materially reduce risk but do not claim mathematical proof that no
future defect can exist. Real-hardware acceptance is still required for vendor
constants and behaviors, especially the documentation-derived NexGen profile.

## Out of Scope

- Production profiles for machine families beyond the four listed above.
- Automatic inference of a real vendor's semantics from arbitrary traffic.
- Taking Online Remote control or issuing production/process commands.
- Replacing ThingsBoard/Linkstuffs user administration or device provisioning.
- SEMI or vendor conformance certification.
- Guaranteeing undocumented NexGen constants before contact with real hardware.
