# AStar SECS/GEM EAP Middleware — Windows 11 Quickstart (Detailed)

> This guide takes a fresh Windows 11 PC to a running service that connects to a **MueTec DaVinci 200 MC4 HC1** (and other tools) over SECS/GEM and forwards data to **ThingsBoard / Linkstuffs** over HTTPS, while writing per-lot CSV files.
>
> Read it top to bottom the first time. Every command is meant to be copy-pasted into **PowerShell**.

---

## 0. What you are installing

| Item | Value |
|---|---|
| Service name | `AstarSecsGemEapMiddleware` |
| Install location | `C:\SECSGEM_EAP\app\` |
| Config file | `C:\SECSGEM_EAP\app\config\production.yaml` |
| Logs | `C:\SECSGEM_EAP\logs\` |
| Per-lot CSV | `D:\MachineData\EAP_<display_name>\csv_in\` |
| Upstream | ThingsBoard/Linkstuffs HTTPS `POST /api/v1/{token}/telemetry` |
| SECS library | secsgem 0.3.0 (HSMS-SS, SECS-II, GEM/GEM300) |

**Data flow:** `DaVinci tool → HSMS/SECS-II → middleware → (HTTPS telemetry → ThingsBoard) + (per-lot CSV → D:\ and network share)`

---

## 1. Pre-flight checklist

Fill this in **before** you touch the PC. Get the per-tool values from the tool owner / vendor.

**Network & PC**

- [ ] Windows 11, you have a local **Administrator** account
- [ ] The PC is on the **same network/VLAN** as the tools and can `ping` each tool IP
- [ ] Outbound **HTTPS (443)** to the ThingsBoard/Linkstuffs host is allowed
- [ ] Drive **`D:\`** exists and is writable (per-lot CSVs are written there)

**Per tool (one row each)**

| Field | DaVinci example | Your value |
|---|---|---|
| `endpoint_id` | `TOOL_02` | |
| `display_name` | `DAVINCI200_MC4_HC1_01` | |
| `machine_profile` | `davinci_200_mc4_hc1` | |
| `host` (tool HSMS IP) | `10.10.20.32` | |
| `port` | `5000` | |
| `secs_device_id` | `0` | |
| `hsms_mode` | `active` | |
| HTTPS device token | from Linkstuffs admin | |

> **The single most common first-connection failure is a `secs_device_id` mismatch.** The tool's HSMS Device ID is configurable on the tool (often `0`, range `0–32767`). It **must** equal `secs_device_id` in your config or the tool will reject the HSMS Select and you will never connect. Confirm this number with the tool owner.

Available profiles: `davinci_200_mc4_hc1`, `spts_fxp_omega`, `ptiq_secsgem`.

---

## 2. Confirm the package Python version

Read `PYTHON_VERSION.txt` at the extracted package root. The `python\` directory
is conditional: the normal offline release includes a matching installer, but
a package built with `REBUILD_WHEELS=1` may require that exact Python version to
be installed separately.

Confirm the required and installed versions:

```powershell
Get-Content .\PYTHON_VERSION.txt
python --version
```

> If the versions differ and `python\` is absent, install the exact version from
> `PYTHON_VERSION.txt` or pass its interpreter path with `-PythonExe`.

---

## 3. Allow the reviewed installer for this session

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process -Force
```

---

## 4. Extract and run the installer

1. Copy the deploy ZIP and obtain its expected SHA-256 from the trusted release
   record or approved secure channel.
2. Verify the ZIP with `Get-FileHash -Algorithm SHA256`; stop on any mismatch.
3. Extract the verified ZIP to `C:\Users\<you>\Downloads\astar-middleware-deploy`.
4. In **PowerShell as Administrator**:

```powershell
cd C:\Users\<you>\Downloads\astar-middleware-deploy
Unblock-File .\install.ps1
.\install.ps1
```

> Only after the trusted SHA-256 or an approved signature is verified may you
> use SmartScreen **More info → Run anyway** or `Unblock-File`.

The installer:

- creates `C:\SECSGEM_EAP\{app,logs,data,archive,machines}`
- copies the source (including the generated **`output\davinci200_mc4_hc1\EventSubscription.json`**, which tells the tool exactly which events to report)
- installs all Python packages **offline** from the bundled `wheels\` folder
- runs an import smoke check

Expected tail:

```
==> Installing Python dependencies offline   [OK] Dependencies installed
==> Smoke checking middleware imports         [OK] All imports OK
==> Installation complete.
```

If you see red errors, screenshot and stop here.

---

## 5. Configure your tools

Open the config:

```powershell
notepad C:\SECSGEM_EAP\app\config\production.yaml
```

Edit the `machines:` list — one block per tool:

```yaml
machines:
  - endpoint_id:     "TOOL_02"
    display_name:    "DAVINCI200_MC4_HC1_01"
    machine_profile: "davinci_200_mc4_hc1"
    host:            "10.10.20.32"     # tool's HSMS IP — change me
    port:            5000              # tool's HSMS port
    secs_device_id:  0                 # MUST match the tool's HSMS Device ID
    hsms_mode:       "active"          # tool is passive → we dial it
    enabled:         true
    enable_alarms:   false             # see step 6
