# NexGen MG Series: Two-VM End-to-End Guide

From two empty VMware Fusion VMs to a NexGen MG simulator feeding live
telemetry into Linkstuffs (ThingsBoard).

**Scope:** VM creation → Windows install → networking → middleware + GUI on both
VMs → MG simulator → HSMS handshake → Linkstuffs upstream.

Companion docs, referenced rather than duplicated:
[TWO_VM_FABNET_TEST_SETUP.md](TWO_VM_FABNET_TEST_SETUP.md) (rig background and
the DaVinci path), [LINKSTUFFS_SETUP.md](LINKSTUFFS_SETUP.md) (the ThingsBoard
side in full), [OPERATIONS.md](OPERATIONS.md) (service operation).

---

## Part 0 — Topology

| VM | Role | Runs | FabNet IP |
|---|---|---|---|
| `astar-server` | **Host / EAP** | middleware service + GUI | 192.168.102.128 |
| `machine` | **Equipment** | NexGen MG simulator + GUI | 192.168.102.129 |

```
  machine VM                     astar-server VM              Internet
 ┌──────────────────┐          ┌────────────────────┐       ┌──────────────┐
 │ NexGen MG sim    │  HSMS    │ EAP middleware     │ HTTPS │  Linkstuffs  │
 │ passive :5051    │◄─────────┤ TOOL_04 active     ├──────►│  ThingsBoard │
 │                  │  FabNet  │ profile:           │       │  /api/v1/    │
 │ GUI (config)     │          │  nexgen_mg_series  │       │  <token>/    │
 └──────────────────┘          │ GUI (control)      │       │  telemetry   │
                               └────────────────────┘       └──────────────┘
        192.168.102.129              192.168.102.128
```

The MG simulator **listens**; the middleware **connects out** to it. That is
why the machine block is `hsms_mode: active` — the mode names the *middleware's*
role, not the equipment's.

Two network adapters per VM:

- **WAN** — Fusion "Share with my Mac". Internet. Needed only to reach
  Linkstuffs and to download build tools.
- **FabNet** — Fusion "Private to my Mac". The equipment segment. No gateway.

---

## Part 1 — Create both VMs

You need a Windows 11 **ARM64** ISO (your Mac is arm64; x64 Windows will not
boot). Get it from `microsoft.com/software-download/windows11`, choosing the
ARM64 option. Allocate per VM: 4 GB RAM, 4 CPU, 64 GB disk.

**File → New → Install from disc or image**, point at the ISO. Name one
`astar-server`, one `machine`.

Then for **each** VM: **Virtual Machine → Settings**

1. **Network Adapter** → **Share with my Mac**. This is NIC 1 (WAN).
2. **Add Device… → Network Adapter** to add NIC 2.
3. Click **Network Adapter 2** → under **Custom**, choose **Private to my Mac**.
   This is NIC 2 (FabNet).
4. Confirm **Connect Network Adapter** is ticked on both.
5. **Isolation** → enable **Drag and Drop**.

Both VMs' NIC 2 now sit on the same private subnet. That is your virtual switch.

> The Fusion VM name, the Windows hostname, and the login-screen name are three
> different things and they disagree on this rig. The login screen shows the
> *user account*. Always identify a VM by running `hostname` inside it.

### Install Windows on both

At **"Let's connect you to a network"**, press **Shift+F10** and run:

```cmd
oobe\bypassnro
```

The VM reboots into setup with an **"I don't have internet"** option. Use it,
then **Limited setup** for a local account. This keeps a throwaway test VM off
your Microsoft account.

Finally: **Virtual Machine → Install VMware Tools**, then reboot.

---

## Part 2 — Network

Do everything below in **both** VMs, in PowerShell **as Administrator**.

### 2.1 Identify and rename adapters

```powershell
Get-NetIPConfiguration
```

Identify FabNet by its **blank `IPv4DefaultGateway`**, not by name or order —
the numbering is not consistent between VMs. The adapter that *has* a gateway
is WAN.

```powershell
Rename-NetAdapter -Name "Ethernet 2" -NewName "FabNet"
Rename-NetAdapter -Name "Ethernet"   -NewName "WAN"
```

Substitute the real current names. `Rename-NetAdapter` takes `-Name` directly
and must **not** be piped from `Get-NetAdapter`.

### 2.2 Open the firewall

