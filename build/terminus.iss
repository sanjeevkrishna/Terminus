; Terminus - Inno Setup installer
;
; Compiled by .github/workflows/release.yml:
;
;   ISCC.exe /DMyAppVersion=1.2.0 /DMyAppVersionNumeric=1.2.0 \
;            /DDistDir=<abs path to PyInstaller onedir output> build\terminus.iss
;
; Per-user install by default: no UAC prompt, and Terminus needs no
; machine-wide state. Its data lives in %USERPROFILE%\.terminus regardless.
;
; Requires Inno Setup 6.3 or newer (for ArchitecturesAllowed=x64compatible).
;
; File path: build/terminus.iss

; ---------------------------------------------------------------------------
; Defines
; ---------------------------------------------------------------------------
#define MyAppName        "Terminus"
#define MyAppPublisher   "Cisco Systems, Inc."
#define MyAppURL         "https://github.com/sanjeevkrishna/Terminus"
#define MyAppExeName     "Terminus.exe"

; Display version - may contain suffixes such as 1.2.0-rc1
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

; VersionInfoVersion accepts digits and dots only, so CI passes a cleaned
; value separately. Never merge these two.
#ifndef MyAppVersionNumeric
  #define MyAppVersionNumeric "0.0.0"
#endif

; Folder containing Terminus.exe and _internal\.
#ifndef DistDir
  #define DistDir AddBackslash(SourcePath) + "..\dist\windowed\Terminus"
#endif

#define IconPath AddBackslash(SourcePath) + "..\terminus\static\img\terminus.ico"
#define WizLarge   AddBackslash(SourcePath) + "wizard-large.bmp"
#define WizLarge2x AddBackslash(SourcePath) + "wizard-large@2x.bmp"
#define WizSmall   AddBackslash(SourcePath) + "wizard-small.bmp"
#define WizSmall2x AddBackslash(SourcePath) + "wizard-small@2x.bmp"

; Fail at compile time with a clear message rather than producing a broken
; installer or an opaque ISCC error.
#if !DirExists(DistDir)
  #error DistDir does not exist. Run PyInstaller before compiling this script.
#endif
#if !FileExists(AddBackslash(DistDir) + MyAppExeName)
  #error Terminus.exe not found inside DistDir.
#endif