```

- Set `enabled: false` for any tool you are not wiring up yet.
- `hsms_mode: "active"` is correct for the DaVinci (the tool listens, we connect). Only use `passive` if a specific tool is configured to dial out to us.

Save with **Ctrl+S**.

---

## 6. Set the ThingsBoard / Linkstuffs token(s)

Get each device token from **Linkstuffs admin → Entities → Devices → Manage credentials**. Store tokens in machine environment variables and reference them from the installed `production.yaml`; never commit a live token.

For the HTTPS path (the default, works through Cloudflare):

```yaml
linkstuffs:
  enabled: false                 # HTTPS is the recommended production path

linkstuffs_http:
  enabled: true
  base_url: "https://astar-monitoring.linkstuffs.com"
  device_tokens:
    DAVINCI200_MC4_HC1_01: "${LINKSTUFFS_HTTP_DAVINCI_TOKEN}"
    SPTS_fxP_OMEGA_01:     ""     # empty token = device skipped
    PTIQ_01:               ""
  verify_tls: true
```

- The `device_tokens` **key must exactly match** the tool's `display_name`.
- An enabled device with no resolved token causes validation to fail.

**Optional — alarms:** `enable_alarms` defaults to `false`. Most tools already report alarms by default. Only set `enable_alarms: true` for a tool if it does **not**, and only after confirming with the tool owner that it accepts a host "enable all alarms" (S5F3) request.

---

## 7. Tool-side prerequisites (confirm with the tool owner)

These are **on the tool**, not the PC. Getting them wrong means "connected but no data".

- [ ] Tool HSMS is **PASSIVE** (it listens; we connect `active`).
- [ ] Tool's **HSMS Device ID** equals your `secs_device_id`.
- [ ] **No other host** (MES, vendor software) is already holding the tool's single HSMS session — disconnect it first.
- [ ] **Event reports are E30 style, not E40 style.** On Kontron FabLink this is an ini setting and needs a Host Interface restart after changing (Host Interface Manual §4.2.1). The middleware consumes **S6F11** collection-event reports; in E40 style the lot/wafer events will not arrive as expected.

---

## 8. Validate the config

```powershell
cd C:\SECSGEM_EAP\app
python -m eap_middleware validate-config --config config\production.yaml
```

Look for `"valid": true`. Fix any reported error before moving on.

---

## 9. Test one tool (HSMS only — nothing sent upstream)

This opens the real HSMS session and reads the tool's identity. It is the honest "will it connect on the first try" check.

```powershell
python -m eap_middleware test-machine --config config\production.yaml --endpoint-id TOOL_02
```

- **Success:** the tool model/identity prints within ~10 seconds.
- `Connection refused` → wrong IP/port, or tool HSMS is off, or another host is connected.
- `Connection timeout` → network/firewall path blocked (`ping 10.10.20.32` first).
- `Device ID mismatch` → fix `secs_device_id`.

Test every enabled tool at once:

```powershell
python -m eap_middleware test-machine --config config\production.yaml --endpoint-id ALL
```

---

## 10. Test the upstream (ThingsBoard / Linkstuffs)

```powershell
python -m eap_middleware test-linkstuffs --config config\production.yaml
```

If it fails, re-check the token (step 6) and `base_url`. A `401 Unauthorized` means the token is wrong for that device.

---

## 11. First live run (interactive)

Run in the foreground so you can watch it connect:

```powershell
python -m eap_middleware run-service --config config\production.yaml
```

Within ~30 seconds each enabled tool should log **Communication established**. Trigger (or wait for) a lot and confirm:

- a per-lot CSV appears under `D:\MachineData\EAP_DAVINCI200_MC4_HC1_01\csv_in\`
- telemetry appears on the device in ThingsBoard/Linkstuffs

Press **Ctrl+C** to stop.

---

## 12. Install as a Windows service (auto-start on boot)

### NSSM (recommended)

1. Obtain `nssm.exe` only from the site-approved release source. Before copying
   or executing it, verify an approved code signature or compare its SHA-256
   with the trusted release record:

```powershell
Get-AuthenticodeSignature .\nssm.exe
Get-FileHash .\nssm.exe -Algorithm SHA256
```

Stop if the signature is not approved or the hash differs. After verification,
copy it to `C:\Tools\nssm\nssm.exe`.

2. Register the service:

```powershell
cd C:\SECSGEM_EAP\app
scripts\install_service.ps1            # auto-detects Python + paths
# If Python isn't on PATH or NSSM is elsewhere:
# scripts\install_service.ps1 -PythonExe "C:\Python311\python.exe" -NssmExe "C:\Tools\nssm\nssm.exe"
```

3. Start and verify:

```powershell
Start-Service AstarSecsGemEapMiddleware
Get-Service   AstarSecsGemEapMiddleware     # Status should be: Running
```

---

## 13. Confirm it is working

```powershell
# Tail the live log
Get-Content C:\SECSGEM_EAP\logs\service_stderr.log -Tail 50 -Wait
```

You want to see, per tool: `Communication established`, then `Reports defined successfully` → `Events linked successfully` → `Events enabled successfully`. After a lot runs, telemetry flows to ThingsBoard and a CSV lands in `D:\MachineData\...`.

> No log output at all? Open **Event Viewer → Windows Logs → Application** and filter by `python`.

---

## 14. Day-2 operations

| Task | How |
|---|---|
| Change a tool IP / token | Edit `config\production.yaml`, then `Restart-Service AstarSecsGemEapMiddleware` |
| Change which SVIDs are polled | Edit `C:\SECSGEM_EAP\machines\<display_name>\config\SvidList.json` (hot-reloaded, no restart) |
| Turn data collection on/off | `DataCollectSwitch.json` in the same per-machine folder (hot-reloaded) |
| Stop / start | `Stop-Service` / `Start-Service AstarSecsGemEapMiddleware` |
| Collect logs for support | ZIP `C:\SECSGEM_EAP\logs\` and send it |

**Outage resilience:** telemetry that can't be delivered is queued in a durable SQLite outbox (`C:\SECSGEM_EAP\data\`) and retried with backoff, so short ThingsBoard/network outages do not lose data (30-day retention).

---

## 15. Where everything lives

```
C:\SECSGEM_EAP\
├─ app\                         middleware code + config
│  ├─ config\production.yaml    your config (IPs, tokens)
│  └─ output\davinci200_mc4_hc1\EventSubscription.json   curated event subscription
├─ logs\                        service_stdout.log / service_stderr.log
├─ data\                        SQLite outbox (queued telemetry)
├─ archive\
└─ machines\<display_name>\config\   per-machine admin JSON (hot reload)

