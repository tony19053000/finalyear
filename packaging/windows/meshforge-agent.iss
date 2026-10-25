; MeshForge agent installer for Windows 10 (22H2) / 11, x64 (V0.17 G4, D395).
;
; Built by scripts/build_windows_installer.py (never by hand): it passes
;   /DBundleDir=<D384 bundle folder>   /DAppVersion=<PEP 440 version>
;   /DVersionDir=<version>-<revision>  /DNumericVersion=<a.b.c.d>
;   /DOutputDir=<folder>               /DOutputName=<file name without .exe>
;
; What it does (the decisions live in Python, src/meshforge/agent/winsetup.py):
;   1. pages: welcome -> licence -> install folder -> "what this computer will
;      share" -> ready -> install -> finish ("Open MeshForge", checked);
;   2. copies the bundle to {app}\<VersionDir>\ (side by side with an older
;      version: an upgrade never overwrites the files the service runs);
;   3. runs, elevated, "<VersionDir>\python\python.exe -I -m
;      meshforge.agent.winservice setup": legacy dev task stopped + disabled,
;      service created / re-pointed, identity migrated, console access for the
;      installing user, firewall rule, start + health check, rollback to the
;      previous version on a failed upgrade, current.txt + PATH shims;
;   4. the finish page shows that step's own words; the installer never says
;      "done" over a stopped service without saying why. Silent installs
;      (/VERYSILENT /SUPPRESSMSGBOXES /LOG=...) exit 100 + the step's exit code
;      when it did not end healthy (101 failed / rolled back, 104 needs
;      attention), 0 when it did.
;   Uninstall ("Apps & features" or unins000.exe [/VERYSILENT] [/REMOVEDATA]):
;   "winservice teardown" removes the service, its firewall rule, Docker access,
;   the legacy task and the PATH entry; data is kept unless the owner says yes to
;   "remove all data" (default No, Q9) or /REMOVEDATA is given.

#ifndef BundleDir
  #error BundleDir is not defined: build with scripts/build_windows_installer.py
#endif
#ifndef AppVersion
  #error AppVersion is not defined: build with scripts/build_windows_installer.py
#endif
#ifndef VersionDir
  #error VersionDir is not defined: build with scripts/build_windows_installer.py
#endif
#ifndef NumericVersion
  #define NumericVersion "0.0.0.0"
#endif
#ifndef OutputDir
  #define OutputDir "dist"
#endif
#ifndef OutputName
  #define OutputName "MeshForge-Agent-Setup-" + VersionDir
#endif

; Fixed forever: Windows recognises upgrades and the uninstall entry by it.
#define AppId "{{A8774C05-C367-491D-833B-4979498B0BEE}"
#define ServiceName "MeshForgeAgent"

