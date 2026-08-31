# AStar EAP Middleware — Windows 11 Quickstart

## HTTPS recommended; MQTT is an optional fallback

| | MQTT | HTTPS |
|---|---|---|
| Port | 8883 (TLS) | 443 |
| Token | One gateway token (all machines) | One token **per machine** |
| Works through Cloudflare? | No | Yes |
| Devices auto-provisioned? | Yes | No — create each in Linkstuffs admin first |

Test which port is open from the server before editing config:
```powershell
Test-NetConnection astar-monitoring.linkstuffs.com -Port 8883
Test-NetConnection astar-monitoring.linkstuffs.com -Port 443
```
Use HTTPS by default. Use MQTT only when TLS port 8883 and gateway mode are
approved for the site. Both transports can run simultaneously when required.

---

## 1. Prerequisites

- Windows 11, PowerShell as Administrator
- Deploy ZIP: `astar-middleware-deploy-YYYY-MM-DD-HHMMSS.zip`

Read `PYTHON_VERSION.txt` after extraction. A matching Python installer is
normally bundled; rebuild packages may require that version to be installed
separately.

---

## 2. Install

Before extraction, compare the ZIP's `Get-FileHash -Algorithm SHA256` result
with the expected hash from the trusted release record or approved secure
channel. Do not bypass SmartScreen when the hash or approved signature has not
been verified. After successful verification, SmartScreen's **More info → Run
anyway** and `Unblock-File` may be used.

Extract the whole ZIP first — Windows will run a file from inside the ZIP
viewer, but it cannot see the files next to it.

Then **double-click `SETUP.bat`**. A window opens, asks what this computer is
for, and installs it. There is nothing to type: no execution-policy change, no
paths, no ports.

| You pick | You get |
|---|---|
| **The EAP** | Middleware + the *ASTAR EAP Control* panel. The production choice. |
| **A test machine** | Simulator + the *ASTAR Simulator* panel, with its inbound port opened. |
| **Both** | Both, for trying the whole thing on one computer. |

Windows asks for administrator rights once, when you press Install. Shortcuts
land on the desktop and under **Start → ASTAR SECS-GEM**, and the panel for the
role you chose opens by itself when the install finishes.

For unattended or scripted installs, `install.ps1` is still a first-class entry
point and takes the same choice as a parameter:

```powershell
Set-ExecutionPolicy RemoteSigned -Scope Process -Force
cd C:\Users\<you>\Downloads\astar-middleware-deploy
Unblock-File .\install.ps1
.\install.ps1                     # the EAP host (default)
.\install.ps1 -Role Simulator     # a test machine
.\install.ps1 -Role Both          # both on one box
```

Installs to `C:\SECSGEM_EAP\app\` and adds firewall rules for HSMS and MQTT.
`-Role Middleware` installs no equipment-side code: a production EAP host never
carries a simulator.

---

## 3. Configure machines

Open **ASTAR EAP Control** from the desktop and use the *Machines* tab: **Add**,
then fill the row. The *Equipment host / IP* box offers this machine's own
adapters from a list, so an address never has to be read off `ipconfig` and
retyped. **Test connection** proves the link before anything else is set up —
it probes directly when no service is running yet, so it works on a machine
that has only just been installed.

The panel writes the YAML below; edit the file by hand only if you prefer:

```yaml
machines:
  - endpoint_id:     "TOOL_02"
    display_name:    "DAVINCI200_MC4_HC1_01"
    machine_profile: "davinci_200_mc4_hc1"   # spts_fxp_omega | ptiq_secsgem
    host:            "192.0.2.32"             # TEST-NET; replace with tool IP
    port:            5000
    secs_device_id:  0
    hsms_mode:       "active"                 # active = we dial the tool
    enabled:         true
