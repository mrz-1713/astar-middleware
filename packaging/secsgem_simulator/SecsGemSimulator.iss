#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef AppSource
  #define AppSource ".build\secsgem-simulator\dist\SecsGemSimulator"
#endif
#ifndef OutputDir
  #define OutputDir "artifacts\secsgem-simulator"
#endif

[Setup]
AppId={{A61F39A7-E142-4E4D-AD7A-D7B6357445DE}
AppName=ASTAR SECS/GEM Simulator
AppVersion={#AppVersion}
AppPublisher=ASTAR Middleware
DefaultDirName={localappdata}\Programs\SecsGemSimulator
DefaultGroupName=ASTAR SECS/GEM Simulator
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=SecsGemSimulator-Setup-{#AppVersion}-win-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\AstarSimulatorGui.exe
VersionInfoVersion={#AppVersion}
VersionInfoDescription=ASTAR SECS/GEM Simulator (equipment or host role)

[Dirs]
Name: "{app}\logs"

[Files]
; The YAML files carry the operator's own IPs and role choice, so they are
; laid down once and never overwritten or removed by a later version.
Source: "{#AppSource}\*"; DestDir: "{app}"; Excludes: "simulator.yaml,davinci-active.yaml,davinci-passive.yaml,host-example.yaml"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#AppSource}\simulator.yaml"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "{#AppSource}\davinci-active.yaml"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "{#AppSource}\davinci-passive.yaml"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "{#AppSource}\host-example.yaml"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
; Every shortcut names the SECS role first and the HSMS direction second.
; The old "(Passive)" / "(Active)" labels named only the transport, which
; left the operator to guess whether the thing was the tool or the EAP.
Name: "{group}\Simulator Control Panel"; Filename: "{app}\AstarSimulatorGui.exe"; WorkingDir: "{app}"; Parameters: "--config ""{app}\simulator.yaml"""
Name: "{commondesktop}\Simulator Control Panel"; Filename: "{app}\AstarSimulatorGui.exe"; WorkingDir: "{app}"; Parameters: "--config ""{app}\simulator.yaml"""; Tasks: desktopicon
Name: "{group}\Run as EQUIPMENT (listen, HSMS passive)"; Filename: "{app}\start-passive.bat"; WorkingDir: "{app}"; IconFilename: "{app}\SecsGemSimulator.exe"
Name: "{group}\Run as EQUIPMENT (dial out, HSMS active)"; Filename: "{app}\start-active.bat"; WorkingDir: "{app}"; IconFilename: "{app}\SecsGemSimulator.exe"
Name: "{group}\Run as HOST (dial out, HSMS active)"; Filename: "{app}\start-host.bat"; WorkingDir: "{app}"; IconFilename: "{app}\SecsGemSimulator.exe"
Name: "{group}\Edit control panel configuration"; Filename: "{sys}\notepad.exe"; Parameters: """{app}\simulator.yaml"""
Name: "{group}\Edit EQUIPMENT passive configuration"; Filename: "{sys}\notepad.exe"; Parameters: """{app}\davinci-passive.yaml"""
Name: "{group}\Edit EQUIPMENT active configuration"; Filename: "{sys}\notepad.exe"; Parameters: """{app}\davinci-active.yaml"""
Name: "{group}\Edit HOST configuration"; Filename: "{sys}\notepad.exe"; Parameters: """{app}\host-example.yaml"""
Name: "{group}\Open Logs"; Filename: "{sys}\explorer.exe"; Parameters: """{app}\logs"""
Name: "{group}\Operator Guide"; Filename: "{sys}\notepad.exe"; Parameters: """{app}\README_OPERATOR.md"""
Name: "{group}\Uninstall ASTAR SECS/GEM Simulator"; Filename: "{uninstallexe}"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut for the control panel"; GroupDescription: "Additional shortcuts:"