Fusion's private network appears as an *Unidentified network*, which Windows
classifies as **Public**, and Public blocks inbound ICMP and inbound TCP.

```powershell
Set-NetConnectionProfile -InterfaceAlias "FabNet" -NetworkCategory Private
New-NetFirewallRule -DisplayName "Allow ICMPv4-In FabNet" -Protocol ICMPv4 -IcmpType 8 -Direction Inbound -Action Allow
```

On the **machine** VM only, also open the HSMS port:

```powershell
New-NetFirewallRule -DisplayName "Allow HSMS 5051 In" -Direction Inbound -Protocol TCP -LocalPort 5051 -Action Allow
```

`Set-NetConnectionProfile` sometimes fails to stick on an Unidentified network.
That is a known Windows limitation and does not matter here — both rules are
written without `-Profile`, so they apply to Public too.

### 2.3 Verify the switch

Get each VM's own FabNet address:

```powershell
(Get-NetIPAddress -InterfaceAlias FabNet -AddressFamily IPv4).IPAddress
```

Then ping **the other** VM from each side:

```powershell
ping 192.168.102.128
```

```powershell
ping 192.168.102.129
```

**Do not ping the VM's own address.** A self-ping always succeeds and proves
nothing. Two tells that you have a real cross-machine ping: the address you
pinged is not the one this VM reports for itself, and the first reply is
noticeably slower than the rest (ARP — e.g. `19ms` then `1ms`), whereas a
self-ping is uniformly `<1ms`.

Both directions must report `Lost = 0 (0% loss)` before you go further.

---

## Part 3 — Install the middleware and GUI on both VMs

Install on **both**. On the machine VM the middleware itself goes unused — the
package is the offline delivery vehicle for Python 3.11 x64, `secsgem`, the
`simulator/` source, and the GUI.

### 3.1 Build the package on the Mac

```bash
./scripts/build_deploy_package.sh
```

This writes `deploy_out/astar-middleware-deploy-<timestamp>.zip` (~96 MB) with
the bundled x64 Python installer and every dependency as a `win_amd64` wheel.

### 3.2 Transfer

Drag-and-drop from Finder onto each VM window, or serve it from the Mac's
FabNet address, which reaches the VMs and nothing else:

```bash
python3 -m http.server 8000 --bind 192.168.102.1
```

Then in each VM browse to `http://192.168.102.1:8000`.

Extract to a plain local path such as `C:\astar-deploy`. **Not OneDrive** —
online-only placeholders break the SHA-256 check in the next step.

### 3.3 Install

In each VM, PowerShell **as Administrator**:

```powershell
Set-ExecutionPolicy RemoteSigned -Scope Process -Force
Set-Location C:\astar-deploy
Get-ChildItem -Recurse -File | Unblock-File
.\install.ps1
```

