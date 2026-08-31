# DaVinci 200 to ASTAR Middleware - HSMS/SECS-GEM Setup and Recovery Guide

This guide is for the MueTec DaVinci 200 running ToolCommander 4.0.110 and FabLink suite 6.5.0. Follow the sections in order. Do not start the ASTAR connection steps until the DaVinci has a local TCP listener on port 5000.

A firewall can block access to an existing listener. It cannot create a listener or repair a failed FabLink startup.

## Step 0 - Confirm the two TCP connections

Do not mix up ports 3000 and 5000.

| Connection | Passive/listening side | Active/connecting side | Normal address/port | Purpose |
|---|---|---|---|---|
| Internal equipment link | FabLink Tool Conn | ToolCommander | `127.0.0.1:3000` | ToolCommander exchanges equipment state with FabLink. |
| External HSMS-SS link | DaVinci FabLink Host Conn | ASTAR middleware | DaVinci FabNet IP, shown below as `192.0.2.32:5000` | HSMS, SECS-II, and GEM communication. |

The Software Operation Manual says the `[HostInterface]` values `TLKAddress=127.0.0.1` and `TLKPort=3000` are for ToolCommander-to-FabLink communication and should not be changed (PDF pp. 55-56). Never point ASTAR to port 3000.

## Pass conditions before starting ASTAR

All four conditions must be true:

1. ToolCommander Host Interface is enabled and saved as `Server`, port `5000`.
2. FabLink Status is green, or at minimum its Host Conn page reports passive/server operation without an error state.
3. Windows on the DaVinci shows port `5000` as `LISTENING`.
4. The Tool Conn internal link is connected and the equipment link can synchronize.

If condition 2 or 3 is false, remain on the DaVinci recovery steps. If condition 4 is false, HSMS TCP may eventually open, but GEM/E30 state cannot become healthy until the local ToolCommander-to-FabLink link is restored.

## Part 1 - Put the tool in a safe state

1. Finish or stop active production according to the site's operating procedure. Confirm no wafer is moving and no job is active.
2. If the tool is Online Remote, press the GEM/Equipment Remote header control and choose `Offline`. The Maintenance Manual explicitly requires leaving remote operation before maintenance (PDF p. 16).
3. Do not initialize, reboot, or power-cycle a running machine merely to open the HSMS port.
4. Log in with a user that has both `Change Parameters` and `Control Fab Hostinterface`. These rights are documented in the Software Operation Manual (PDF p. 40).

## Part 2 - Configure and verify the FabNet IPv4 connection

The addresses `192.0.2.0/24` below are documentation-only TEST-NET examples and
will not route to real equipment. Replace them with addresses approved for the
site FabNet before following the commands. Use these values only when they match
the approved site network plan:

- DaVinci FabNet IPv4 address: `192.0.2.32`
- subnet mask: `255.255.255.0`
- ASTAR PC example address: `192.0.2.2`

For a `/24` network, `192.0.2.0` is the example network address and must not be used as a default gateway. If the DaVinci and ASTAR PC are directly connected or on the same Layer-2 subnet, leave the FabNet default gateway blank. If routing is required, enter the actual router address provided by site IT.

On the DaVinci, open an elevated PowerShell window and record the interface state:

```powershell
Get-NetIPConfiguration -InterfaceAlias "FabNet"
Get-NetIPAddress -InterfaceAlias "FabNet" -AddressFamily IPv4
```

Replace `192.0.2.32` with the actual FabNet address assigned to the DaVinci adapter. The Host Conn page may list several local addresses because the control PC has several adapters. Do not use the Wi-Fi, loopback, tool-control, or another machine-internal address merely because it appears in that list.

Set the ASTAR PC to another unused address in the same subnet (the examples use `192.0.2.2/24`). Then test both directions if site policy permits ICMP:

```powershell
# Run on the DaVinci
ping 192.0.2.2

# Run on the ASTAR PC
ping 192.0.2.32
```

Ping is only a network-path check. It does not prove that HSMS is running.

## Part 3 - Configure ToolCommander as HSMS passive/server

