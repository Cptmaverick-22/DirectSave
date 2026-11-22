; Inno Setup script for YT Downloader

#ifndef MyAppVersion
  #define MyAppVersion "1.1"
#endif

#define MyAppName "YT Downloader"
#define MyAppExeName "YTDownloader.exe"

[Setup]
LicenseFile=LICENSE
; Fixed GUID so future installer versions upgrade in place cleanly.
AppId={{8F3B4A1E-9C2D-4E7A-B5D1-6A2F8C1E3B4D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
DefaultDirName={autopf}\YTDownloader
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Per-user install (no admin password / UAC prompt required).
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=dist\installer
OutputBaseFilename=YTDownloader-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
#ifexist "app.ico"
SetupIconFile=app.ico
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Package the entire PyInstaller --onedir output (YTDownloader.exe + _internal\).
Source: "dist\YTDownloader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "THIRD_PARTY_LICENSES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent