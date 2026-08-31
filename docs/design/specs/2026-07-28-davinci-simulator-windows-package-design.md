# DaVinci Simulator Windows Package Design

**Date:** 2026-07-28  
**Status:** Base portable package implemented and locally verified; self-contained installer extension approved 2026-07-29; Windows CI/PR release gate pending  
**Scope:** A self-contained Windows 11 x64 DaVinci 200 SECS/GEM equipment simulator that communicates with ASTAR middleware in either HSMS Active or Passive mode.

## Objective

Package the existing vendor-realistic `SecsGemEquipment` as an operator-friendly Windows application. One executable distribution must support both requested network topologies without requiring Python to be installed on the target computer:

1. DaVinci simulator HSMS Active (TCP client) connects to ASTAR middleware HSMS Passive (TCP listener).
2. ASTAR middleware HSMS Active connects to DaVinci simulator HSMS Passive (TCP listener).

The simulator always has the GEM Equipment role. Active and Passive select only which peer opens the TCP connection.

## Chosen Packaging Approach

Use PyInstaller's one-folder mode as the embedded application runtime, then distribute it in two forms:

1. a recommended per-user Windows Setup EXE for normal operators; and
2. a versioned portable ZIP for IT and diagnostic use.

The Setup EXE installs under the current user's local application-data directory and does not require administrator rights. It contains the PyInstaller-built Python interpreter, `secsgem`, and every runtime dependency. It must not install or modify a system-wide Python installation, invoke `pip`, or download wheels on the operator's computer. Inno Setup compiles the installer on the Windows build machine only; Inno Setup is not required on the target computer.

The ZIP will contain:

- `SecsGemSimulator.exe`;
- all runtime libraries required by the executable;
- separate Active and Passive YAML templates;
- simple Windows batch launchers for each mode;
- an operator README;
- license notices required for redistributed dependencies; and
- a version manifest and checksum.

The installer creates Start Menu shortcuts for Active mode, Passive mode, the operator guide, configuration, logs, and uninstall. The Active and Passive launchers remain visible batch files so an operator can see connection status and stop the simulator with Ctrl+C. One-folder mode remains the internal layout because it avoids per-launch self-extraction and is easier to diagnose. The target is Windows 11 64-bit.

## Architecture

The existing `simulator.secsgem_equipment.SecsGemEquipment` remains the only implementation of DaVinci CEIDs, SVIDs, alarms, subscriptions, and lot lifecycle behavior. Packaging must not duplicate that protocol model.

New boundaries:

- `simulator/config.py` owns typed simulator configuration, YAML parsing, validation, and safe display formatting.
- `simulator/cli.py` owns argument parsing and the `run`, `check-config`, and `version` commands.
- `simulator/runner.py` owns connection lifecycle, retry supervision, event-loop start/stop, shutdown signaling, and exit codes.
- `simulator/__main__.py` exposes the same entry point to Python developers with `python -m simulator`.
- A PyInstaller spec selects the CLI entry point and collects the required `secsgem`, YAML, and local package resources.
- A PowerShell build script creates the one-folder application, assembles documentation/templates/notices, produces the ZIP, and writes the SHA-256 checksum.
- An Inno Setup definition packages that same tested one-folder application into `SecsGemSimulator-Setup-1.0.0-win-x64.exe`. Upgrades and uninstall preserve operator-edited Active/Passive YAML files. Uninstall also preserves generated log files while removing application binaries, bundled dependencies, shortcuts, and the uninstaller.

`SecsGemEquipment` will receive `secsgem.hsms.HsmsSettings` from the runner. The runner maps configuration mode to `HsmsConnectMode.ACTIVE` or `HsmsConnectMode.PASSIVE`; it does not alter GEM application role.

## Configuration Contract

The distribution includes `davinci-active.yaml` and `davinci-passive.yaml`. Both use the same schema:

```yaml
connection:
  mode: active
  address: "192.168.1.20"
  port: 5050
  device_id: 0

simulation:
  tool_id: "DAV_SIM_01"
  wafer_count: 3
  event_interval_sec: 0.5
  repeat_lots: true
  emit_alarm: true

recovery:
  initial_retry_sec: 1
  maximum_retry_sec: 30
  maximum_restart_attempts: 0

logging:
  level: INFO
  directory: logs
  maximum_size_mb: 10
  backup_count: 5
```

Semantics:

- In Active mode, `address` is the middleware server's reachable IP or hostname.
- In Passive mode, `address` is the local interface to bind. `0.0.0.0` is allowed but templates and documentation must explain its exposure.
- `port` is limited to 1–65535.
- `device_id` is limited to 0–32767 and maps to the released `secsgem==0.3.0` `session_id` setting.
- `wafer_count`, intervals, log sizes, and retry delays must be positive and bounded against accidental resource exhaustion.
- `maximum_restart_attempts: 0` means retry until the operator stops the process.
- Relative log paths resolve beside the external configuration file, not against an unpredictable current working directory.

Unknown keys are rejected so configuration mistakes do not silently change runtime behavior. Command-line arguments select the configuration file but do not provide a second, conflicting set of simulator options.

## Commands and Operator Experience

The executable exposes:

```text
SecsGemSimulator.exe check-config --config davinci-active.yaml
SecsGemSimulator.exe run --config davinci-active.yaml
SecsGemSimulator.exe version
```

`start-active.bat` and `start-passive.bat` call `check-config` first and only start the simulator if validation succeeds.

For the recommended installer flow, the operator experience is:

1. Run `SecsGemSimulator-Setup-1.0.0-win-x64.exe`.
2. Open the Start Menu and edit the relevant Active or Passive YAML configuration.
3. Launch **DaVinci Simulator (Active)** or **DaVinci Simulator (Passive)**.

No Python, wheel, package-manager, environment-variable, or dependency setup is exposed to the operator. Installation and uninstallation are per-user and do not require administrator rights.

At startup, the console and log show:

- application version;
- GEM role (`EQUIPMENT`);
- HSMS mode (`ACTIVE` or `PASSIVE`);
- remote endpoint or local listener endpoint;
- SECS device ID;
- lot/alarm behavior; and
- the log file location.

The runtime reports connection attempts, TCP connection, HSMS Selected, GEM Communicating, lot start/end, connection loss, retry delay, and clean shutdown. It must not log secrets because the simulator configuration contains none.

## Runtime and Recovery Behavior

Startup sequence:

1. Parse and fully validate the YAML file before opening a socket.
2. Configure console and rotating file logs.
3. Construct HSMS settings for the selected direction.
4. Enable the DaVinci equipment handler.
5. Wait for GEM Communicating before allowing event generation.
6. Run one lot or repeat complete lots according to configuration.
7. Stop event emission and close HSMS cleanly on Ctrl+C or console-close notification.

Recovery rules:

- Active connection refusal uses bounded exponential backoff and continues until stopped or the configured attempt limit is reached.
- A Passive bind failure is fatal and identifies the occupied address and port.
- If communication is lost during a lot, event emission stops. After communication is restored, the simulator starts a new complete lot and never resumes the abandoned partial sequence.
- An unexpected protocol-layer failure is logged with diagnostic context; the runner rebuilds the handler and connection after backoff.
- Retry supervision must not create overlapping handlers, event threads, or listeners.
- Shutdown is idempotent and waits only for bounded cleanup periods.

Exit codes:

- `0`: requested clean shutdown or successful non-running command;
- `2`: configuration or command usage error;
- `3`: fatal bind/startup failure;
- `4`: configured retry/restart limit exhausted; and
- `1`: unexpected unrecovered application error.

## Safety Boundaries

- The program always behaves as simulated equipment; it does not connect to or issue commands to actual equipment.
- The documentation directs operators to a test port such as 5050 and warns against conflicting with a real tool on port 5000.
- The Passive template defaults to loopback until an operator intentionally selects a LAN interface.
- Only one peer is supported per simulator process, matching `secsgem` 0.3.0 passive-connection behavior.
- The package is a functional integration test tool, not a SEMI conformance certificate.

