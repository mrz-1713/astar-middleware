# ASTAR Middleware Full Deployment Guide: Mac to Windows 11 Server

This guide covers the full field workflow:

1. Connect from a Mac to a Windows 11 server.
2. Copy the ASTAR middleware ZIP package to the server.
3. Install the middleware on the server.
4. Prepare configuration, network access, and tool prerequisites.
5. Validate, test, deploy, and run the middleware as a Windows service.

The target server is assumed to be Windows 11. Commands in Windows sections are PowerShell commands.

---

## 1. What Will Be Installed

| Item | Value |
|---|---|
| Windows install root | `C:\SECSGEM_EAP` |
| Application folder | `C:\SECSGEM_EAP\app` |
| Main config | `C:\SECSGEM_EAP\app\config\production.yaml` |
| Logs | `C:\SECSGEM_EAP\logs` |
| Data/outbox | `C:\SECSGEM_EAP\data` |
| Service name | `AstarSecsGemEapMiddleware` |
| Local CSV output | `D:\MachineData\EAP_<display_name>\csv_in` |
| Default HTTPS upstream | `https://astar-monitoring.linkstuffs.com` |

The middleware connects to tools over HSMS/SECS/GEM, writes per-lot CSV files, and sends telemetry to Linkstuffs / ThingsBoard.

---

## 2. Information Needed Before Deployment

Collect this before opening the server.

### Windows Server Access

- Windows server IP address or hostname.
- Windows username and password.
- Confirmation that the account has local Administrator access.
- Confirmation that Remote Desktop is enabled on the Windows server.

### Per Tool

| Field | Example | Notes |
|---|---|---|
| `endpoint_id` | `TOOL_02` | Internal middleware ID. |
| `display_name` | `DAVINCI200_MC4_HC1_01` | Must match Linkstuffs token key. |
| `machine_profile` | `davinci_200_mc4_hc1` | Supported: `davinci_200_mc4_hc1`, `spts_fxp_omega`, `ptiq_secsgem`. |
| Tool HSMS IP | `10.10.20.32` | Tool IP reachable from Windows server. |
| Tool HSMS port | `5000` | Confirm with tool owner/vendor. |
| `secs_device_id` | `0` | Must exactly match the tool's HSMS Device ID. |
| HSMS mode | `active` | Normal case: tool is passive, middleware connects to it. |
| Linkstuffs device token | `...` | One token per device for HTTPS mode. |

Important: only one host can normally hold an HSMS connection to a tool. If MES or vendor software is already connected, disconnect it before testing this middleware.

---

## 3. Prepare the Deployment ZIP on the Mac

From the repository root on the Mac:

```bash
cd /Users/nrzngr/Desktop/astar-middleware-main
./scripts/build_deploy_package.sh
```

The script creates:

```text
deploy_out/astar-middleware-deploy-YYYY-MM-DD-HHMMSS.zip
deploy_out/astar-middleware-deploy-YYYY-MM-DD-HHMMSS.zip.sha256
```

Use the newest ZIP in `deploy_out`. Publish its SHA-256 through the approved
release record or another trusted channel; the adjacent `.sha256` file is a
convenience copy, not an independent trust source.

The ZIP contains:

- `install.ps1`
- `README_DEPLOY.txt`
- `SETUP_CHECKLIST.txt`
- `QUICKSTART.md`
- `source\`
- `wheels\`
- `python\` if the offline Python installer is available

---

## 4. Connect to the Windows 11 Server from Mac

### Option A: Microsoft Remote Desktop

1. Install **Microsoft Windows App** or **Microsoft Remote Desktop** from the Mac App Store.
2. Open the app.
3. Add a PC:
   - PC name: Windows server IP or hostname.
   - User account: Windows username and password.
4. Connect.
5. Accept the certificate warning if this is the expected internal server.

### Option B: Finder SMB Share

Use this if file sharing is enabled on the Windows server.

1. In Finder, press `Cmd+K`.
2. Enter:

```text
smb://<windows-server-ip>/C$
```

or a shared folder path:

```text
smb://<windows-server-ip>/<share-name>
```

3. Log in with a Windows Administrator account.
4. Copy the deployment ZIP to a simple path such as:

```text
C:\Users\<windows-user>\Downloads
```

### Option C: RDP Shared Folder

If using Microsoft Remote Desktop:

1. Edit the saved PC connection.
2. Open **Folders**.
3. Add a Mac folder, for example:

```text
/Users/nrzngr/Desktop/astar-middleware-main/deploy_out
```

4. Reconnect to the Windows server.
5. Inside Windows, open File Explorer.
6. The shared Mac folder appears under redirected drives or devices.
7. Copy the ZIP to:

```text
C:\Users\<windows-user>\Downloads
```

### Option D: SCP

Use this only if OpenSSH Server is enabled on Windows.

From the Mac:

```bash
scp deploy_out/astar-middleware-deploy-YYYY-MM-DD-HHMMSS.zip* <windows-user>@<windows-server-ip>:/C:/Users/<windows-user>/Downloads/
```

---

## 5. Extract the ZIP on Windows

On the Windows server:

1. Open File Explorer.
2. Go to:

```text
C:\Users\<windows-user>\Downloads
```

3. Obtain the expected SHA-256 from the trusted release record or approved
   secure channel. Do not treat a hash received with the ZIP as trusted by
   itself. Verify it before bypassing SmartScreen:

```powershell
$Zip = Get-ChildItem .\astar-middleware-deploy-*.zip | Sort-Object LastWriteTime -Descending | Select-Object -First 1
(Get-FileHash $Zip.FullName -Algorithm SHA256).Hash
```

4. Stop if the calculated hash differs from the trusted expected value.
5. Right-click the verified ZIP and choose **Extract All**.
6. Extract to:

```text
C:\Users\<windows-user>\Downloads\astar-middleware-deploy
```

After extraction, confirm this file exists:

```text
C:\Users\<windows-user>\Downloads\astar-middleware-deploy\install.ps1
```

---

## 6. Install the Middleware

Open **PowerShell as Administrator**.

Allow the reviewed installer only for the current PowerShell session:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process -Force
```