`install.ps1` is idempotent. It verifies the release manifest, installs the
bundled x64 Python 3.11, creates `C:\SECSGEM_EAP\`, copies the source
(including `gui/`), pip-installs everything **offline** from the bundled
wheels, smoke-tests the imports, adds firewall rules, and opens
`production.yaml` in Notepad.

> **Do not install ARM64 Python in the guests.** The wheels are `win_amd64` and
> the bundled interpreter is `python-3.11.9-amd64.exe`. Windows 11 ARM runs x64
> under emulation — this is deliberate and it works. ARM64 Python makes pip
> reject every bundled wheel with "not a supported wheel on this platform".

If you built `AstarMiddleware-Setup-1.0.0-win-x64.exe`, run that instead — it
does all of the above plus Start Menu and desktop shortcuts.

### 3.4 Confirm

```powershell
python -c "import sys; print(sys.version)"
python -c "import secsgem, yaml, paho.mqtt.client, cryptography, pandas; print('deps ok')"
Test-Path C:\SECSGEM_EAP\app\gui\app.py
```

Expect `3.11.9`, `deps ok`, `True`.

### 3.5 Launch the GUI

```powershell
cd C:\SECSGEM_EAP\app
pythonw -m gui.app --config config\production.yaml
```

A tkinter window opens. Do this on both VMs. If you installed via the setup
exe, use the Start Menu shortcut instead.

The GUI is a **passive client**. It edits `production.yaml`, reads the
service's status snapshot, and submits start/stop/restart/test commands to the
running service through a local command inbox. It owns no HSMS threads, so
closing it leaves sessions running.

---

## Part 4 — machine VM: run the NexGen MG simulator

```powershell
cd C:\SECSGEM_EAP\app
python -m simulator.nexgen_mg_simulator --host 0.0.0.0 --port 5051 --wafers 3 --interval 0.5 --loop
```

Flags: `--host 0.0.0.0` so it accepts across FabNet rather than loopback only;
`--loop` to keep producing lots. Default HSMS role is `passive` (it listens),
which is what you want. Leave this window running.

Other MG-specific switches, from `--help`:

| Flag | Effect |
|---|---|
| `--refuse-band gem300` | reject one subscription band |
| `--start-offline` | start in HOST OFF-LINE so S1F17 is exercised |
| `--no-substrate-ids` | behave as a cassette tool with no GEM300 substrate IDs |
| `--process-state-ascii` | report ProcessState as ASCII, not an integer |
| `--no-alarm` | suppress the alarm event |

> **On GUI-driven simulators.** The GUI can start and stop a simulator, but
> `EapService._start_simulator` gives the simulator the *opposite* HSMS role of
> its machine block and binds `127.0.0.1` when the simulator ends up active.
> That design pairs a simulator with the middleware **on the same box**. Across
> two VMs, run the simulator from the CLI as above. Use the machine VM's GUI for
> editing config and reading logs, not for driving this simulator.

Confirm from the **server** VM that the port is reachable:

```powershell
Test-NetConnection 192.168.102.129 -Port 5051 -InformationLevel Quiet
```

Must be `True`. If `False`, it is the firewall rule from 2.2 or the `--host`
flag — not SECS/GEM.

---

## Part 5 — server VM: point the middleware at it

Edit `C:\SECSGEM_EAP\app\config\production.yaml`. The template already ships a
`TOOL_04` / `NEXGEN_MG_01` block using the `nexgen_mg_series` profile. Change
four things in it:

```yaml
  - endpoint_id: "TOOL_04"
    display_name: "NEXGEN_MG_01"
    machine_profile: "nexgen_mg_series"
    host: "192.168.102.129"        # machine VM FabNet IP
    port: 5051                     # match the simulator
    secs_device_id: 0
    hsms_mode: "active"            # middleware dials out
    enabled: false                 # see Part 6 before flipping this
    runtime_mode: "real"
    request_online: true
    enable_alarms: true
    drain_spool_on_connect: false
    storage:
      log_dir: "C:/SECSGEM_EAP/logs/NEXGEN_MG_01"
      simulator_log_dir: "C:/SECSGEM_EAP/logs/NEXGEN_MG_01/simulator"
      local_csv_path: "C:/SECSGEM_EAP/data/csv_in"   # NOT the D: default
      network_csv_path: ""                            # skip the file-server mirror
      admin_config_path: "C:/SECSGEM_EAP/machines/NEXGEN_MG_01/config"
