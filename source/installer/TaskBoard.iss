#define MyAppName "任务看板系统"
#define MyAppVersion "2.0.1"
#define MyAppExeName "TaskBoard.exe"

[Setup]
AppId={{A71D03F1-966D-49D2-A264-39F7BD20E097}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\TaskBoard
DefaultGroupName=任务看板系统
OutputDir=..\..\dist
OutputBaseFilename=Setup_TaskBoard_2.0.1
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=..\taskboard\taskboard.ico
DisableProgramGroupPage=no
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts"

[Files]
Source: "..\..\dist\TaskBoard\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "Data\*,instance.lock,.instance.json"
Source: "..\..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\任务看板系统"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 任务看板系统"; Filename: "{uninstallexe}"
Name: "{autodesktop}\任务看板系统"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch TaskBoard"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: dirifempty; Name: "{app}"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent) then
    if DirExists(ExpandConstant('{app}\Data')) then
      MsgBox('Task data in the Data folder has been preserved.', mbInformation, MB_OK);
end;