Go to the extracted package:

```powershell
cd C:\Users\<windows-user>\Downloads\astar-middleware-deploy
```

Run the installer:

```powershell
Unblock-File .\install.ps1
.\install.ps1
```

Expected installer behavior:

- Checks or installs the required Python version.
- Creates `C:\SECSGEM_EAP`.
- Copies source files to `C:\SECSGEM_EAP\app`.
- Installs Python dependencies from the bundled `wheels` folder.
- Runs an import smoke test.
- Adds outbound Windows Firewall rules for HSMS and Linkstuffs MQTT.
- Opens `production.yaml` in Notepad.

Expected successful output includes:

```text
Installation complete.
```

If Windows SmartScreen blocks the script:

1. Stop unless the ZIP SHA-256 was already matched against the trusted release
   record or the package has a valid approved code signature.
2. After successful verification, click **More info** and **Run anyway**.

If PowerShell says the verified script is blocked, run
`Unblock-File .\install.ps1`; the installer then verifies its internal release
manifest before launching any bundled executable.

---

## 7. Configure the Middleware

Open the config:

```powershell
notepad C:\SECSGEM_EAP\app\config\production.yaml
```

### Configure Linkstuffs HTTPS

For Cloudflare or normal HTTPS deployments, keep MQTT disabled and HTTPS enabled:

```yaml
linkstuffs:
  enabled: false

linkstuffs_http:
  enabled: true
  base_url: "https://astar-monitoring.linkstuffs.com"
  device_tokens:
    SPTS_fxP_OMEGA_01:      ""
    DAVINCI200_MC4_HC1_01:  "${LINKSTUFFS_HTTP_DAVINCI_TOKEN}"
    PTIQ_01:                ""
  verify_tls: true
```

Rules:

- The key under `device_tokens` must exactly match the machine `display_name`.
- Every enabled HTTPS machine must resolve a non-empty token or validation
  fails. Keep machines without a token at `enabled: false`.
- A `401 Unauthorized` during testing usually means the token is wrong.

### Configure Machines

Example DaVinci machine:

```yaml
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
    request_online: false
    drain_spool_on_connect: false
    local_csv_path: "D:/MachineData/EAP_DAVINCI200_MC4_HC1_01/csv_in"
    network_csv_path: "\\\\TD-DATASVR-F2C4\\TD_DAVINCI200_MC4_HC1_01.csv_in"
    admin_config_path: "C:/SECSGEM_EAP/machines/DAVINCI200_MC4_HC1_01/config"
```

Set unused tools to:

```yaml
enabled: false
```

Save the file.

---

## 8. Deployment Preparation Checks

Run these before the first live deployment.

### Check Python

```powershell
python --version
```

The version must exactly match the package's `PYTHON_VERSION.txt`.

### Check Tool Network Reachability

```powershell
ping <assigned equipment IP>
```

If ping is disabled by policy, use PowerShell TCP testing:

```powershell
Test-NetConnection 10.10.20.32 -Port 5000
```

### Check `D:\` CSV Drive

```powershell
Test-Path D:\
```

Expected:

```text
True
```

