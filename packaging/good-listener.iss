#define AppName "Good Listener"
#define AppVersion "0.1.0"
#define AppPublisher "good-listener"
#define AppExeName "good-listener.exe"

[Setup]
AppId={{8454DC2D-A7FD-4C4A-98F5-3EA6392F7549}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\Good Listener
DefaultGroupName=Good Listener
OutputDir=..\dist\installer
OutputBaseFilename=good-listener-{#AppVersion}-windows-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#AppExeName}

[Files]
Source: "..\dist\good-listener\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Good Listener"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\Good Listener"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "바탕 화면 바로가기 만들기"; GroupDescription: "추가 아이콘:"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Good Listener 실행"; Flags: nowait postinstall skipifsilent

; Uninstall intentionally leaves %LOCALAPPDATA%\GoodListener intact.
; Meeting audio, transcripts, minutes, and the DPAPI-protected key are deleted only
; through the in-app explicit delete flow or the documented administrator procedure.