D:\MachineData\EAP_<display_name>\csv_in\        local per-lot CSV
\\TD-DATASVR-F2C4\TD_<display_name>.csv_in\      network mirror (optional)
```

Per-lot CSV header (fixed): `Datetime,ToolEvent,EAP_ToolName,LoadPort,Chamber,LotID,WaferID,Recipe,SECSGEM_Raw_Event`

---

## 16. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `validate-config` error | Read the message — usually a typo or a missing required field in `production.yaml` |
| `python --version` differs from `PYTHON_VERSION.txt` | Use the bundled installer or install that exact version and pass `-PythonExe` |
| test-machine: `Connection refused` | Wrong IP/port, tool HSMS off, or another host already connected |
| test-machine: `timeout` | Network/VLAN/firewall blocking; `ping` the tool first |
| test-machine: `Device ID mismatch` | Set `secs_device_id` to the tool's HSMS Device ID |
| Connected but **no events** | Tool is in **E40** report style (set to E30), or the wrong profile is selected |
| `load_port` blank on some events | Tool emitted a process event before any port-bearing event; confirm event ordering with the tool owner (data still flows) |
| No alarms arriving | Only if the tool does not report alarms by default, confirm with its owner that host S5F3 “enable all alarms” is accepted; then set `enable_alarms: true` |
| ThingsBoard `401 Unauthorized` | Wrong device token — re-copy from Linkstuffs admin |
| Telemetry OK but **no CSV** | `D:\` missing or not writable; check `logs\` for IOError |
| Config change ignored | `Restart-Service AstarSecsGemEapMiddleware` |
| SmartScreen blocks install | Verify the trusted ZIP hash/signature first, then run `Unblock-File .\install.ps1` |

---

## 17. Appendix — minimal working `production.yaml`

```yaml
linkstuffs:
  enabled: false

linkstuffs_http:
  enabled: true
  base_url: "https://astar-monitoring.linkstuffs.com"
  device_tokens:
    DAVINCI200_MC4_HC1_01: "${LINKSTUFFS_HTTP_DAVINCI_TOKEN}"
  timeout_sec: 10
  retry_count: 3
  retry_delay_sec: 1
  verify_tls: true

paths:
  install_dir: "C:/SECSGEM_EAP"
  log_dir: "C:/SECSGEM_EAP/logs"
  data_dir: "C:/SECSGEM_EAP/data"
  archive_dir: "C:/SECSGEM_EAP/archive"
  outbox_db: "C:/SECSGEM_EAP/data/outbox.sqlite3"

logging:
  level: "INFO"

reconnect_interval_sec: 10

machines:
  - endpoint_id: "TOOL_02"
    display_name: "DAVINCI200_MC4_HC1_01"
    machine_profile: "davinci_200_mc4_hc1"
    host: "10.10.20.32"
    port: 5000
    secs_device_id: 0
    hsms_mode: "active"
    enabled: true
    enable_alarms: false
    local_csv_path: "D:/MachineData/EAP_DAVINCI200_MC4_HC1_01/csv_in"
```

> Set `logging.level: "DEBUG"` temporarily when troubleshooting a connection, then switch back to `INFO`.
