; Inno Setup script for Better Voice Typing.
;
;   iscc /DAppVersion=1.0.0 installer\BetterVoiceTyping.iss
;
; Expects the PyInstaller one-folder build in dist\BetterVoiceTyping\.
; Per-user install (no admin prompt) into %LOCALAPPDATA%\Programs. User data
; stays in Documents\VoiceTyping and %LOCALAPPDATA%\BetterVoiceTyping and is
; never touched by install, update or uninstall.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Better Voice Typing"
#define AppExe "BetterVoiceTyping.exe"
#define AppPublisher "Elevate Code"
#define AppURL "https://github.com/Elevate-Code/better-voice-typing"

[Setup]
AppId={{7E1C2C7A-5B2E-4C1F-9A63-3D0F8B9E4A21}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\dist\installer
OutputBaseFilename=BetterVoiceTyping-Setup-{#AppVersion}
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Refuse to install over a running instance (same mutex as modules/single_instance.py);
; the in-app updater waits for the app to exit before running setup silently.
AppMutex=Local\BetterVoiceTyping_SingleInstance
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startup"; Description: "Start {#AppName} when I sign in to Windows"
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist\BetterVoiceTyping\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: startup
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Nothing: user data in Documents\VoiceTyping is deliberately left in place.