1. In ToolCommander, open `Components`.
2. Select `Host Interface (Global)`.
3. Open `Parameters`.
4. Confirm `Enable` is checked.
5. Set `Communication Mode` to `Server`.
6. Leave `Host Address` alone. It is not required in Server mode. `localhost` in this field is not the address ASTAR should use.
7. Set `TCP/IP Port` to `5000`.
8. Set or confirm these HSMS timers:

   | Timer | Value |
   |---|---:|
   | T3 | 45 s |
   | T5 | 10 s |
   | T6 | 5 s |
   | T7 | 10 s |
   | T8 | 5 s |

9. Leave `Soft Start Timeout` at the machine-approved value. This is not an HSMS timer, and changing it does not repair a missing listener.
10. Press `Save`, then `Close`.
11. Reopen `Parameters` and verify that `Enable`, `Server`, and `5000` persisted.

The Software Operation Manual states that activation starts the Host Interface application on the control PC and that TC Control shutdown stops it (PDF p. 126). Its parameter descriptions confirm that Server mode needs no host address (PDF pp. 127-128). The Host Interface Manual defines the equipment default as PASSIVE, port 5000, Device ID 0, with the timer values above (PDF pp. 24-25).

## Part 4 - Prove the effective FabLink mode

Open the separate FabLink `Host Interface - MueTec` status window.

1. Select `Host Conn.`.
2. Confirm the displayed connection mode is `Server (passive)`.
3. Confirm `Own Port` is `5000`.
4. It is normal to see more than one local address under `Own Addresses`; the listener must still bind to port 5000.
5. If it shows `Client (active)` and a Remote Address/Remote Port, the running configuration is wrong for the intended topology even if ToolCommander still displays Server.
6. Do not use the gray status radio buttons as the primary configuration control. Correct the values in ToolCommander `Host Interface (Global)`, save, and reload the Host Interface application as described below.

List every process currently using local or remote port 5000:

```powershell
$port5000 = Get-NetTCPConnection -ErrorAction SilentlyContinue |
  Where-Object { $_.LocalPort -eq 5000 -or $_.RemotePort -eq 5000 }

$port5000 |
  Format-Table LocalAddress, LocalPort, RemoteAddress, RemotePort, State, OwningProcess

$port5000.OwningProcess |
  Sort-Object -Unique |
  ForEach-Object {
    Get-CimInstance Win32_Process -Filter "ProcessId=$_" |
      Select-Object ProcessId, Name, ExecutablePath, CommandLine
  }
```

Interpretation:

- A connection with an ephemeral local port and `RemotePort 5000` is an outbound client connection. It is not a listener.
- If FabLink owns an outbound connection to remote port 5000, FabLink is operating as active/client. Restore Server mode and reload the Host Interface application.
- If another program owns the connection, record its name and stop it only with the equipment owner's approval. It may be a vendor test client or another host session.

## Part 5 - Reload FabLink safely when it stays red

Try these actions in order. Stop as soon as FabLink becomes healthy and port 5000 listens.

### 5A. Wait for the configured soft start

1. Close the parameter dialog after saving.
2. Wait at least the full Soft Start Timeout displayed in the parameters.
3. Reopen the FabLink Status and Host Conn pages.
4. Check the ToolCommander `Messages` screen for a Host Interface error. Read and record the full message before acknowledging it.

### 5B. Toggle only the Host Interface component

Do this only while the tool is idle and Offline:

1. Open `Host Interface (Global) -> Parameters`.
2. Clear `Enable`, press `Save`, and close the dialog.
3. Wait until the host interface reports disabled/stopped.
4. Reopen the parameters, check `Enable`, select `Server`, enter `5000`, and press `Save`.
5. Wait through the Soft Start Timeout again.

### 5C. Controlled ToolCommander/TC Control restart

If FabLink remains red, perform a controlled control-application restart only during approved equipment downtime:

1. Confirm the machine is idle, empty, and Offline.
2. Save screenshots of FabLink Status, Host Conn, and Tool Conn.
3. Exit ToolCommander using its normal `Exit program` function and select the option to shut down the Control application.
4. Wait for ToolCommander and the Host Interface application to close.
5. Start ToolCommander/DaVinci from the normal desktop shortcut.
6. Log in, wait for the configured soft start, and recheck the saved Host Interface parameters.

