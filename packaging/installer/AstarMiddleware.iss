; Single-file Windows installer for the whole ASTAR SECS/GEM EAP middleware.
;
; Payload = the offline deploy package produced by scripts/build_deploy_package.sh
; (bundled Python installer + win_amd64 wheels + source + gui). This script only
; copies that payload to {app} and runs the existing install.ps1 against it - all
; the real work (Python install, offline pip, config merge, firewall, smoke test)
; already lives there and is not duplicated here.
;
; Build:  packaging\installer\build_installer.ps1   (needs Inno Setup 6 + the staged package)

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef AppSource
  #define AppSource "..\..\deploy_out\astar-middleware-deploy"
#endif
#ifndef AppBuildId
  #define AppBuildId "local"
#endif
#ifndef OutputDir
  #define OutputDir "artifacts\installer"
#endif

[Setup]
AppId={{4F1C8A76-2D93-4E15-9B0A-7C6E5D48F312}
AppName=ASTAR SECS/GEM EAP Middleware
AppVersion={#AppVersion}
AppPublisher=ASTAR Middleware
; install.ps1 installs Python for all users and adds firewall rules.
PrivilegesRequired=admin
; The bundled wheels and Python installer are win_amd64 only.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; {app} is the middleware root: install.ps1 puts the code in {app}\app and
; creates logs\ data\ archive\ machines\ beside it. The wheels and the Python
; installer stay here too, so a repair or re-install works with no network and
; no original media.
DefaultDirName={sd}\SECSGEM_EAP
DefaultGroupName=ASTAR EAP Middleware
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=AstarMiddleware-Setup-{#AppVersion}-{#AppBuildId}-win-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\app\gui\app.py
VersionInfoVersion={#AppVersion}
VersionInfoDescription=ASTAR SECS/GEM EAP Middleware build {#AppBuildId} (offline installer)
; Python + ~40 wheels + source; refuse to start if the disk is too tight.
ExtraDiskSpaceRequired=524288000

[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "start-gui.bat"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\ASTAR EAP Control Panel"; Filename: "{app}\start-gui.bat"; WorkingDir: "{app}\app"
Name: "{commondesktop}\ASTAR EAP Control Panel"; Filename: "{app}\start-gui.bat"; WorkingDir: "{app}\app"; Tasks: desktopicon
Name: "{group}\Edit production.yaml"; Filename: "{sys}\notepad.exe"; Parameters: """{app}\app\config\production.yaml"""
Name: "{group}\Open Logs"; Filename: "{sys}\explorer.exe"; Parameters: """{app}\logs"""
Name: "{group}\Deployment Guide"; Filename: "{sys}\notepad.exe"; Parameters: """{app}\README_DEPLOY.txt"""
Name: "{group}\Uninstall ASTAR EAP Middleware"; Filename: "{uninstallexe}"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut for the control panel"; GroupDescription: "Additional shortcuts:"

; Deliberately no [UninstallDelete] for app\config, logs, data, archive or
; machines: those hold the operator's live machine IPs, device tokens and
; historical logs. Uninstall removes only what setup laid down.

[Code]
var
  OfflineInstallFailed: Boolean;

function RunOfflineInstall(): Boolean;
var
  ResultCode: Integer;
begin
  // install.ps1 verifies RELEASE_MANIFEST.sha256 before touching anything, so
  // a truncated or tampered payload aborts here rather than half-installing.
  Result := Exec(
    ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\upgrade.ps1')
      + '" -InstallDir "' + ExpandConstant('{app}') + '"',
    ExpandConstant('{app}'), SW_SHOW, ewWaitUntilTerminated, ResultCode)
    and (ResultCode = 0);
end;

function GetCustomSetupExitCode(): Integer;
begin
  if OfflineInstallFailed then
    // Machine-detectable failure for deployment automation. This is outside
    // Inno's reserved 0-8 range and stable for ASTAR installer consumers.
    Result := 100
  else
    Result := 0;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    // Inno ignores a non-zero exit code by default, which would report success
    // after a failed Python or pip step. Surface it instead.
    if not RunOfflineInstall() then begin
      OfflineInstallFailed := True;
      MsgBox('Setup copied the files but install.ps1 did not complete.'#13#10#13#10
        + 'Re-run it from an Administrator PowerShell to see the error:'#13#10
        + ExpandConstant('{app}\upgrade.ps1'), mbError, MB_OK);
    end;
end;