If `D:\` does not exist, either create/provision the drive or change `local_csv_path` in `production.yaml`.

### Check Network CSV Share

If `network_csv_path` is enabled:

```powershell
Test-Path "\\FILESERVER\EAP_DAVINCI200_MC4_HC1_01.csv_in"
```

If the share is unavailable, the middleware still keeps the local CSV, but the network mirror will fail until the share is fixed.

### Confirm Tool-Side Settings

With the tool owner/vendor, confirm:

- Tool HSMS is passive if middleware `hsms_mode` is `active`.
- Tool IP, port, and SECS Device ID are correct.
- No other host is connected to the tool's HSMS port.
- For DaVinci/FabLink, event reporting style is acceptable. E30/S6F11 gives full collection-event detail. E40 notifications are ingested but are coarser.
- `enable_alarms: true` is used only if the tool owner confirms the tool accepts host-side S5F3 alarm enable.

---

## 9. Validate the Configuration

Open a new PowerShell window:

```powershell
cd C:\SECSGEM_EAP\app
python -m eap_middleware validate-config --config config\production.yaml
```

Expected result includes:

```text
"valid": true
```

Fix all validation errors before continuing.

---

## 10. Test Machine Connectivity

Test one machine:

```powershell
cd C:\SECSGEM_EAP\app
python -m eap_middleware test-machine --config config\production.yaml --endpoint-id TOOL_02
```

Test all enabled machines:

```powershell
python -m eap_middleware test-machine --config config\production.yaml --endpoint-id ALL
```

Common results:

| Error | Meaning | Fix |
|---|---|---|
| `Connection refused` | Wrong IP/port, tool HSMS is off, or another host is connected. | Confirm tool HSMS service and disconnect other host. |
| `Connection timeout` | Network path blocked. | Check VLAN, firewall, route, and port. |
| Device ID mismatch | `secs_device_id` does not match tool. | Correct `secs_device_id` in `production.yaml`. |

---

## 11. Test Linkstuffs / ThingsBoard

```powershell
cd C:\SECSGEM_EAP\app
python -m eap_middleware test-linkstuffs --config config\production.yaml
```

If this fails:

- Confirm `linkstuffs_http.enabled: true`.
- Confirm `base_url`.
- Confirm the token for the exact `display_name`.
- Confirm outbound HTTPS port `443` is allowed.

---

## 12. First Live Run Before Service Deployment

Run the middleware interactively first:

```powershell
cd C:\SECSGEM_EAP\app
python -m eap_middleware run-service --config config\production.yaml
```

Watch for:

- Tool connection established.
- No repeated reconnect loop.
- Linkstuffs publish success.
- CSV files created under `D:\MachineData\EAP_<display_name>\csv_in`.

Trigger or wait for a real lot event, then confirm:

```powershell
Get-ChildItem D:\MachineData\EAP_DAVINCI200_MC4_HC1_01\csv_in
```

Stop the interactive run with:

```text
Ctrl+C
```

---

## 13. Install as a Windows Service

NSSM is recommended for production because it auto-starts on boot and captures logs cleanly.

### Install NSSM

Copy `nssm.exe` to:

```text
C:\Tools\nssm\nssm.exe
```

Make sure `nssm.exe` is on PATH or pass its full path to the service installer.

### Register the Service

Open **PowerShell as Administrator**:

```powershell
cd C:\SECSGEM_EAP\app
scripts\install_service.ps1 -NssmExe "C:\Tools\nssm\nssm.exe"
```

If Python is not auto-detected:

```powershell
scripts\install_service.ps1 `
  -PythonExe "C:\Python311\python.exe" `
  -NssmExe "C:\Tools\nssm\nssm.exe"