Do not kill FabLink from Task Manager or power-cycle the machine as the first recovery method. The manuals only document the managed application lifecycle.

## Part 6 - Verify the local listeners

Run on the DaVinci:

```powershell
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object { $_.LocalPort -in 3000,5000 } |
  Sort-Object LocalPort |
  Format-Table LocalAddress, LocalPort, State, OwningProcess

netstat -ano | findstr /R /C:":3000" /C:":5000"
```

Expected results:

- Port 3000: a local listener used by the internal ToolCommander/FabLink link.
- Port 5000: a line such as `0.0.0.0:5000 LISTENING`, `[::]:5000 LISTENING`, or `192.0.2.32:5000 LISTENING`.

For each listener PID, identify its process:

```powershell
$listenerPids = Get-NetTCPConnection -State Listen |
  Where-Object { $_.LocalPort -in 3000,5000 } |
  Select-Object -ExpandProperty OwningProcess -Unique

Get-Process -Id $listenerPids |
  Select-Object Id, ProcessName, Path
```

### If port 5000 still does not listen

Do not change ASTAR yet. Check the following in this order:

1. FabLink Host Conn still displays passive/server and own port 5000.
2. No other process already owns local port 5000:

   ```powershell
   Get-NetTCPConnection -LocalPort 5000 -ErrorAction SilentlyContinue |
     Format-Table LocalAddress, LocalPort, RemoteAddress, RemotePort, State, OwningProcess
   ```

3. Tool Conn is connected. If it is not, confirm the internal values remain `127.0.0.1:3000`. Do not replace them with a FabNet address.
4. Review the effective configuration read-only. Back up the files before any vendor-approved edit:

   ```cmd
   findstr /n /i "mode address port device" C:\AIS\Fablink\HostConnection.ini
   findstr /n /i "address port" C:\AIS\Fablink\EquipmentConnection.ini
   findstr /n /i "TLKAddress TLKPort" C:\AIS\ToolCommander\MueTecApplication.ini
   ```

5. Collect the newest logs. The documented ToolCommander troubleshooting logs are under `C:\AIS\ToolCommander\VCTC_InternalLogging\<year>\<month>` (Software Operation Manual, PDF p. 36). Locate FabLink logs without assuming an undocumented fixed folder:

   ```powershell
   Get-ChildItem C:\AIS\Fablink -Recurse -File -ErrorAction SilentlyContinue |
     Where-Object { $_.Extension -in '.log','.txt' } |
     Sort-Object LastWriteTime -Descending |
     Select-Object -First 30 FullName, LastWriteTime, Length
   ```

6. Search the newest logs for `bind`, `listen`, `socket`, `5000`, `configuration`, `license`, `exception`, and `error`.

If FabLink is still red after a managed restart, the saved values are correct, port 5000 is free, and the logs show configuration or license errors, stop and contact MueTec/Kontron AIS. Do not invent INI values. Provide the vendor bundle listed near the end of this guide.

## Part 7 - Test the network and firewall only after LISTENING appears

On the DaVinci itself:

```powershell
$DaVinciFabNetIp = "<assigned DaVinci FabNet IP>"
Test-NetConnection $DaVinciFabNetIp -Port 5000
Test-NetConnection 127.0.0.1 -Port 5000  # optional binding diagnostic
```

The FabNet probe must report `TcpTestSucceeded : True`. The loopback probe is
expected to pass only when FabLink listens on all interfaces or explicitly on
loopback; it may correctly fail when FabLink is bound only to the FabNet IP.

From the ASTAR PC:

```powershell
$DaVinciFabNetIp = "<assigned DaVinci FabNet IP>"
Test-NetConnection $DaVinciFabNetIp -Port 5000
```

If the local DaVinci test passes but the ASTAR test fails, then investigate routing, VLANs, cable/switch state, and Windows Firewall. If site policy permits an inbound rule, an administrator can use:

