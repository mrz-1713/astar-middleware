===============================================================================
  ASTAR SECS/GEM EAP MIDDLEWARE — Deployment Guide, Windows 11
===============================================================================

WHAT IS IN THIS PACKAGE
------------------------
  install.ps1            — installer (run this first, as Administrator)
  README_DEPLOY.txt      — this file
  SETUP_CHECKLIST.txt    — fill this in before you start
  PYTHON_VERSION.txt     — required Python major.minor for this package
  python\                — matching installer when bundled (conditional)
  wheels\                — Python packages, no internet needed
  source\                — middleware source code


STEP 0  PRE-INSTALL CHECKLIST
------------------------------
Before running the installer, confirm:

  [ ] Windows 11
  [ ] Current PowerShell session allows the reviewed installer:
        Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process -Force
        Unblock-File .\install.ps1
  [ ] The server PC is on the same network as the SECS/GEM equipment
  [ ] You have the following on hand:
        - Linkstuffs device token(s) for each machine (from Linkstuffs admin)
        - IP address, HSMS port, and SECS device ID for each tool


STEP 1  EXTRACT THE ZIP
------------------------
Right-click the ZIP → Extract All → choose any location, e.g.:

    C:\Users\<you>\Downloads\astar-middleware-deploy


STEP 2  RUN THE INSTALLER
--------------------------
Before extraction, compare the ZIP SHA-256 with the expected value from the
trusted release record or approved secure channel. Do not trust only the hash
file delivered beside the ZIP. Stop on a mismatch. Only after verification may
you use SmartScreen "More info" → "Run anyway".

1. Open PowerShell as Administrator.
2. Change into the extracted folder:

       cd C:\Users\<you>\Downloads\astar-middleware-deploy

3. Run:

       Unblock-File .\install.ps1
       .\install.ps1

   The version in PYTHON_VERSION.txt installs automatically when a matching
   python\ installer is bundled. Otherwise install that exact version first.
   Expected output:

       ==> Verifying release manifest                       [OK]
       ==> Checking Python <version from PYTHON_VERSION.txt>
       ==> Creating C:\SECSGEM_EAP directory tree            [OK]
       ==> Copying middleware source to C:\SECSGEM_EAP\app  [OK]
       ==> Installing Python dependencies offline            [OK] Dependencies installed
       ==> Smoke checking middleware imports                 [OK] All imports OK
       ==> Installation complete.

   Red errors? Screenshot and send to the developer before continuing.


STEP 3  CONFIGURE MACHINE IPs
------------------------------
The installer opens production.yaml in Notepad automatically.
Find the "machines:" section and update each tool entry:

    - endpoint_id: "TOOL_02"
      display_name: "DAVINCI200_MC4_HC1_01"
      machine_profile: "davinci_200_mc4_hc1"
      host: "10.10.20.32"        <- update to the real tool IP
      port: 5000                 <- update if tool uses a different port
      secs_device_id: 0          <- update if tool uses a different ID
      enabled: true

Set enabled: false for any tool not yet connected.
Save with Ctrl+S.


STEP 4  SET LINKSTUFFS TOKENS
------------------------------
Find the "linkstuffs_http:" section in production.yaml. Reference one
machine environment variable next to each matching display_name:

    device_tokens:
      DAVINCI200_MC4_HC1_01: "${LINKSTUFFS_HTTP_DAVINCI_TOKEN}"

Tokens are in Linkstuffs admin → Entities → Devices → Manage credentials.
Set each environment variable outside the repository before starting the
service. Never store a live token in a Git checkout.


STEP 5  VALIDATE THE CONFIG
----------------------------
Open a new PowerShell window (not as Administrator):

    cd C:\SECSGEM_EAP\app
    python -m eap_middleware validate-config --config config\production.yaml

Fix any errors reported before continuing.


STEP 6  TEST ONE MACHINE
--------------------------
This opens the HSMS connection and reads the tool's identity without
sending anything to Linkstuffs. Use it to confirm network reach and
correct IP/port before starting the service.

    python -m eap_middleware test-machine --config config\production.yaml --endpoint-id TOOL_02

Expected: tool model name printed within ~10 seconds.
Errors:
  "Connection refused"  — wrong IP/port, or tool HSMS is off
  "Connection timeout"  — firewall blocking, or tool unreachable
  "Device ID mismatch"  — wrong secs_device_id

To test all enabled machines at once:

    python -m eap_middleware test-machine --config config\production.yaml --endpoint-id ALL


STEP 7  TEST LINKSTUFFS
------------------------
    python -m eap_middleware test-linkstuffs --config config\production.yaml

If it fails, recheck the token (Step 4) and base_url in production.yaml.


