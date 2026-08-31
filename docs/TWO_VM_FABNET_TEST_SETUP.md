# Local Production-Like Test Rig: 2x Windows 11 VM on VMware Fusion (Apple Silicon)

This guide builds a local test topology that mirrors the real field setup:
two separate Windows 11 PCs on the same switch — one acting as the DaVinci
tool control PC, one acting as the ASTAR middleware server — using only
VMware Fusion on an Apple Silicon Mac. No physical hardware or second Mac
required.

Every step below was executed and verified end-to-end on 2026-08-11; the
`secs-ok` output in [section 10](#10-verify-the-hsms-path) is real output from
this rig, not an example.

Each VM gets **two virtual NICs**, matching how the real DaVinci control PC
is wired (see `docs/DAVINCI_SECS_GEM_SETUP.md`, "several local adapters... do not
use the Wi-Fi, loopback, tool-control, or another machine-internal address"):

| NIC | VMware Fusion network mode | UTM equivalent | Purpose |
|---|---|---|---|
| NIC 1 | **Share with my Mac** | Shared Network (NAT) | Internet access for Windows Update, installers |
| NIC 2 | **Private to my Mac** | Host Only | The isolated "FabNet switch" the two VMs talk over |

Fusion's **Private to my Mac** mode automatically joins every VM configured
with it onto the same private virtual subnet — that private subnet *is* your
virtual switch. You don't create a separate switch object. Fusion's own
description of the mode confirms it: "Multiple virtual machines can be
connected to the same private network."

---

## 0. Prerequisites

- VMware Fusion (13 or newer) installed.
- ~25 GB free disk per VM (2 VMs ≈ 50 GB minimum; 100 GB+ recommended).
- 8 GB+ RAM to spare if you'll run both VMs at once (4 GB each is the
  practical floor for Windows 11).
- A Windows 11 **ARM64** ISO — your Mac is `arm64`, so x64 Windows will not
  boot. Get it from Microsoft's official download page
  (`microsoft.com/software-download/windows11`), selecting the
  ARM64/multi-edition ISO option.
- This repo checked out on the Mac, with `./scripts/build_deploy_package.sh`
  runnable (needs `python3`, `zip`/`unzip` — already on macOS).

> **Architecture note.** The guests are Windows-on-ARM, but the deploy package
> bundles **x64** wheels (`*-win_amd64.whl`) and an **x64** Python installer
> (`python-3.11.9-amd64.exe`). This is deliberate and it works: Windows 11 ARM
> runs x64 under emulation. Do **not** install ARM64 Python in the guests — pip
> would then reject every bundled wheel with "not a supported wheel on this
> platform". Let `install.ps1` install the bundled x64 Python itself.

---

## 1. Create the two VMs

Create both VMs the normal Fusion way (**File → New → Install from disc or
image**, point at the Windows 11 ARM64 ISO). Allocate at least 4 GB RAM,
4 CPU cores, 64 GB disk each.

Name one `ASTAR-SERVER` and one `DAVINCI-PC`.

> **The Fusion VM name, the Windows hostname, and the login-screen name are
> three different things, and on this rig they disagree.** The name on the
> Windows login screen is the *user account*, not the machine. Always identify
> a VM by running `hostname` inside it. On the reference rig, the VM whose
> Fusion display name is `ASTAR-SERVER` reports hostname `DESKTOP-F1UBSEM`.

For **each** VM, before or after installing Windows:

1. Select the VM → **Virtual Machine → Settings**.
2. Click **Network Adapter** → select **Share with my Mac**. This is NIC 1
   (WAN).
3. Back in Settings, click **Add Device… → Network Adapter** to add NIC 2.
4. Click the new **Network Adapter 2** → under **Custom**, select
   **Private to my Mac**. This is NIC 2 (FabNet).
5. Confirm **Connect Network Adapter** is checked on both.

Both VMs' NIC 2 now land on the same private subnet automatically — that's
your virtual switch.

---

## 2. Install Windows 11 on both VMs

Boot each VM and run through Windows Setup:

1. Language/region → Next.
2. At **"Let's connect you to a network"** (OOBE tries to force a Microsoft
   account + internet): press **Shift+F10** to open a command prompt, then
   run:
   ```cmd
   oobe\bypassnro
   ```
   The VM reboots back into setup with a **"I don't have internet"** option
   now visible — use it, then choose **Limited setup** to get a local
   account. This avoids tying a throwaway test VM to a Microsoft account.
3. Set a local account (a simple password — this machine never leaves your
   Mac).
4. Skip the optional Microsoft apps/privacy prompts.
5. Install VMware Tools when prompted (**Virtual Machine → Install VMware
   Tools**), then reboot. Tools gives you clipboard sharing and drag-and-drop,
   which you will want in section 5.

---

## 3. Identify and rename the adapters

In each VM, open PowerShell **as Administrator** and run:

```powershell
Get-NetIPConfiguration
```

You'll see two adapters. **Identify the FabNet adapter by its missing default
gateway, not by its name or order** — the order is not consistent between VMs.
On the reference rig the adapter numbering was reversed between the two VMs.

- **FabNet** = the adapter with a blank `IPv4DefaultGateway` and
  `NetProfile.Name: Unidentified network`.
- **WAN** = the adapter that has a real `IPv4DefaultGateway`.

Rename them (substitute the actual current names — note `Rename-NetAdapter`
takes `-Name` directly and must **not** be piped from `Get-NetAdapter`):

```powershell
Rename-NetAdapter -Name "Ethernet 2" -NewName "FabNet"
Rename-NetAdapter -Name "Ethernet"   -NewName "WAN"
```

This directly avoids the exact mistake `docs/DAVINCI_SECS_GEM_SETUP.md` warns
about — picking the wrong adapter's address because a PC has several.

Fusion's DHCP already hands each VM a distinct address on the private subnet
(`192.168.102.128` and `192.168.102.129` on the reference rig), so **static
IPs are optional**. If you want them pinned anyway:

```powershell
New-NetIPAddress -InterfaceAlias "FabNet" -IPAddress 192.168.102.10 -PrefixLength 24
```

No gateway on `FabNet` — same rule as the real FabNet setup: two devices
directly on one L2 segment don't need one.

---

## 4. Open the firewall for FabNet

Fusion's private network shows up in Windows as an *Unidentified network*,
which Windows classifies as **Public** — and the Public profile blocks inbound
ICMP, so ping fails until you fix this. In **both** VMs:

```powershell
Set-NetConnectionProfile -InterfaceAlias "FabNet" -NetworkCategory Private
New-NetFirewallRule -DisplayName "Allow ICMPv4-In FabNet" -Protocol ICMPv4 -IcmpType 8 -Direction Inbound -Action Allow
```

`Set-NetConnectionProfile` sometimes silently fails to stick on an
*Unidentified network* — that's a known Windows limitation, and it doesn't
matter here. The firewall rule above is deliberately written **without**
`-Profile`, so it applies to Public as well and ping works either way.

---

## 5. Verify the virtual switch

Get each VM's FabNet address:

```powershell
(Get-NetIPAddress -InterfaceAlias FabNet -AddressFamily IPv4).IPAddress
```

Then ping **the other VM** from each side:

```powershell
# From DAVINCI-PC (192.168.102.129)
ping 192.168.102.128

# From ASTAR-SERVER (192.168.102.128)
ping 192.168.102.129
```

> **Do not ping the VM's own address.** A self-ping always succeeds and proves
> nothing — this is easy to do by accident and it will make you believe the
> switch works when it doesn't. Two tells that you're looking at a real
> cross-machine ping: the address you pinged is *not* the one
> `Get-NetIPConfiguration` reports for this VM, and the first reply is
> noticeably slower than the rest (ARP resolution — e.g. `19ms` then `1ms`),
> whereas a self-ping is uniformly `<1ms`.

Both directions must report `Lost = 0 (0% loss)` before you touch SECS/GEM at
all.

---

## 6. Get files into the VMs

**Fusion on Apple Silicon has no Sharing pane** — the VirtIO-FS shared-folder
approach that works under UTM is not available here. Use one of:

- **Drag and drop** (needs VMware Tools): Settings → **Isolation** → enable
  **Drag and Drop**, then drag the file from Finder onto the VM window.
- **A local HTTP server on the Mac.** The Mac holds an address on the FabNet
  subnet itself (`192.168.102.1`), so binding there serves the VMs *only* and
  never exposes the files to your Wi-Fi LAN:
  ```bash
  cd <folder with the files>
  python3 -m http.server 8000 --bind 192.168.102.1
  ```
  Then browse to `http://192.168.102.1:8000` inside each VM. Ctrl+C when done.
- **OneDrive**, if the guests are signed in.

> If you extract the package into a OneDrive-backed folder, **copy it to a
> plain local path such as `C:\astar-deploy` before running the installer.**
> `install.ps1` SHA-256-verifies every file in the package, and OneDrive
> online-only placeholders will make that fail or stall.

---

## 7. Install on both VMs

On the Mac, build the package:

```bash
cd /Volumes/Backup/astar-middleware-main
./scripts/build_deploy_package.sh
```

This produces `deploy_out/astar-middleware-deploy-<timestamp>.zip`.

Transfer and extract it on **both** VMs, then in PowerShell **as
Administrator**:

```powershell
Set-ExecutionPolicy RemoteSigned -Scope Process -Force
Set-Location C:\astar-deploy          # wherever you extracted it
Get-ChildItem -Recurse -File | Unblock-File
.\install.ps1
```

`install.ps1` is idempotent and safe to re-run. It verifies the release
manifest, installs the bundled x64 Python 3.11, creates `C:\SECSGEM_EAP\`,
copies the source, pip-installs every dependency **offline** from the bundled
wheels, smoke-tests the imports, and opens `production.yaml` in Notepad.

Run it on **DAVINCI-PC too**. The middleware itself goes unused there — you
are running it as the offline delivery mechanism for Python 3.11 x64,
`secsgem`, and the `simulator/` source, all of which the package already
contains. That is much faster and more reliable than installing Python and
pip-installing from the internet inside the guest.

---

## 8. DAVINCI-PC: run the simulator (acts as the machine)

The deploy package ships `source/simulator/` (the simulator code) but **not**
`packaging/secsgem_simulator/` (the sample configs), so write the config
yourself. In PowerShell on DAVINCI-PC — substitute this VM's own FabNet IP:

```powershell
@'
connection:
  mode: passive
  address: "192.168.102.129"
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
'@ | Set-Content -Encoding UTF8 C:\SECSGEM_EAP\app\davinci-passive.yaml
```

Open the firewall for the HSMS port (5050 — a dedicated test port, not 5000):

```powershell
New-NetFirewallRule -DisplayName "DaVinci Sim HSMS 5050" `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5050
```

Start it:

```powershell
cd C:\SECSGEM_EAP\app
python -m simulator run --config davinci-passive.yaml
```

Confirm the log says it is **listening on the FabNet IP, not `127.0.0.1`**:

```
[INFO] simulator.runner: Starting DaVinci GEM EQUIPMENT in HSMS PASSIVE mode;
       listening on 192.168.102.129:5050; device_id=0
[WARNING] simulator.runner: Waiting for GEM communication at 192.168.102.129:5050
```

The repeating "Waiting for GEM communication" warning is normal — it is the
passive side idling until the host connects. Leave this window running.

---

## 9. ASTAR-SERVER: point the middleware at the simulator

Edit `C:\SECSGEM_EAP\app\config\production.yaml` and change **only** the
`TOOL_02` block's `host` and `port` to the simulator's FabNet address:

```yaml
  - endpoint_id: "TOOL_02"
    display_name: "DAVINCI200_MC4_HC1_01"
    machine_profile: "davinci_200_mc4_hc1"
    host: "192.168.102.129"     # DAVINCI-PC's FabNet IP
    port: 5050
    secs_device_id: 0
    hsms_mode: "active"
    enabled: false              # leave false — see the box below
```

> **Leave `enabled: false`.** `eap_middleware/config.py` rejects any config
> where a machine is enabled but neither `linkstuffs` (MQTT) nor
> `linkstuffs_http` is enabled:
> *"Every enabled machine requires an upstream route."* Since this rig
> deliberately runs with both telemetry routes off, an enabled machine cannot
> pass `validate-config` at all.
>
> This does not block the test. `cmd_test_machine` in `eap_middleware/cli.py`
> matches targets on `endpoint_id` and only consults the `enabled` flag for
> `--endpoint-id ALL`, so an explicit `--endpoint-id TOOL_02` still exercises a
> disabled machine. When you later want `run-service` (which *does* honour the
> flag), [section 11](#11-run-the-full-lifecycle) shows how to satisfy the
> upstream check.

> **Do not edit this file with PowerShell 5.1's `Get-Content` without
> `-Encoding UTF8`.** The header comment contains an em-dash; the default ANSI
> read followed by a UTF-8 write double-encodes it into a C1 control character,
> and the YAML parser then dies with
> `ReaderError: unacceptable character #x009d`. Edit it in Notepad, or pass
> `-Encoding UTF8` explicitly. If you have already corrupted it, restore the
> pristine copy from `<package>\source\config\production.yaml`.

---

## 10. Verify the HSMS path

With the simulator running on DAVINCI-PC, on **ASTAR-SERVER**:

```powershell
cd C:\SECSGEM_EAP\app

Test-NetConnection 192.168.102.129 -Port 5050 -InformationLevel Quiet
python -m eap_middleware validate-config --config config\production.yaml
python -m eap_middleware test-machine --config config\production.yaml --endpoint-id TOOL_02
```

Expected, in order:

1. `Test-NetConnection` → `True`.
2. `validate-config` → prints the config summary with `"valid": true` and your
   `TOOL_02` block showing `"host": "192.168.102.129"`, `"port": 5050`.
3. `test-machine` → TCP connects, HSMS goes `NOT CONNECTED` →
   `CONNECTED / NOT SELECTED` → `SELECTED`, S1F1 is answered by S1F2, and you
   get:

```
secs-ok: TOOL_02 192.168.102.129:5050 device_id=0 identity=['DaVinci200', 'DaVinci200 Version 4.9.3']
```

That line is the whole point of the rig: it proves the network path, the HSMS
handshake, and GEM identity exchange all work before you touch real hardware.

On the simulator side you will then see a `ConnectionResetError: [WinError
10054]` followed by "communication loss". **This is expected** — `test-machine`
disconnects as soon as it has the identity, and the simulator logs the drop and
resumes listening.

Triage order if it fails — the same order the real setup guide uses, which is
the entire point of building this rig:

- Step 1 fails → it's the `FabNet` adapters or the firewall. Go back to
  sections 4–5.
- Step 1 passes but step 3 doesn't → it's an HSMS-level config problem (device
  ID, active/passive mismatch), not the network.

---

## 11. Run the full lifecycle

Section 10 proves the network and the handshake. To also exercise the **event
lifecycle and the per-lot CSV**, you need `run-service` — and `run-service` only
processes machines with `enabled: true`, which per the box in section 9 requires
an upstream route. Three edits to `production.yaml` get you there:

```yaml
linkstuffs_http:
  enabled: true                                  # 1) satisfy the upstream check
  base_url: "http://astar-monitoring.linkstuffs.com:8080"  # origin only
  device_tokens:
    DAVINCI200_MC4_HC1_01: "dummy-test-token"    # 2) token for the enabled machine
```
```yaml
  - endpoint_id: "TOOL_02"
    ...
    enabled: true                                # 3) flip it on
    local_csv_path: "C:/SECSGEM_EAP/data/csv_in" # 4) MUST be writable — see below
    network_csv_path: ""                         #    empty disables the mirror
```

> **The template's CSV paths point at `D:` and `\\FILESERVER`, neither of which
> exists on a test VM — and the local one is fatal.** In
> `eap_middleware/csv_store.py`, the `network_dir.mkdir()` for the mirror is
> wrapped in `try/except` (a missing file server is only logged), but
> `local_dir.mkdir()` is **not**. A non-writable `local_csv_path` therefore
> raises straight out of the S6F11 handler and you get
> `[ERROR] gateway.host: [TOOL_02] Error processing S6F11: [WinError 5] Access
> is denied` on every single event, with no CSV ever written. On a stock VM `D:`
> is the DVD drive, so the template value fails exactly this way. Setting
> `network_csv_path: ""` makes `csv_network_dir` return `None`
> (`eap_middleware/models.py`) and skips the mirror entirely.

The dummy token is safe: `LinkstuffsHttpPublisher.queue_event` only enqueues to
a SQLite outbox and never performs network I/O inline, and `csv_writer.append`
is a separate call in `EapService`. Telemetry POSTs to the invalid `base_url`
fail in a background worker and retry from the outbox; the SECS/GEM path and the
CSV output are unaffected.

Validate and run:

```powershell
cd C:\SECSGEM_EAP\app
python -m eap_middleware validate-config --config config\production.yaml
python -m eap_middleware run-service --config config\production.yaml
```

Leave it running for a minute. You should see, live: S6F11 event reports coming
in and being answered with S6F12, periodic S1F3/S1F4 status polls returning real
values (`Recipe_Overlay_v3`), and S5F1 alarm reports answered with S5F2 —

```
[INFO] gateway.host: [TOOL_02] Alarm SET: ID=5010001, Code=0, Text=Aligner: Analog Input Channels in Manual Mode
```

Stop it with Ctrl+C, then check the output:

```powershell
Get-ChildItem C:\SECSGEM_EAP\data\csv_in -File | Select-Object Name,Length,LastWriteTime
```

One CSV per completed lot, roughly one every 12 seconds with the default
`wafer_count: 3` / `event_interval_sec: 0.5`:

```
DAVINCI200_MC4_HC1_01_Lot_20260810_201407_149584_LP1.csv   1843
DAVINCI200_MC4_HC1_01_Lot_20260810_201418_903516_LP1.csv   1537   <- partial, flushed on stop
```

And the contents show the raw SECS/GEM events mapped to tool events:

```csv
Datetime,ToolEvent,EAP_ToolName,LoadPort,Chamber,LotID,WaferID,Recipe,SECSGEM_Raw_Event
2026-08-10 20:14:07.149584,UnMounted,DAVINCI200_MC4_HC1_01,1,NA,,,,MaterialRemoved
2026-08-10 20:14:08.189683,Mounted,DAVINCI200_MC4_HC1_01,1,NA,,,,MaterialReceived
2026-08-10 20:14:08.720378,Loaded,DAVINCI200_MC4_HC1_01,1,NA,,,,CarrierIDRead
2026-08-10 20:14:09.250998,Clamped,DAVINCI200_MC4_HC1_01,1,NA,,,,CarrierClamped
```

That is the complete path — network, HSMS, GEM, event mapping, and file output —
working end to end without any real hardware.

> Remember to set `enabled: false` again if you want `validate-config` to pass
> with the telemetry routes turned back off.

---

## 12. Iterating without reinstalling

- Re-copy just `eap_middleware/` and `config/production.yaml` into
  `C:\SECSGEM_EAP\app` instead of rerunning `install.ps1` each time.
- Snapshot both VMs (**Virtual Machine → Snapshots**, while shut down) once
  they're in a known-good "installed and configured" state, so you can revert
  instantly instead of reinstalling Windows.

---

## Common pitfalls

| Symptom | Cause |
|---|---|
| Ping "succeeds" but nothing else works | You pinged the VM's own address. Check against `Get-NetIPConfiguration`; a real cross-VM ping shows a slower first reply |
| VMs can't ping each other on `FabNet` | Both NIC 2 must be **Private to my Mac**; and the Public-profile firewall blocks ICMP until you add the rule in section 4 |
| `Rename-NetAdapter : The input object cannot be bound…` | You piped `Get-NetAdapter` into it. Call `Rename-NetAdapter -Name … -NewName …` directly |
| No **Sharing** pane in VM Settings | Expected on Fusion/Apple Silicon. Use drag-and-drop or the HTTP server in section 6 |
| `install.ps1` fails SHA-256 verification | The package sits in OneDrive with online-only placeholders. Copy to a plain local path first |
| pip: "not a supported wheel on this platform" | ARM64 Python is installed. The bundled wheels are x64; let `install.ps1` install its bundled x64 Python |
| `CONFIG ERROR: Every enabled machine requires an upstream route` | A machine is `enabled: true` with both telemetry routes off. Leave it `false` and use `test-machine --endpoint-id` (section 9), or enable a route (section 11) |
| `Error processing S6F11: [WinError 5] Access is denied: 'D:\MachineData\...'` on every event, no CSV written | `local_csv_path` still points at the template's `D:` drive, which is the DVD drive on a VM. Unlike the network mirror, a failing local path is fatal — see section 11 |
| `ReaderError: unacceptable character #x009d` in production.yaml | Edited with PowerShell 5.1 `Get-Content` without `-Encoding UTF8`; restore from `<package>\source\config\production.yaml` |
| Firewall rule added but `Test-NetConnection` still fails | Rule's `-Profile` doesn't match the adapter's actual profile — check `Get-NetConnectionProfile`, or omit `-Profile` entirely |
| Simulator listens but only on `127.0.0.1` | `davinci-passive.yaml`'s `connection.address` wasn't changed from the loopback default (README_OPERATOR.md calls this out too) |
| Windows 11 setup demands a Microsoft account, `bypassnro` didn't help | Some builds need `start ms-cxh:localonly` instead — run that at the same OOBE network screen |