```powershell
Get-NetConnectionProfile -InterfaceAlias "FabNet"
$AstarFabNetIp = "<assigned ASTAR FabNet IP>"
New-NetFirewallRule -DisplayName "DaVinci FabLink HSMS 5000" `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5000 `
  -RemoteAddress $AstarFabNetIp -Profile Private
```

Replace `Private` with the active, site-approved FabNet firewall profile shown
by `Get-NetConnectionProfile`. Restrict `-RemoteAddress` to the assigned ASTAR
IP or approved FabNet subnet. Do not add a broad exception when there is no
local listener. Remove the temporary rule after testing unless site security
approves it permanently:

```powershell
Remove-NetFirewallRule -DisplayName "DaVinci FabLink HSMS 5000"
```

## Part 8 - Configure ASTAR as HSMS active/client

The DaVinci is passive/server, so ASTAR must be active/client. In `config/production.yaml`, use:

```yaml
machines:
  - endpoint_id: "TOOL_02"
    display_name: "DAVINCI200_MC4_HC1_01"
    machine_profile: "davinci_200_mc4_hc1"
    host: "192.0.2.32"
    port: 5000
    secs_device_id: 0
    hsms_mode: "active"
    enabled: true
```

Rules:

- `host` is the DaVinci FabNet IPv4 address, not `localhost`.
- `hsms_mode: active` means ASTAR opens the TCP connection to the passive DaVinci.
- `secs_device_id` must match the DaVinci HSMS Device ID. The documented default is `0`; confirm the effective value before connecting.
- Only one external HSMS host should connect during the test. Stop old MES, vendor test host, or duplicate ASTAR instances with the system owner's approval.

Start or restart ASTAR only after the remote TCP test passes.

## Part 9 - Verify HSMS, GEM communication, and control state in order

Use this sequence. Do not skip ahead:

1. TCP socket connects.
2. HSMS changes from `NOT CONNECTED` to `CONNECTED / NOT SELECTED`.
3. HSMS Select completes and the state becomes `SELECTED`.
4. GEM establishes communications using S1F13/S1F14 and the communication state becomes `Communicating`.
5. In ToolCommander Host Interface `Operation`, confirm:
   - FabLink connection indicator is green.
   - HSMS state says connected/selected.
   - Communications state says communicating.
6. Choose `Online Local` for the first integration test. Do not choose Online Remote yet.
7. Confirm the top GEM state displays Online Local in yellow.
8. Run `Are You There Request` only after HSMS is selected; it checks the S1F1/S1F2 round trip.

The Host Interface Manual defines NOT CONNECTED, CONNECTED/NOT SELECTED, and SELECTED in PDF pp. 25-27. The Software Operation Manual documents the FabLink, HSMS, communication, control, processing, and spooling status fields in PDF p. 129 and the Offline/Online Local/Online Remote buttons in PDF p. 130.

`Online Local` does not create the TCP listener. It is a GEM control state selected after transport and communication are healthy.

## Recovery decision table

| Observed state | Meaning | Next action |
|---|---|---|
| FabLink red, Host Conn `-1`, no `LISTENING` on 5000 | FabLink startup/configuration/bind failure on the DaVinci | Stay on Parts 4-6. Do not troubleshoot ASTAR. |
| Host Conn shows Client/active and outbound connections to remote port 5000 | Wrong effective direction for this design | Restore ToolCommander Server mode and reload FabLink. |
| Tool Conn says Not Connected on port 3000 | Internal ToolCommander-to-FabLink link is down | Verify `127.0.0.1:3000`, logs, and managed application restart. Do not use port 3000 externally. |
| Port 5000 listens locally but ASTAR TCP test fails | Network path or firewall | Check FabNet IP, subnet, route, switch/VLAN, then firewall. |
| TCP connects but HSMS never selects | HSMS Select control exchange fails, another session interferes, or T6/T7 expires | Confirm DaVinci passive, ASTAR active, one host, and inspect Select.req/Select.rsp logs. |
| HSMS selected but E30 communication remains disabled/not communicating | GEM establish-communication exchange has not completed; Device ID or SECS message handling may be wrong | Confirm the effective Device ID (default: 0) and inspect ASTAR logs for S1F13/S1F14, S9 errors, and timer/protocol errors. |
| Communicating but GEM is Offline | Transport is good; control state is still Offline | Select Online Local. |
| GEM300 red or objects not initialized | Separate GEM300 managers are not ready | Do not use this as the first test for basic E30/HSMS. Resolve after FabLink, Tool IF, HSMS, and GEM communication are healthy. |
| EDA red | EDA is a separate interface from HSMS/SECS-GEM | It does not prove that TCP port 5000 should or should not listen. |
| Bus Logic gray | Module is not active/configured | Not a primary port-5000 diagnostic. |