```

Two edits there are load-bearing and easy to miss:

- **`local_csv_path`** — the template ships `D:/MachineData/...`, and `D:` is
  the DVD drive on a stock VM. In `csv_store.py` the network mirror's `mkdir`
  is wrapped in try/except but the **local** `mkdir` is not, so an unwritable
  local path raises out of the S6F11 handler. Every event then fails with
  `[WinError 5] Access is denied` and no CSV is ever written.
- **`network_csv_path: ""`** — skips the `\\FILESERVER\...` mirror that does
  not exist here.

> **Never edit `production.yaml` with PowerShell 5.1 `Get-Content` without
> `-Encoding UTF8`.** The file's header comment contains an em-dash; the default
> ANSI read plus a UTF-8 write double-encodes it into a control character the
> YAML reader rejects. Use Notepad or the GUI.

### Prove the HSMS path

With the simulator running, on the **server** VM:

```powershell
cd C:\SECSGEM_EAP\app
python -m eap_middleware validate-config --config config\production.yaml
python -m eap_middleware test-machine --config config\production.yaml --endpoint-id TOOL_04
```

`test-machine` works on a **disabled** machine when you name it explicitly —
`cmd_test_machine` matches on `endpoint_id` and only consults the `enabled`
flag for `--endpoint-id ALL`. That is why `enabled: false` is fine right now,
and it is what keeps `validate-config` passing before you have an upstream
(see Part 6).

Expect HSMS to walk `NOT CONNECTED` → `CONNECTED / NOT SELECTED` → `SELECTED`,
S1F1 answered by S1F2, and a final line like:

```
secs-ok: TOOL_04 192.168.102.129:5051 device_id=0 identity=['MG Series', 'NWS MG 1.1.18']
```

That line proves the network path, the HSMS handshake, and GEM identity
exchange all work.

On the simulator side you will then see `ConnectionResetError: [WinError
10054]` and a communication-loss line. **Expected** — `test-machine` hangs up
as soon as it has the identity, and the simulator resumes listening.

**Triage order.** `Test-NetConnection` fails → adapters or firewall, go back to
Part 2. It passes but `test-machine` fails → HSMS-level config (device ID,
active/passive mismatch, wrong port), not the network.

---

## Part 6 — Linkstuffs (ThingsBoard)

### 6.1 The constraint that will bite you first

`eap_middleware/config.py` rejects any machine with `enabled: true` while both
`linkstuffs.enabled` and `linkstuffs_http.enabled` are false:

```
Every enabled machine requires an upstream route: enable linkstuffs_http
with per-device tokens or enable MQTT
```

So `enabled: true` and an upstream must be turned on **together**. There is no
valid "enabled but no telemetry" configuration.

### 6.2 Create the device and get a token

In ThingsBoard, as tenant administrator: **Devices → +** → create a device
named `NEXGEN_MG_01`, then open it → **Manage credentials** → copy the **access
token**.

> On the HTTPS device API, devices are **not** auto-created — that is MQTT
> gateway behaviour. A missing or wrong token makes the publisher drop that
> tool's events **silently**: the tool connects, CSVs are written normally, and
> only the dashboard stays empty, which reads like a ThingsBoard-side fault.

The token map is keyed by `display_name`, not `endpoint_id`. `NEXGEN_MG_01` is
the key.

Full ThingsBoard walkthrough — device profiles, dashboards, alarm rules,
payload shapes — is in [LINKSTUFFS_SETUP.md](LINKSTUFFS_SETUP.md).

### 6.3 Configure the upstream

In `production.yaml` on the **server** VM:

```yaml
linkstuffs_http:
  enabled: true
  base_url: "https://your-thingsboard-host"
  device_tokens:
    NEXGEN_MG_01: "PASTE_THE_ACCESS_TOKEN_HERE"
  timeout_sec: 10
  retry_count: 3
  retry_delay_sec: 1
  verify_tls: true
```

The publisher posts to `{base_url}/api/v1/{token}/telemetry` and
`/attributes`. Keep `verify_tls: true`.

Now flip the machine on:

```yaml
    enabled: true
```

and re-validate:

```powershell
python -m eap_middleware validate-config --config config\production.yaml
```

Expect `"valid": true` and the `TOOL_04` block showing
`"host": "192.168.102.129"`, `"port": 5051`.

### 6.4 Run the service

```powershell
cd C:\SECSGEM_EAP\app
python -m eap_middleware run-service --config config\production.yaml
```

Unlike `test-machine`, `run-service` **does** honour the `enabled` flag — which
is the whole reason for 6.1 and 6.3.

### 6.5 Verify end to end

Four independent places, in order:

1. **Service log** — `C:\SECSGEM_EAP\logs\NEXGEN_MG_01\` shows the HSMS select,
   S1F1/S1F2, then S6F11 event reports arriving as the simulator runs lots.
2. **CSV** — a per-lot file appears under `C:\SECSGEM_EAP\data\csv_in\`. If the
   log shows events but no CSV, re-read the `local_csv_path` trap in Part 5.
3. **Outbox** — `C:\SECSGEM_EAP\data\linkstuffs_http_outbox.sqlite3` grows,
   then drains. Events are queued to this SQLite outbox first and posted
   asynchronously; a stuck, growing outbox means the HTTPS post is failing.
4. **ThingsBoard** — open `NEXGEN_MG_01` → **Latest telemetry**. Values appear
   as lots run.

If 1–3 are healthy and only 4 is empty, it is the token or `base_url` — that is
exactly the silent-drop failure from 6.2.

### 6.6 Offline network testing without a real ThingsBoard

To exercise `run-service` when there is no ThingsBoard to point at, set
`linkstuffs_http.enabled: true` with a dummy token and leave `base_url` at its
placeholder:

```yaml
    NEXGEN_MG_01: "dummy-test-token"
