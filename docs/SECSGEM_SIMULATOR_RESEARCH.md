# `secsgem` Simulator Feasibility for ASTAR Middleware

**Research date:** 2026-07-28  
**Scope:** Determine whether the Python `secsgem` project can cover the two tests requested in the chat: an external simulator that connects into ASTAR middleware, and an equipment/server simulator on the ASTAR machine that the middleware connects to.

## Decision

`secsgem` can support both tests at the protocol level. It provides separate GEM host and equipment handlers, and its HSMS settings independently select active or passive TCP direction. Its own documentation explicitly lists testing, development simulation, and complete host/equipment implementations as use cases ([official overview](https://secsgem.readthedocs.io/en/stable/)).

It is not, however, a downloadable end-user GUI or a ready-made vendor simulator. The official distribution is a Python library installed with `pip`, and the upstream repository supplies editable Python samples ([installation](https://secsgem.readthedocs.io/en/stable/installation.html), [upstream samples](https://github.com/bparzella/secsgem/tree/v0.3.0/samples)). Therefore the answer is:

- **Equipment/server simulator on this machine: implemented.** The packaged DaVinci simulator can listen in HSMS Passive mode while ASTAR connects in Active mode.
- **External/client simulator that connects into the middleware: implemented.** The same executable can run as HSMS Active equipment and connect to an ASTAR Passive listener.

Both modes now use the strict CLI in [`simulator/cli.py`](../simulator/cli.py), the supervised lifecycle in [`simulator/runner.py`](../simulator/runner.py), and the same vendor-realistic [`SecsGemEquipment`](../simulator/secsgem_equipment.py). Windows packaging assets are under [`packaging/secsgem_simulator`](../packaging/secsgem_simulator/README_OPERATOR.md).

## Do not mix up the two independent role pairs

| Layer | Role | Meaning |
|---|---|---|
| GEM application | **Host** | The factory/EAP side that asks for status, configures reports, and receives equipment events. ASTAR is the GEM host. `secsgem` exposes `GemHostHandler` as the host base class ([host-handler reference](https://secsgem.readthedocs.io/en/stable/reference/gem/hosthandler.html)). |
| GEM application | **Equipment** | The tool side that answers requests and emits data/events. A simulator used against ASTAR should normally be a `GemEquipmentHandler` subclass ([equipment-handler reference](https://secsgem.readthedocs.io/en/stable/reference/gem/equipmenthandler.html)). |
| HSMS transport | **Active** | Opens the outbound TCP connection; this is the TCP client. In `secsgem`, `HsmsConnectMode.ACTIVE` creates a TCP client connection ([v0.3.0 settings source](https://github.com/bparzella/secsgem/blob/v0.3.0/secsgem/hsms/settings.py)). |
| HSMS transport | **Passive** | Binds/listens and accepts the inbound TCP connection; this is the TCP server. In `secsgem`, `HsmsConnectMode.PASSIVE` creates a TCP server connection ([v0.3.0 settings source](https://github.com/bparzella/secsgem/blob/v0.3.0/secsgem/hsms/settings.py)). |

GEM role and HSMS direction are independent. An **equipment** simulator can be HSMS active or passive, and a **host** can be HSMS active or passive. The phrase "client simulator" in the chat should therefore mean **GEM equipment + HSMS active**, not "GEM host."

## Requirement-to-topology mapping

| Chat requirement | Simulator | ASTAR middleware | Status in this repository |
|---|---|---|---|
| Download/run a client simulator on another PC and have it connect to ASTAR | GEM **equipment**, HSMS **active** (TCP client) | GEM **host**, HSMS **passive** (TCP listener) | Implemented by the standalone `run` command and Active YAML/launcher; covered by real-socket and packaged-executable smoke tests. |
| Run a server simulator on this machine and have ASTAR connect to it | GEM **equipment**, HSMS **passive** (TCP listener) | GEM **host**, HSMS **active** (TCP client) | Implemented by the same standalone command and Passive YAML/launcher; covered by real-socket and packaged-executable smoke tests. |

`secsgem` itself permits both directions through `HsmsSettings.connect_mode`; for an active connection `address` is remote, while for passive it is the local bind address ([official settings reference](https://secsgem.readthedocs.io/en/stable/reference/hsms/settings.html)). Upstream also intentionally demonstrates the less-common pairing of an active equipment sample and a passive host sample ([equipment sample](https://github.com/bparzella/secsgem/blob/v0.3.0/samples/gem_equipment.py), [host sample](https://github.com/bparzella/secsgem/blob/v0.3.0/samples/gem_host.py)).

## Version, platform, installation, and license

- The latest official PyPI release is **`secsgem` 0.3.0**, published 2024-09-14. Its release metadata requires Python `>=3.8,<4.0` and publishes a universal `py3-none-any` wheel ([PyPI project and files](https://pypi.org/project/secsgem/)).
- Current upstream `main` has moved its project requirement to Python `^3.10`, and its CI matrix runs Python 3.10 through 3.14 on Ubuntu, macOS, and Windows ([current `pyproject.toml`](https://github.com/bparzella/secsgem/blob/main/pyproject.toml), [current build workflow](https://github.com/bparzella/secsgem/blob/main/.github/workflows/build.yaml)). For ASTAR, use the project's bundled **64-bit Python 3.11** deployment rather than changing runtimes merely for the simulator; see [`deploy/PYTHON_VERSION.txt`](../deploy/PYTHON_VERSION.txt).
- This repository deliberately pins `secsgem==0.3.0` because its gateway depends on the 0.3.x API and bundles `deploy/wheels/secsgem-0.3.0-py3-none-any.whl`; see [`requirements.txt`](../requirements.txt). Keep this exact pin for both peers so the test is reproducible.
- The project is licensed **LGPL-2.1-or-later** ([upstream package metadata](https://github.com/bparzella/secsgem/blob/v0.3.0/pyproject.toml), [license text](https://github.com/bparzella/secsgem/blob/v0.3.0/LICENSE)). Internal use is straightforward, but any redistributed test bundle should retain the applicable notices and be reviewed against the LGPL obligations, especially if the library itself is modified.

For an internet-connected Python environment:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install secsgem==0.3.0
```

The official installation command is `pip install secsgem`; the explicit version above preserves ASTAR compatibility ([official installation page](https://secsgem.readthedocs.io/en/stable/installation.html)). On the offline ASTAR deployment, use the bundled wheel set rather than downloading again.

## Test 1: external active equipment connects to passive ASTAR

This is the requested "client simulator" test.

1. Configure the ASTAR machine entry with `hsms_mode: "passive"`, a chosen listen port, and an appropriate `hsms_bind_address` (`0.0.0.0` for a controlled LAN test or the ASTAR interface address). `gateway.create_host_settings` already maps that configuration to a passive `HsmsSettings` listener; see [`gateway/host.py`](../gateway/host.py).
2. On the other computer, run a `GemEquipmentHandler` with `device_type=EQUIPMENT`, `connect_mode=ACTIVE`, the ASTAR computer's IP address, the same port, and the same SECS device/session ID. That exact role/direction is the upstream equipment sample ([sample source](https://github.com/bparzella/secsgem/blob/v0.3.0/samples/gem_equipment.py)).
3. Verify HSMS selection, GEM `COMMUNICATING`, and the S1F1/S1F2 identity exchange. Then verify report definition/enabling and at least one S6F11 collection event so the test covers more than an open TCP socket.

The upstream sample is useful as a connectivity skeleton, but it exposes only illustrative status variables and equipment constants. For a meaningful ASTAR test, the distributable client should use this repository's `EquipmentSimulator` behavior and vendor profile rather than claiming that the generic upstream sample simulates DaVinci or SPTS semantics.

The colleague-ready implementation uses `SecsGemSimulator.exe run --config davinci-active.yaml`. The external YAML supplies the middleware IP, port, and matching device ID; `start-active.bat` validates it before opening a connection.

## Test 2: local passive equipment server for active ASTAR

This path is available through the same strict CLI. From the repository root, start the vendor-realistic passive DaVinci equipment simulator:

```powershell
.venv\Scripts\python -m simulator check-config `
  --config packaging\secsgem_simulator\davinci-passive.yaml
.venv\Scripts\python -m simulator run `
  --config packaging\secsgem_simulator\davinci-passive.yaml
```

Configure the corresponding ASTAR machine entry with:

```yaml
host: 127.0.0.1
port: 5050
secs_device_id: 0
hsms_mode: "active"
enabled: true
```

Then run the non-process-affecting identity probe:

```powershell
.venv\Scripts\python -m eap_middleware test-machine `
  --config config/production.yaml --endpoint-id TOOL_02
```

For a lifecycle test, run the service against the simulator and confirm S1F1/S1F2, report subscriptions, S6F11 events, alarms, and generated per-lot CSV output. If the simulator and middleware are on different machines, bind the simulator to an approved LAN interface (or `0.0.0.0` in an isolated test network), target that machine's IP from ASTAR, and permit only the chosen TCP port through the firewall.

## What was verified locally

The complete local suite and final packaged-runtime smoke test were run with this checkout's `secsgem` 0.3.0 installation:

```text
python -m pytest -q tests

230 passed, 4 deselected in 51.42s

python packaging/secsgem_simulator/smoke_packaged_exe.py --exe <built-executable>

Packaged executable passed Active and Passive HSMS smoke tests
```

These are complementary tests: one uses passive equipment with active ASTAR, and the other uses active equipment with passive ASTAR. This confirms the two chat topologies in the current codebase; it does not constitute third-party SEMI conformance certification.

## Important limitations

1. **Toolkit, not turnkey simulator.** Upstream calls the project a work in progress, classifies it as alpha, installs it as a Python package, and provides source samples rather than a simulator executable ([upstream README](https://github.com/bparzella/secsgem/tree/v0.3.0), [v0.3.0 package metadata](https://github.com/bparzella/secsgem/blob/v0.3.0/pyproject.toml)). A friendly launcher, configuration, scripted events, and operator UI remain application work.
2. **Partial GEM coverage.** Upstream's own compliance statement lists missing or non-compliant areas including equipment processing states, trace collection, recipe management, material movement, clock, limits monitoring, and spooling; it also notes incomplete persistence/control behavior in several implemented areas ([official GEM compliance statement](https://secsgem.readthedocs.io/en/stable/gem/compliance.html)). Do not infer full GEM or vendor conformance from a successful handshake.
3. **Generic samples do not model ASTAR's machines.** The official equipment sample has two demonstration SVs and two ECs ([sample source](https://github.com/bparzella/secsgem/blob/v0.3.0/samples/gem_equipment.py)). ASTAR's existing simulator adds its own SVIDs, CEIDs, alarms, subscriptions, and DaVinci lifecycle, so it is the stronger server-side test target.
4. **Version sensitivity.** The ASTAR gateway decodes messages using the 0.3.x API and explicitly pins 0.3.0 in [`requirements.txt`](../requirements.txt). Do not install an unpinned development checkout for acceptance testing; upstream itself warns that development code can be unstable ([official installation page](https://secsgem.readthedocs.io/en/stable/installation.html)).
5. **Use the tagged 0.3.0 API.** The released settings keyword is `session_id`; unreleased `main` renamed it to `device_id` ([v0.3.0 settings](https://github.com/bparzella/secsgem/blob/v0.3.0/secsgem/common/settings.py), [current settings](https://github.com/bparzella/secsgem/blob/main/secsgem/common/settings.py)). ASTAR's `secs_device_id` configuration is translated internally, so do not copy unreleased examples into the pinned runtime. The v0.3.0 equipment sample also registers EC ID `20` but checks ID `2` in its update callback; correct that typo before using the sample to test EC writes ([sample source](https://github.com/bparzella/secsgem/blob/v0.3.0/samples/gem_equipment.py)).
6. **Passive mode is single-peer.** The v0.3.0 TCP server accepts one connection and resumes listening after that peer disconnects ([server source](https://github.com/bparzella/secsgem/blob/v0.3.0/secsgem/common/tcp_server_connection.py)). Do not run a vendor host, test simulator, and duplicate ASTAR instance against the same passive endpoint simultaneously.
7. **Transport success is only the first gate.** A TCP connect does not prove HSMS Select, GEM communication establishment, correct device ID, accepted event subscriptions, or valid vendor payloads. Acceptance must observe application messages and ASTAR outputs.

## Concrete recommendation

Proceed with `secsgem==0.3.0`; no alternative protocol library is needed for these two tests.

1. Use the existing `simulator.secsgem_equipment` immediately for **ASTAR active -> simulator passive** testing on this machine.
2. Use the packaged Active YAML/launcher for **simulator active -> ASTAR passive** testing. Treat the upstream `gem_equipment.py` as proof of the configuration pattern, not as the vendor simulator.
3. Use the packaged Passive YAML/launcher for **ASTAR active -> simulator passive** testing, keeping the port and device ID identical on both peers.
4. Use the same acceptance sequence for both: TCP connection -> HSMS Selected -> GEM Communicating -> S1F1/S1F2 identity -> report subscription -> S6F11 lifecycle event/alarm -> ASTAR CSV/telemetry evidence.
5. Keep the test on an isolated or approved LAN and do not interpret simulator success as certification against a real tool's complete GEM/E30/E37 or vendor-specific interface.
