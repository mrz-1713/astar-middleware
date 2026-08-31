#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef AppSource
  #define AppSource ".build\mg-simulator\dist\MGSimulator"
#endif
#ifndef OutputDir
  #define OutputDir "artifacts\mg-simulator"
#endif

[Setup]
AppId={{7C4A1E52-9B30-4D68-B1F2-3E5A6C08D941}
AppName=MG Simulator
AppVersion={#AppVersion}
AppPublisher=ASTAR Middleware
DefaultDirName={localappdata}\Programs\MGSimulator
DefaultGroupName=MG Simulator
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=MGSimulator-Setup-{#AppVersion}-win-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\MGSimulator.exe
VersionInfoVersion={#AppVersion}
VersionInfoDescription=NexGen MG Series SECS/GEM Equipment Simulator

[Dirs]
Name: "{app}\logs"

[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\MG Simulator (Passive)"; Filename: "{app}\start-passive.bat"; WorkingDir: "{app}"; IconFilename: "{app}\MGSimulator.exe"
Name: "{group}\MG Simulator (Active)"; Filename: "{app}\start-active.bat"; WorkingDir: "{app}"; IconFilename: "{app}\MGSimulator.exe"
Name: "{group}\MG Simulator (Refused Band Demo)"; Filename: "{app}\start-band-refusal-demo.bat"; WorkingDir: "{app}"; IconFilename: "{app}\MGSimulator.exe"
Name: "{group}\MG Simulator (Host Off-Line Demo)"; Filename: "{app}\start-host-offline-demo.bat"; WorkingDir: "{app}"; IconFilename: "{app}\MGSimulator.exe"
Name: "{group}\Open Logs"; Filename: "{sys}\explorer.exe"; Parameters: """{app}\logs"""
Name: "{group}\Operator Guide"; Filename: "{sys}\notepad.exe"; Parameters: """{app}\README_OPERATOR.md"""
Name: "{group}\Uninstall MG Simulator"; Filename: "{uninstallexe}"