```

Set `enabled: false` for tools you don't have yet.

---

## 4. Set tokens

Get tokens from Linkstuffs admin → Entities → Devices → Manage credentials.
Store them in machine environment variables and reference those variables from
the installed `production.yaml`; never paste a live token into a Git checkout.

**MQTT:**
```yaml
linkstuffs:
  enabled:      true
  host:         "astar-monitoring.linkstuffs.com"
  port:         8883
  tls:          true
  allow_insecure: false
  access_token: "${LINKSTUFFS_GATEWAY_ACCESS_TOKEN}"
linkstuffs_http:
  enabled: false
```

**HTTPS (Cloudflare path):**
```yaml
linkstuffs:
  enabled: false
linkstuffs_http:
  enabled:  true
  base_url: "https://astar-monitoring.linkstuffs.com"
  device_tokens:
    DAVINCI200_MC4_HC1_01: "${LINKSTUFFS_HTTP_DAVINCI_TOKEN}"
    SPTS_fxP_OMEGA_01:     "${LINKSTUFFS_HTTP_SPTS_TOKEN}"
```

Validation fails when an enabled HTTPS machine has no resolved token.

---

## 5. Validate and test

```powershell
cd C:\SECSGEM_EAP\app

# Catch config errors before starting
python -m eap_middleware validate-config --config config\production.yaml

# Test HSMS connection to one tool (no data sent to Linkstuffs)
python -m eap_middleware test-machine --config config\production.yaml --endpoint-id TOOL_02

# Test Linkstuffs connectivity
python -m eap_middleware test-linkstuffs --config config\production.yaml

# Run interactively to confirm live connections
python -m eap_middleware run-service --config config\production.yaml
```

Within ~30 s you should see `Connected to <tool IP>` per machine.

---

## 6. Install as Windows service

Requires `nssm.exe` — copy it to `C:\Tools\nssm\nssm.exe` from any PC
with internet, then:

```powershell
cd C:\SECSGEM_EAP\app
scripts\install_service.ps1          # auto-detects Python and paths

Start-Service AstarSecsGemEapMiddleware
Get-Service   AstarSecsGemEapMiddleware   # expect: Status Running
```

No NSSM? Use Task Scheduler instead — see `deploy\README_DEPLOY.txt` Step 9.

Tail the log live:
```powershell
Get-Content C:\SECSGEM_EAP\logs\service_stderr.log -Tail 50 -Wait
```

Service crashes with no log? Check Windows Event Viewer → Windows Logs →
Application for the Python error.

---

## DaVinci 200 MC4 HC1 — real-hardware notes

The DaVinci is **HSMS-passive** (the middleware dials out — keep `hsms_mode: active`),
device id `0`, port `5000`. Only one host may hold the HSMS session at a time —
disconnect any existing MES/OEM connection first.

Two optional per-machine flags handle real-tool edge cases (both default off,
both safe to leave off if your tool is configured normally):

```yaml
machines:
  - endpoint_id: "TOOL_02"
    display_name: "DAVINCI200_MC4_HC1_01"
    machine_profile: "davinci_200_mc4_hc1"
    host: "10.10.20.32"
    port: 5000
    request_online: false        # set true if the tool may sit OFF-LINE: it
                                  # then ignores subscription/status/alarm
                                  # requests and sends NO events. S1F17 lifts it
                                  # ON-LINE (does NOT take REMOTE control).
    drain_spool_on_connect: false # set true to pull back events the tool spooled
                                  # during a host/network outage (S6F23).