[Setup]
AppId={#AppId}
AppName=MeshForge Agent
AppVersion={#AppVersion}
AppVerName=MeshForge Agent {#AppVersion}
AppPublisher=MeshForge team
AppPublisherURL=https://github.com/tony19053000/finalyear
AppSupportURL=https://github.com/tony19053000/finalyear
VersionInfoVersion={#NumericVersion}
VersionInfoProductName=MeshForge Agent
VersionInfoDescription=MeshForge Agent Setup
DefaultDirName={autopf}\MeshForge
UsePreviousAppDir=yes
AppendDefaultDirName=yes
DirExistsWarning=no
DisableWelcomePage=no
DisableDirPage=no
DisableProgramGroupPage=yes
DisableReadyPage=no
LicenseFile=license.txt
PrivilegesRequired=admin
; Q29: x64 Windows only (no ARM), Windows 10 22H2 (19045) or newer.
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.19045
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
WizardStyle=modern
SetupLogging=yes
; The service is stopped by our own step (PrepareToInstall / winservice setup),
; never by Restart Manager guessing at running programs.
CloseApplications=no
RestartApplications=no
; winservice setup edits the machine PATH; Setup broadcasts the change at the end.
ChangesEnvironment=yes
UninstallDisplayName=MeshForge Agent
UninstallDisplayIcon={app}\{#VersionDir}\python\pythonw.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
FinishedHeadingLabel=MeshForge setup finished

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}\{#VersionDir}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\MeshForge"; Filename: "{app}\{#VersionDir}\python\pythonw.exe"; Parameters: "-I -m meshforge.agent.console.launcher"; WorkingDir: "{app}"; Comment: "Open the MeshForge console of this computer"
Name: "{autoprograms}\MeshForge - give Docker access"; Filename: "{app}\{#VersionDir}\python\pythonw.exe"; Parameters: "-I -m meshforge.agent.console.launcher --docker-access"; WorkingDir: "{app}"; Comment: "Let MeshForge run jobs in Docker (asks for an administrator)"

[Run]
Filename: "{app}\{#VersionDir}\python\pythonw.exe"; Parameters: "-I -m meshforge.agent.console.launcher"; WorkingDir: "{app}"; Description: "Open MeshForge"; Flags: postinstall nowait skipifsilent runasoriginaluser; Check: ServiceHealthy

[UninstallDelete]
; {app} always ends in "MeshForge" (checked before install), so this removes
; only MeshForge's own folder: every version, the PATH shims, current.txt.
Type: filesandordirs; Name: "{app}"

[Code]
var
  SharePage: TOutputMsgWizardPage;
  SetupRan: Boolean;
  SetupResultCode: Integer;
  SetupMessage: String;
  RemoveData: Boolean;

function SetupPython(): String;
begin
  Result := ExpandConstant('{app}\{#VersionDir}\python\python.exe');
end;

{ The [Run] entry "Open MeshForge" only when the service ended healthy. }
function ServiceHealthy(): Boolean;
begin
  Result := SetupRan and (SetupResultCode = 0);
end;

procedure InitializeWizard();
begin
  SharePage := CreateOutputMsgPage(wpSelectDir,
    'What this computer will share',
    'MeshForge runs in the background as the ' + '{#ServiceName}' + ' service.',
    'MeshForge starts with Windows and keeps running when you sign out, so the ' +
    'fabric you join can use this computer''s spare CPU (and, if you allow it, ' +
    'its GPU).' + #13#10#13#10 +
    'It will NOT run anyone''s jobs until you turn on "accept jobs" in the ' +
    'MeshForge console. GPU sharing is off and CPU use is capped at 50 % until ' +
    'you change it there.' + #13#10#13#10 +
    'At the end of the installation the console opens in your browser, so this ' +
    'computer can join your fabric with the code (or QR code) from the ' +
    'dashboard.' + #13#10#13#10 +
    'Your data, identity and logs go to ' + ExpandConstant('{commonappdata}') +
    '\MeshForge; uninstalling keeps them unless you choose to remove them.');
end;

function DirNameProblem(const Dir: String): String;
begin
  Result := '';
  if CompareText(ExtractFileName(RemoveBackslashUnlessRoot(Dir)), 'MeshForge') <> 0 then
    Result := 'Please choose a folder named "MeshForge" (for example ' +
      ExpandConstant('{autopf}') + '\MeshForge): uninstalling removes that ' +
      'whole folder.';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Problem: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    Problem := DirNameProblem(WizardDirValue());
    if Problem <> '' then
    begin
      MsgBox(Problem, mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function ServiceRunsFrom(const Folder: String): Boolean;
var
  ImagePath: String;
begin
  Result := False;
  if RegQueryStringValue(HKLM, 'SYSTEM\CurrentControlSet\Services\{#ServiceName}',
      'ImagePath', ImagePath) then
    Result := Pos(Lowercase(Folder), Lowercase(ImagePath)) > 0;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Result := DirNameProblem(WizardDirValue());
  if Result <> '' then
    exit;
  { A repair of the version the service runs: its files are in use. An upgrade
    goes into a new folder and the old one keeps running until the setup step. }
  if ServiceRunsFrom(ExpandConstant('{app}\{#VersionDir}\')) then
  begin
    Log('The service runs this version: stopping it before the files are copied');
    if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
        '-NoProfile -NonInteractive -Command "Stop-Service {#ServiceName} -Force ' +
        '-ErrorAction SilentlyContinue; (Get-Service {#ServiceName}).WaitForStatus(' +
        '''Stopped'', ''00:01:30'')"',
        '', SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
      Result := 'The ' + '{#ServiceName}' + ' service did not stop within 90 s. ' +
        'Wait for running MeshForge jobs to finish, then run the installer again.';
  end;
end;

procedure RunSetupStep();
var
  Params, ResultFile, MessageFile: String;
  Lines: TArrayOfString;
  I: Integer;
begin
  ResultFile := ExpandConstant('{commonappdata}\MeshForge\logs\setup-result.json');
  MessageFile := ExpandConstant('{tmp}\setup-message.txt');
  Params := '-I -m meshforge.agent.winservice setup --result-file "' + ResultFile +
    '" --message-file "' + MessageFile + '"';
  WizardForm.StatusLabel.Caption :=
    'Starting MeshForge and checking that it runs (up to a minute)...';
  Log('Setup step: ' + SetupPython() + ' ' + Params);
  if not Exec(SetupPython(), Params, ExpandConstant('{app}'), SW_HIDE,
      ewWaitUntilTerminated, SetupResultCode) then
  begin
    SetupMessage := 'The MeshForge setup step could not run (' +
      SysErrorMessage(SetupResultCode) + '). MeshForge is NOT running.';
    SetupResultCode := -1;
  end
  else if LoadStringsFromFile(MessageFile, Lines) then
  begin
    SetupMessage := '';
    for I := 0 to GetArrayLength(Lines) - 1 do
    begin
      Log('setup: ' + Lines[I]);
      SetupMessage := SetupMessage + Lines[I] + #13#10;
    end;
  end
  else
    SetupMessage := Format('The MeshForge setup step ended with exit code %d and no ' +
      'message; see %s. MeshForge may NOT be running.', [SetupResultCode, ResultFile]);
  Log(Format('Setup step exit code: %d', [SetupResultCode]));
  SetupRan := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    RunSetupStep();
    if (SetupResultCode <> 0) and not WizardSilent() then
      MsgBox(SetupMessage, mbError, MB_OK);
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and SetupRan then
  begin
    WizardForm.FinishedLabel.WordWrap := True;
    WizardForm.FinishedLabel.Caption := SetupMessage;
  end;
end;

{ Silent installs report the setup step's verdict through the exit code. }
function GetCustomSetupExitCode(): Integer;
begin
  Result := 0;
  if SetupRan and (SetupResultCode <> 0) then
  begin
    if SetupResultCode < 0 then
      Result := 99
    else
      Result := 100 + SetupResultCode;
  end;
end;

{ ------------------------------------------------------------- uninstall }
function InitializeUninstall(): Boolean;
var
  I: Integer;
begin
  RemoveData := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/REMOVEDATA') = 0 then
      RemoveData := True;
  if (not RemoveData) and (not UninstallSilent()) then
    RemoveData := MsgBox('Also remove all MeshForge data from this computer?' + #13#10#13#10 +
      'This deletes this computer''s node identity (it would join ' +
      'as a NEW node if MeshForge is installed again), its caches and logs in ' +
      ExpandConstant('{commonappdata}') + '\MeshForge.' + #13#10#13#10 +
      'Choose No to keep them (recommended).',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Params, Python: String;
  Code: Integer;
begin
  if CurUninstallStep <> usUninstall then
    exit;
  Python := ExpandConstant('{app}\{#VersionDir}\python\python.exe');
  Params := '-I -m meshforge.agent.winservice teardown --result-file "' +
    ExpandConstant('{%TEMP}') + '\meshforge-uninstall.json"';
  if RemoveData then
    Params := Params + ' --remove-data';
  Log('Teardown: ' + Python + ' ' + Params);
  if not FileExists(Python) then
  begin
    Log('No ' + Python + ': the service cannot be removed by its own helper');
    if not UninstallSilent() then
      MsgBox('MeshForge''s files are incomplete, so its service may stay installed. ' +
        'In an administrator prompt run: sc.exe delete {#ServiceName}', mbError, MB_OK);
    exit;
  end;
  if (not Exec(Python, Params, ExpandConstant('{app}'), SW_HIDE,
      ewWaitUntilTerminated, Code)) or (Code <> 0) then
  begin
    Log(Format('Teardown exit code: %d', [Code]));
    if not UninstallSilent() then
      MsgBox('MeshForge could not remove its service completely (exit code ' +
        IntToStr(Code) + '). See ' + ExpandConstant('{%TEMP}') +
        '\meshforge-uninstall.json; an administrator can run: sc.exe delete ' +
        '{#ServiceName}', mbError, MB_OK);
  end
  else
    Log('Teardown finished');
end;