STEP 8  WINDOWS FIREWALL
--------------------------
The installer adds outbound rules for port 5000 (HSMS) and 8883 (MQTT TLS)
automatically. If you see firewall pop-ups when the service starts, add
the rules manually:

    New-NetFirewallRule -DisplayName "SECS/GEM HSMS" `
        -Direction Outbound -Protocol TCP -RemotePort 5000 -Action Allow

    New-NetFirewallRule -DisplayName "Linkstuffs MQTT" `
        -Direction Outbound -Protocol TCP -RemotePort 8883 -Action Allow


STEP 9  INSTALL AS A WINDOWS SERVICE
--------------------------------------
Recommended — auto-starts on boot and restarts on crash.

Option A — NSSM (recommended):
  Copy nssm.exe to C:\Tools\nssm\nssm.exe, then:

    cd C:\SECSGEM_EAP\app
    scripts\install_service.ps1

  Override paths if needed:
    scripts\install_service.ps1 `
        -PythonExe "C:\Python311\python.exe" `
        -NssmExe   "C:\Tools\nssm\nssm.exe"

Option B — Task Scheduler (no NSSM):

    $action  = New-ScheduledTaskAction -Execute "python" `
                 -Argument "-m eap_middleware run-service --config C:\SECSGEM_EAP\app\config\production.yaml" `
                 -WorkingDirectory "C:\SECSGEM_EAP\app"
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0
    Register-ScheduledTask -TaskName "AstarEapMiddleware" `
        -Action $action -Trigger $trigger -Settings $settings `
        -RunLevel Highest -Force
    Start-ScheduledTask -TaskName "AstarEapMiddleware"

Service commands (NSSM):
    Start-Service AstarSecsGemEapMiddleware
    Stop-Service  AstarSecsGemEapMiddleware
    Get-Service   AstarSecsGemEapMiddleware


WHERE THINGS LIVE AFTER INSTALL
---------------------------------
  C:\SECSGEM_EAP\app\                   middleware code
  C:\SECSGEM_EAP\app\config\            production.yaml and admin defaults
  C:\SECSGEM_EAP\logs\                  service log (stdout + stderr)
  C:\SECSGEM_EAP\data\                  SQLite outbox (queued telemetry)
  C:\SECSGEM_EAP\machines\<name>\config\ per-machine admin files (hot reload)

Per-lot CSV output:
  D:\MachineData\EAP_<display_name>\csv_in\       local copy
  \\TD-DATASVR-F2C4\TD_<display_name>.csv_in\     network share copy

D:\ must exist and be writable. The network share path can be changed in
production.yaml (network_csv_path) if your server uses a different share.


HOW TO CONFIRM IT IS WORKING
------------------------------
1. Start the service.
2. Tail the log:
       Get-Content C:\SECSGEM_EAP\logs\service_stderr.log -Tail 30 -Wait
3. Within ~30 seconds, each enabled tool shows "Connected to <IP>".
4. Trigger a lot run — CSV appears in D:\MachineData\... and telemetry
   appears in Linkstuffs within a few seconds.

No log output at all on startup? Check Windows Event Viewer:
  Start → "Event Viewer" → Windows Logs → Application → filter by "python"


COMMON PROBLEMS
----------------

Q: install.ps1 fails with "execution policy" error
A: Allow the reviewed installer for the current PowerShell session:
   Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process -Force
   Unblock-File .\install.ps1

Q: "pip install errors — could not find a version"
A: The wheels target the version recorded in PYTHON_VERSION.txt.
   Compare: type PYTHON_VERSION.txt
            python --version

Q: test-machine: "ConnectionRefusedError"
A: Wrong IP or port in production.yaml, or the tool's HSMS service is not
   running. Confirm with the tool operator. Also check: no other host
   software (MES, vendor software) is already connected to that tool.

Q: test-machine: "timeout"
A: Network path blocked. Ping the tool IP first:
   ping 10.10.20.32
   If ping fails, check switch/VLAN routing between server and tool.

Q: test-linkstuffs: "Connection refused"
A: Linkstuffs unreachable or wrong token. Check base_url and the token
   in production.yaml.

Q: Config changes not taking effect
A: Restart the service:   Restart-Service AstarSecsGemEapMiddleware

Q: Telemetry arriving but no CSV files
A: Confirm D:\MachineData\... exists and the service account can write to it.
   Check C:\SECSGEM_EAP\logs\ for IOError entries.

Q: Need to send logs to the developer
A: ZIP C:\SECSGEM_EAP\logs\ and attach. That folder has everything needed.


===============================================================================
  Stop here if anything above does not match what you see. Contact the
  developer before continuing — easier to fix early than after a partial setup.
===============================================================================