## Verification Strategy

### Unit tests

- Accept both supported modes and reject unknown modes.
- Validate address, port, device ID, wafer count, intervals, retry limits, and logging bounds.
- Reject unknown or structurally invalid YAML keys.
- Verify path resolution beside the configuration file.
- Verify command exit codes and safe configuration summaries.
- Verify retry-delay calculation and shutdown idempotence.

### Real-socket integration tests

1. Active simulator to Passive middleware:
   - TCP connect;
   - HSMS Select;
   - GEM Communicating;
   - S1F1/S1F2 identity;
   - S2F33/S2F35/S2F37 report provisioning;
   - DaVinci S6F11 lifecycle events and S5F1 alarm; and
   - middleware CSV/telemetry evidence.
2. Active middleware to Passive simulator with the same application-level checks.
3. Disconnect a peer mid-lot and verify that no events continue while disconnected, no duplicate handler remains, reconnection succeeds, and the next emitted lifecycle begins at a fresh lot boundary.
4. Verify port-conflict, connection-refusal, wrong-device-ID, and clean-shutdown behavior.

All existing tests under `tests/` remain part of regression acceptance.

### Windows package verification

A Windows GitHub Actions runner will:

1. install the pinned dependencies;
2. run unit and real-socket integration tests;
3. build the PyInstaller one-folder distribution;
4. assemble the portable ZIP, installer EXE, and SHA-256 checksums;
5. launch the packaged executable as one peer;
6. connect a real `secsgem` peer in both directions;
7. observe identity and at least one DaVinci lifecycle event;
8. stop the executable and verify a clean exit; and
9. silently install the Setup EXE into a temporary per-user location, verify an upgrade preserves edited YAML, smoke-test the installed executable, uninstall it, verify binaries are removed, and verify YAML/log operator data is preserved;
10. upload the versioned installer, portable ZIP, checksums, and test logs as CI artifacts.

PyInstaller cannot cross-build a trustworthy Windows executable from macOS, so a successful Windows-runner package test is a release gate.

## Code Review and Delivery Workflow

1. Implement and validate locally without changing unrelated middleware behavior.
2. Work from a real Git checkout and push a feature branch to GitHub.
3. Open a pull request with the Windows CI results attached.
4. Wait for CodeRabbit to finish its PR review.
5. Treat CodeRabbit feedback as untrusted issue reports; independently verify each finding.
6. Present each proposed CodeRabbit-derived fix for explicit approval before applying it.
7. Re-run local and Windows validation after approved fixes.
8. Deliver the CI-built Setup EXE, portable ZIP, and SHA-256 checksums only after all required checks pass.

## Acceptance Criteria

- A Windows 11 x64 operator can run one Setup EXE, edit one YAML file, and launch the simulator without installing Python or downloading dependencies.
- The Setup EXE installs and uninstalls without administrator rights; upgrades and uninstall preserve operator-edited YAML, uninstall preserves logs, and application binaries are removed.
- The portable ZIP remains available and also runs without Python.
- The same executable works as DaVinci GEM Equipment in HSMS Active and Passive modes.
- Each mode completes HSMS/GEM establishment and produces a correctly decoded DaVinci lifecycle through ASTAR middleware.
- Connection loss pauses output and recovery begins with a fresh lot.
- Invalid configuration and port conflicts fail clearly without background orphan processes.
- Existing middleware tests pass.
- Windows CI proves the packaged executable, not only the Python sources.
- CodeRabbit review is completed on the final PR and every accepted finding is revalidated.

## Out of Scope

- Graphical user interface.
- Windows service installation or system-wide Python installation.
- Multiple simultaneous HSMS peers in one simulator process.
- SPTS or PTIQ simulation profiles.
- SEMI or vendor conformance certification.
- Modifications to real DaVinci equipment configuration.