```

Verified safe on this rig: `LinkstuffsHttpPublisher.queue_event` only enqueues
to the SQLite outbox with no inline network I/O, and `csv_writer.append` is an
independent call in `EapService`. You still get the HSMS path, events, and CSVs;
only the upstream post fails and retries.

---

## Part 7 — Driving it from the GUI

With `run-service` running on the server VM, open the GUI there. It reads the
service's status snapshot and can act on it:

- **Machines** — live per-endpoint HSMS/GEM/HTTPS state; Start, Stop, Restart
  and Test per machine; Add / Duplicate / Remove; connection, storage, HTTPS and
  simulator settings.
- **Upstream** — Linkstuffs HTTPS, Linkstuffs MQTT, and the legacy Tool Data
  API. Tokens are masked until **Show secrets** is ticked.

Use it instead of hand-editing YAML once things work — it writes the config
atomically and checks the revision, so it will not clobber a concurrent edit.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `ping` fails both ways | FabNet adapter or firewall — Part 2.2 |
| `Test-NetConnection … 5051` is `False` | simulator not running, bound to loopback (missing `--host 0.0.0.0`), or port rule missing |
| Port open but `test-machine` fails | HSMS config: device ID, port, or active/passive mismatch |
| "Every enabled machine requires an upstream route" | `enabled: true` with both upstreams off — Part 6.1 |
| Events in log, no CSV, `[WinError 5]` | `local_csv_path` still points at `D:` — Part 5 |
| Everything healthy, ThingsBoard empty | wrong/missing device token, or device not created — Part 6.2 |
| YAML parse error after a PowerShell edit | em-dash double-encoded; edit with Notepad or the GUI |
| pip: "not a supported wheel on this platform" | ARM64 Python installed; use the bundled x64 one |
| No **Sharing** pane in Fusion | expected on Apple Silicon; use drag-and-drop or the HTTP server |
| `install.ps1` fails SHA-256 verification | package sits in OneDrive with online-only placeholders; copy to a plain local path |
| Simulator logs `WinError 10054` after `test-machine` | expected — the test disconnects once it has the identity |
| `Reconnect watchdog: TOOL_04 is disconnected` repeating **while events keep arriving** | two things are talking to the simulator. See below. |

### The reconnect loop that never recovers

Seen on this rig on 2026-08-19: `middleware.log` repeated
`Reconnect watchdog: TOOL_04 is disconnected, restarting session.` every
30–60 s for forty minutes, while `Alarm SET`/`Alarm CLEARED` kept arriving
throughout. The two facts look contradictory and are not.

HSMS equipment serves **exactly one peer**. If a connection the service no
longer tracks is still attached to the simulator, it goes on receiving and
acknowledging events — so the log keeps filling — while every new host the
watchdog creates finds the port occupied and can never establish. Nothing
reconnects, because nothing ever disconnected.

Confirm it on the **machine** VM:

```powershell
netstat -ano | Select-String ":5051"
```

One `ESTABLISHED` line and **no** `LISTENING` line means the slot is taken and
the simulator has stopped accepting. On the **server** VM, check the
equipment's own capture: across the "restarts", the tool's `system:` bytes
advance by exactly one and there is no `Select.req` and no `S1F13` — proof the
TCP connection was never broken.

Fix: stop every middleware instance (including a GUI running the service
in-window — the banner says *"Service running in this window"*), confirm the
port is idle, then start one. The service now closes a replaced connection
unconditionally, and after three failed attempts logs one `ERROR` naming
`gem_state=` so the cause is in the log rather than inferred.

---

## Quick reference

| Item | Value |
|---|---|
| Server FabNet IP | 192.168.102.128 |
| Machine FabNet IP | 192.168.102.129 |
| Mac FabNet IP | 192.168.102.1 |
| MG simulator port | 5051 (passive) |
| Endpoint / display name | `TOOL_04` / `NEXGEN_MG_01` |
| Profile | `nexgen_mg_series` |
| Install root | `C:\SECSGEM_EAP` |
| App root | `C:\SECSGEM_EAP\app` |
| ThingsBoard telemetry URL | `{base_url}/api/v1/{token}/telemetry` |