## Do not do these during recovery

- Do not configure both DaVinci and ASTAR as passive/server.
- Do not configure both sides as active/client.
- Do not point ASTAR to `localhost` unless ASTAR actually runs on the DaVinci control PC.
- Do not use port 3000 for ASTAR.
- Do not edit `TLKAddress` or `TLKPort`; the manual says those internal values should not be changed.
- Do not treat outbound `ESTABLISHED` lines with remote port 5000 as proof of a local listener.
- Do not change firewall rules before proving the local listener exists.
- Do not set the `/24` network address (for example, `192.0.2.0`) as the gateway.
- Do not press Online Remote during first connection testing.
- Do not initialize, reboot, kill control processes, or power-cycle the tool while a wafer/job is active.

## Vendor escalation bundle

If Parts 4-6 do not produce a listener, collect these items before contacting MueTec/Kontron AIS:

1. Screenshot of ToolCommander Host Interface Parameters after reopening it.
2. Screenshots of FabLink Status, Host Conn, and Tool Conn.
3. Output of:

   ```powershell
   Get-NetIPConfiguration
   Get-NetTCPConnection | Where-Object { $_.LocalPort -in 3000,5000 -or $_.RemotePort -in 3000,5000 }
   Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'Fab|Host|Tool' } |
     Select-Object ProcessId, Name, ExecutablePath, CommandLine
   ```

4. Read-only copies of `FabLink.ini`, `HostConnection.ini`, `EquipmentConnection.ini`, `Interface.ini`, `MachineInterface.ini`, and `GEM300.ini` from the actual `C:\AIS\Fablink` installation.
5. Newest FabLink logs and the documented ToolCommander `VCTC_InternalLogging` folder.
6. Exact time of the failed start and the full ToolCommander message text.
7. Software versions: ToolCommander 4.0.110 and FabLink suite 6.5.0, unless the Information screen reports otherwise.

Before sharing, make redacted copies that remove credentials, access tokens,
cryptographic keys, usernames, and unrelated sensitive operational data while
retaining the timestamps, IP/port bindings, effective Device ID, process names,
and error text needed for diagnosis. Never send raw INI files, logs, screenshots,
or command output. Transfer only the reviewed redacted bundle through the
site-approved secure support channel.

## Final success checklist

- [ ] Tool is idle and initial testing is in Offline or Online Local, not Online Remote.
- [ ] FabNet has the intended static IPv4 and no invalid `.0` gateway.
- [ ] ToolCommander Host Interface is enabled, Server, port 5000.
- [ ] Reopened parameters still show the saved values.
- [ ] FabLink Host Conn shows passive/server, own port 5000.
- [ ] Tool Conn is connected on the internal 127.0.0.1:3000 link.
- [ ] FabLink Status is green and Equipment Link can synchronize.
- [ ] DaVinci Windows shows local TCP port 5000 as LISTENING.
- [ ] ASTAR PC can reach the assigned DaVinci FabNet IP on TCP port 5000.
- [ ] ASTAR uses the assigned DaVinci FabNet IP, port 5000, effective Device ID (default: 0), HSMS active.
- [ ] No duplicate MES/test host/ASTAR instance holds the session.
- [ ] HSMS is connected and selected.
- [ ] GEM communication is communicating.
- [ ] Online Local is yellow.
- [ ] S1F1/S1F2 Are You There succeeds.
