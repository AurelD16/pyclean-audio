; pyclean-audio — Windows installer (Inno Setup 6).
;
; Build the runtime first (packaging/build_runtime.ps1), then:
;   iscc.exe packaging\installer\pyclean-audio.iss
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=1.0.0 ^
;       packaging\installer\pyclean-audio.iss
; Output: dist\pyclean-audio-<version>-setup.exe  (~2.6 GB, LZMA2)
;
; Per-user install by default: no administrator rights, nothing written outside
; the user's profile, uninstaller unins000.exe. /VERYSILENT for unattended
; installs ("/VERYSILENT /SUPPRESSMSGBOXES /NORESTART").
;
; The payload is the runtime tree built by build_runtime.ps1: it is read-only at
; run time — results, logs and the model cache live in %LOCALAPPDATA%\pyclean-audio
; (packaging/launcher/launcher.py), so uninstalling keeps the user's work.
;
; The exe bundles pywebview by default (build_runtime.ps1 -WithWebView), so the
; app opens in its own window and closing that window quits. WebView2 — what
; that window needs — ships with Windows 11 and Windows 10 21H2+; where it is
; missing, the launcher opens the page in the default browser instead and the
; page's Quit button stops the app. Nothing is downloaded at install time.

#define MyAppName "pyclean-audio"
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppExeName "pyclean-audio.exe"
#define MyAppPublisher "pyclean-audio"
#define MyAppURL "https://github.com/AurelD16/pyclean-audio"

[Setup]
; stable across versions: an upgrade reuses the same install directory
AppId={{6E3B1F42-6C1B-4E2E-9E0B-1A2B3C4D5E6F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
VersionInfoVersion={#MyAppVersion}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoDescription={#MyAppName} desktop application

; Per-user: {localappdata} resolves to the current user's AppData, and no
; elevation is ever requested. The dialog lets the user ask for an
; machine-wide install (PrivilegesRequiredOverridesAllowed=dialog).
DefaultDirName={localappdata}\Programs\pyclean-audio
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

; A 2.6 GB payload: native LZMA2 with a solid archive. Inno Setup shows its own
; progress bar, and /VERYSILENT suppresses the wizard entirely.
OutputDir=..\..\dist
OutputBaseFilename=pyclean-audio-{#MyAppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
WizardSizePercent=120
ShowLanguageDialog=no
SetupLogging=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Everything built by packaging/build_runtime.ps1: runtime/python, runtime/bin,
; app/, static/, LICENCE, THIRD-PARTY-NOTICES.txt, launcher.py and the .exe.
Source: "..\..\dist\pyclean-audio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; Launches with no console window (the launcher is frozen with --noconsole).
; skipifsilent: /VERYSILENT must not start the app on a build machine.
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Nothing outside {app} is ever deleted: the results and the model cache live in
; %LOCALAPPDATA%\pyclean-audio (PYCLEAN_DATA_DIR / HF_HOME). Deliberately empty.
