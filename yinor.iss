; yinor Inno Setup script（参考 cyRouter 同款模式）
; Build:  build.ps1（自动注入版本号）或 ISCC.exe yinor.iss
; Output: Output\yinor-Setup-v<VERSION>.exe
;
; Design choices:
;   - PrivilegesRequired=lowest → 安装到 %APPDATA%，免管理员权限
;   - .env 与 data/（记忆库）跨 卸载+重装 保留（卸载只删 exe）
;   - 升级 = 直接运行新版 Setup 覆盖安装，数据原地不动
;   - 卸载前 taskkill 防止 exe 文件被占用

#define MyAppName "yinor"
; NOTE: MyAppVersion is overwritten by build.ps1 on every build, sourced
;       from yinor/__init__.py __version__. Do not edit by hand.
#define MyAppVersion "0.2.0"
#define MyAppPublisher "yinor"
#define MyAppExeName "yinor.exe"
#define MyAppURL "https://github.com/cy7372/yinor"

[Setup]
AppId={{8B2D5A71-4C6E-4F2A-9D3B-7E5A6C4B1D09}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={userappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=Output
OutputBaseFilename=yinor-Setup
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式:"

[Files]
Source: "dist\yinor.exe"; DestDir: "{app}"; Flags: ignoreversion restartreplace
; 配置模板：装到安装目录，用户复制/改名为 .env 后填写 key（已有的 .env 不覆盖）
Source: ".env.example"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
; data/（记忆库）运行时自建；显式声明以便卸载时保留（uninsneveruninstall）
Name: "{app}\data"; Flags: uninsneveruninstall

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 yinor"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 卸载前杀进程，防文件占用
Filename: "taskkill"; Parameters: "/F /IM {#MyAppExeName}"; Flags: runhidden; RunOnceId: "KillApp"

[UninstallDelete]
; 只删程序本体；.env 与 data/（用户记忆库）保留——见 [Dirs] uninsneveruninstall
Type: files; Name: "{app}\{#MyAppExeName}"

[Code]
function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
begin
  Exec('taskkill', '/F /IM {#MyAppExeName}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := True;
end;