```

Start the service:

```powershell
Start-Service AstarSecsGemEapMiddleware
```

Check status:

```powershell
Get-Service AstarSecsGemEapMiddleware
```

Expected:

```text
Status   Name
------   ----
Running  AstarSecsGemEapMiddleware
```

---

## 14. Verify the Full Deployment

Watch logs:

```powershell
Get-Content C:\SECSGEM_EAP\logs\service_stderr.log -Tail 50 -Wait
```

Check service stdout if needed:

```powershell
Get-Content C:\SECSGEM_EAP\logs\service_stdout.log -Tail 50 -Wait
```

Confirm:

- Service is running.
- Each enabled tool connects.
- No repeated validation/config errors.
- Telemetry appears in Linkstuffs.
- CSV appears in `D:\MachineData\EAP_<display_name>\csv_in`.
- If configured, CSV also appears in the network share path.

---

## 15. Normal Operations Commands

```powershell
Start-Service AstarSecsGemEapMiddleware
Stop-Service AstarSecsGemEapMiddleware
Restart-Service AstarSecsGemEapMiddleware
Get-Service AstarSecsGemEapMiddleware
```

Validate after config edits:

```powershell
cd C:\SECSGEM_EAP\app
python -m eap_middleware validate-config --config config\production.yaml
```

Test all machines:

```powershell
python -m eap_middleware test-machine --config config\production.yaml --endpoint-id ALL
```

Test upstream:

```powershell
python -m eap_middleware test-linkstuffs --config config\production.yaml
```

---

## 16. Updating an Existing Deployment

1. Build a fresh ZIP on the Mac:

```bash
cd /Users/nrzngr/Desktop/astar-middleware-main
./scripts/build_deploy_package.sh
```

2. Copy the new ZIP to the Windows server.
3. Extract it.
4. Stop the service:

```powershell
Stop-Service AstarSecsGemEapMiddleware
```

5. Run installer as Administrator:

```powershell
cd C:\Users\<windows-user>\Downloads\astar-middleware-deploy
.\install.ps1
```

The installer preserves the existing live config:

```text
C:\SECSGEM_EAP\app\config\production.yaml
```

It writes the new template as:

```text
C:\SECSGEM_EAP\app\config\production.yaml.new
```

and saves a backup snapshot as:

```text
C:\SECSGEM_EAP\app\config\production.yaml.bak
```

6. Compare and merge any needed new config fields:

```powershell
notepad C:\SECSGEM_EAP\app\config\production.yaml
notepad C:\SECSGEM_EAP\app\config\production.yaml.new
```

7. Validate:

```powershell
cd C:\SECSGEM_EAP\app
python -m eap_middleware validate-config --config config\production.yaml
```

8. Start the service:

```powershell
Start-Service AstarSecsGemEapMiddleware
```

9. Watch logs:

```powershell
Get-Content C:\SECSGEM_EAP\logs\service_stderr.log -Tail 50 -Wait
```

---

## 17. Rollback

If the previous deployment worked and the new deployment fails:

1. Stop the service:

```powershell
Stop-Service AstarSecsGemEapMiddleware
```

2. Restore the previous config if needed:

```powershell
Copy-Item C:\SECSGEM_EAP\app\config\production.yaml.bak C:\SECSGEM_EAP\app\config\production.yaml -Force
```

3. Reinstall from the previous known-good ZIP if code rollback is required.
4. Validate:

```powershell
cd C:\SECSGEM_EAP\app
python -m eap_middleware validate-config --config config\production.yaml
```

5. Start service:

```powershell
Start-Service AstarSecsGemEapMiddleware
```

---

## 18. Troubleshooting

### Installer Cannot Run

Allow the reviewed installer for the current PowerShell session:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process -Force
```

If the file is blocked:

```powershell
Unblock-File .\install.ps1
```

### Python Version Error

Check:

```powershell
python --version
type .\PYTHON_VERSION.txt
```

The installed Python version must match the package's required version.

### Machine Test Fails

Check:

```powershell
Test-NetConnection <tool-ip> -Port <tool-port>
```

Then confirm:

- Tool HSMS service is running.
- No other host is connected.
- `secs_device_id` matches the tool.
- Firewall/VLAN routing allows the connection.

### Linkstuffs Test Fails

Check:

- `linkstuffs_http.enabled` is `true`.
- `base_url` is correct.
- Token matches the exact `display_name`.
- Server can access HTTPS port `443`.

### Service Starts Then Stops

Read logs:

```powershell
Get-Content C:\SECSGEM_EAP\logs\service_stderr.log -Tail 100
Get-Content C:\SECSGEM_EAP\logs\service_stdout.log -Tail 100
```

Also check Windows Event Viewer:

```text
Start menu -> Event Viewer -> Windows Logs -> Application
```

### No CSV Output

Confirm:

- A real lot/tool event occurred.
- The machine is connected.
- `D:\` exists.
- `local_csv_path` is correct.
- The Windows service account has write permission.

### No Telemetry in Linkstuffs

Confirm:

- `test-linkstuffs` passes.
- The token is for the correct device.
- The device name in Linkstuffs matches `display_name`.
- The logs show publish attempts and no HTTP errors.

---

## 19. Final Deployment Checklist

- [ ] ZIP built on Mac.
- [ ] Remote Desktop access works from Mac to Windows server.
- [ ] ZIP copied to Windows server.
- [ ] ZIP extracted.
- [ ] `install.ps1` completed successfully.
- [ ] `production.yaml` edited.
- [ ] `validate-config` returns valid.
- [ ] `test-machine` passes for each enabled tool.
- [ ] `test-linkstuffs` passes.
- [ ] Interactive `run-service` works.
- [ ] Windows service installed with NSSM.
- [ ] Service is running.
- [ ] Logs show successful tool connection.
- [ ] CSV output confirmed.
- [ ] Linkstuffs telemetry confirmed.