```

**On a DaVinci, `drain_spool_on_connect` does nothing until the tool is told
to spool.** The SECS-Items workbook's own equipment-constant defaults are:

| ECID | Constant | Default | What it means |
|---|---|---:|---|
| 4020001 | `EnableSpooling` | `0` | Spooling is **off**. Nothing is buffered, so there is nothing to drain. |
| 4020003 | `MaxSpoolMessages` | `20` | Twenty messages — seconds of events on a busy tool. |
| 4020002 | `OverWriteSpool` | `TRUE` | Past twenty, the **oldest** messages are discarded. |
| 4020004 | `MaxSpoolTransmit` | `5` | Drained five at a time. |

At those defaults a middleware or network outage is **unrecoverable data
loss**, not a backlog. If the tool owner can change them, ask for
`EnableSpooling = 1` and `MaxSpoolMessages` raised to cover your worst expected
outage; then set `drain_spool_on_connect: true` here. If they cannot, plan
maintenance windows around it rather than relying on recovery.

The NexGen MG has no spool at all — its manual marks spooling unsupported and
all four spool status variables "Not supported" — so the same caution applies
there permanently and `drain_spool_on_connect` should stay `false`.

> **`request_online` on a DaVinci has an operator-visible consequence.** The
> Software Operation Manual §9.6: *"If the tool operates in control state
> 'Online Remote' internal interlocks for production operation of the tool
> (jobs) will be enabled. This means the user cannot create or modify any
> control or process jobs. Additionally carrier management/handling (e.g.
> cancel carrier, proceed with carrier, dock, undock) cannot be operated
> locally (only from host)."* S1F17 does not itself select REMOTE — the
> LOCAL/REMOTE substate follows the tool's own switch — but if that switch is
> at REMOTE, lifting the tool out of OFF-LINE puts it in Online Remote and
> locks local job and carrier control. Leave it `false` on a DaVinci unless
> the tool owner expects that. It is `true` for the NexGen MG for the opposite
> reason: MG manual §3.2 says an OFF-LINE MG answers `Sx,F0` to every host
> primary except establish-communications and S1F17, so without it you get a
> green connect and a permanently empty feed.

**E30 vs E40 event style** (set on the tool's HostInterface INI): use **E30**.
In E40 the tool sends Process Job events on Stream 16 instead of S6F11 — the
middleware now ingests those automatically, but E40 data is coarser. If you see
an `e40_mode` health note on the dashboard, ask the tool owner to switch the
HostInterface to E30/S6F11 for full carrier/substrate/alarm detail.

**Health states pushed to the dashboard** (watch these per machine):

| State | Meaning / action |
|---|---|
| `connected` / `disconnected` | HSMS link up / down |
| `reconnect_attempted` | watchdog is re-dialing a dropped tool |
| `no_event_reports` | subscription acked but tool fires events with no S6F11 → likely E40 or spooling; check report style |
| `no_status_response` | connected but tool ignores S1F3 → likely OFF-LINE; set `request_online: true` or put it ON-LINE |
| `spooled_messages_pending` | tool buffered messages during an outage; enable `drain_spool_on_connect` or drain manually |
| `e40_mode` | tool is in E40 report style (coarse data) — switch it to E30 |
| `event_reports_ok` | S6F11 reports are flowing again (clears `no_event_reports`) |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `TcpTestSucceeded: False` on 8883 | Keep MQTT disabled and use HTTPS |
| `Connection refused` to tool | Wrong IP/port, or tool HSMS is off |
| `Connection timeout` to tool | Firewall blocking TCP to tool, or wrong subnet |
| No devices in Linkstuffs (MQTT) | Gateway device needs "Is gateway" ticked in Linkstuffs admin |
| HTTPS 401 Unauthorized | Token mismatch — re-copy from Linkstuffs admin |
| CSV files not appearing | Check `D:\` exists; network share errors are logged but don't stop local CSV |
| SmartScreen blocks install.ps1 | Right-click → Properties → Unblock, then re-run |
| Windows Firewall popup on start | Run `New-NetFirewallRule` commands from `README_DEPLOY.txt` Step 8 |

More detail: [`README_DEPLOY.txt`](../deploy/README_DEPLOY.txt) · [`OPERATIONS.md`](OPERATIONS.md) · [`LINKSTUFFS_SETUP.md`](LINKSTUFFS_SETUP.md)