; ---------------------------------------------------------------------------
[Setup]
; ---------------------------------------------------------------------------
; NEVER change AppId: Inno keys upgrade and uninstall detection off it.
AppId={{7C4E9A31-B85D-4F62-9E17-3D5A8C0F62B4}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
VersionInfoVersion={#MyAppVersionNumeric}
VersionInfoProductVersion={#MyAppVersionNumeric}
VersionInfoDescription={#MyAppName} Setup
VersionInfoCompany={#MyAppPublisher}

DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UninstallDisplayName={#MyAppName} {#MyAppVersion}
UninstallDisplayIcon={app}\{#MyAppExeName}

; Per-machine when elevated, per-user otherwise. Avoids forcing UAC on users
; who only want it in their profile. With PrivilegesRequired=lowest,
; {autopf} resolves to {localappdata}\Programs.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; Restart Manager detects a running Terminus and offers to close it, so an
; upgrade does not fail on locked _internal\*.pyd files. Requires no
; cooperation from the application itself.
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=no

MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

OutputDir={#AddBackslash(SourcePath)}..\dist\installer
OutputBaseFilename={#MyAppName}-{#MyAppVersion}-win64-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

#if FileExists(IconPath)
SetupIconFile={#IconPath}
#endif


#if FileExists(WizLarge) && FileExists(WizLarge2x)
WizardImageFile={#WizLarge},{#WizLarge2x}
#endif
#if FileExists(WizSmall) && FileExists(WizSmall2x)
WizardSmallImageFile={#WizSmall},{#WizSmall2x}
#endif

WizardImageStretch=no
WizardImageAlphaFormat=premultiplied

; ---------------------------------------------------------------------------
[Languages]
; ---------------------------------------------------------------------------
Name: "english"; MessagesFile: "compiler:Default.isl"

; ---------------------------------------------------------------------------
[Tasks]
; ---------------------------------------------------------------------------
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
  GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

; ---------------------------------------------------------------------------
[Files]
; ---------------------------------------------------------------------------
Source: "{#AddBackslash(DistDir)}*"; DestDir: "{app}"; \
  Flags: recursesubdirs createallsubdirs ignoreversion

; ---------------------------------------------------------------------------
[Icons]
; ---------------------------------------------------------------------------
Name: "{group}\{#MyAppName}";           Filename: "{app}\{#MyAppExeName}"; Parameters: "{code:LaunchMode}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";     Filename: "{app}\{#MyAppExeName}"; Parameters: "{code:LaunchMode}"; Tasks: desktopicon

; ---------------------------------------------------------------------------
[Run]
; ---------------------------------------------------------------------------
Filename: "{app}\{#MyAppExeName}"; Parameters: "{code:LaunchMode}"; \
  Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; \
  Flags: nowait postinstall skipifsilent

; ---------------------------------------------------------------------------
[UninstallDelete]
; ---------------------------------------------------------------------------
; PyInstaller writes __pycache__ into _internal on first run, so the
; directory is not empty at uninstall time and must be removed explicitly.
Type: filesandordirs; Name: "{app}\_internal"
Type: dirifempty;     Name: "{app}"

[Code]
// ---------------------------------------------------------------------------
// Pascal Script. Comments are // or { }, never ";".
// This is a subset of Delphi: no local const blocks, and declarations must
// appear as const-then-var-then-begin.
// ---------------------------------------------------------------------------

// WebView2 is present on Windows 11 and current Windows 10, but not on older
// or freshly imaged machines. Warn rather than block: browser mode still works.
//
// The GUID literal is repeated rather than hoisted into a constant, because
// Pascal Script does not reliably support local const sections. Braces inside
// string literals are safe here: [Code] does not expand {constants}.

var
  ModePage: TInputOptionWizardPage;

procedure InitializeWizard();
begin
  ModePage := CreateInputOptionPage(wpSelectTasks,
    'Launch Mode',
    'How should Terminus open?',
    'This sets the shortcut''s launch mode. You can change it later in the ' +
    'shortcut properties.',
    True,    // exclusive: radio buttons, not checkboxes
    False);
  ModePage.Add('Desktop window (recommended)');
  ModePage.Add('Default web browser');
  ModePage.Values[0] := True;
end;

// Used as {code:LaunchMode} in [Icons] and [Run]. Honours
// /launchmode=browser|desktop so silent installs can choose too.
function LaunchMode(Param: String): String;
var
  Forced: String;
begin
  Forced := ExpandConstant('{param:launchmode|}');
  if CompareText(Forced, 'browser') = 0 then begin
    Result := 'browser';
    Exit;
  end;
  if CompareText(Forced, 'desktop') = 0 then begin
    Result := 'desktop';
    Exit;
  end;

  if (ModePage <> nil) and ModePage.Values[1] then
    Result := 'browser'
  else
    Result := 'desktop';
end;

function WebView2Present(): Boolean;
var
  Value: String;
begin
  Result :=
    RegQueryStringValue(HKLM,
      'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
      'pv', Value) or
    RegQueryStringValue(HKLM,
      'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
      'pv', Value) or
    RegQueryStringValue(HKCU,
      'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
      'pv', Value);

  // A present-but-empty 'pv' means a broken or removed runtime.
  if Result and (Trim(Value) = '') then
    Result := False;
end;

function InitializeSetup(): Boolean;
begin
  Result := True;

  // Never prompt during an unattended install: it would hang CI or a
  // deployment script forever.
  if WizardSilent() then
    Exit;

  if not WebView2Present() then
  begin
    if MsgBox('The Microsoft Edge WebView2 runtime was not found.' + #13#13 +
              'Terminus needs it for the desktop window. Without it you can ' +
              'still run Terminus in a browser.' + #13#13 +
              'Continue with the installation?',
              mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
  Home: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;

  // Silent uninstall must never destroy user data without being asked.
  if UninstallSilent() then
    Exit;

  Home := GetEnv('USERPROFILE');
  if Home = '' then
    Exit;

  DataDir := AddBackslash(Home) + '.terminus';
  if not DirExists(DataDir) then
    Exit;

  if MsgBox('Remove your Terminus data as well?' + #13#13 +
            DataDir + #13#13 +
            'This deletes saved connectors, session logs, and the ' +
            'encryption key. Stored passwords become unrecoverable.',
            mbConfirmation, MB_YESNO) = IDYES then
    DelTree(DataDir, True, True, True);
end;