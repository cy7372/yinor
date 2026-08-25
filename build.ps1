#Requires -Version 7
<#
  Build yinor: PyInstaller exe + Inno Setup installer → Output/.

  Pipeline:
    1. Read version from yinor/__init__.py (single source of truth)
    2. Generate icon (if stale) + version_info.txt
    3. PyInstaller (via .venv) → dist/yinor.exe
    4. Inno Setup → Output/yinor-Setup-v<ver>.exe

  Usage:
    pwsh ./build.ps1                # full build (exe + installer)
    pwsh ./build.ps1 -Clean         # clean build/dist/Output first
    pwsh ./build.ps1 -ExeOnly       # skip Inno Setup
    pwsh ./build.ps1 -Run           # build exe then smoke-launch it
#>
[CmdletBinding()]
param(
    [switch] $Clean,
    [switch] $ExeOnly,
    [switch] $Run,
    [string] $Version,
    [string] $ReleaseDir
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$py = "$root\.venv\Scripts\python.exe"

Write-Host "==> yinor build" -ForegroundColor Cyan

# ── Resolve version from yinor/__init__.py（single source of truth）────
$initPy = Get-Content "$root/yinor/__init__.py" -Raw
if ($initPy -notmatch '(?m)^__version__\s*=\s*"([^"]+)"') {
    throw "Could not parse __version__ from yinor/__init__.py"
}
$appVer = $Matches[1]
if (-not $Version) { $Version = $appVer }
$fourSeg = "$Version.0"   # Windows exe 版本资源要求四段
Write-Host "    Version: $Version"

# ── 0. Sanity checks ─────────────────────────────────────────────────
foreach ($f in @("yinor.spec", "yinor.iss", "launcher.py", ".env.example")) {
    if (-not (Test-Path "$root/$f")) { throw "Missing required file: $f" }
}
if (-not (Test-Path $py)) { throw "venv python not found: $py (run: uv venv .venv && uv pip install -r requirements.txt pyinstaller)" }

# ── 1. Clean ─────────────────────────────────────────────────────────
if ($Clean) {
    Write-Host "==> Cleaning build/ dist/ Output/" -ForegroundColor Yellow
    Remove-Item -Recurse -Force "$root/build", "$root/dist", "$root/Output" -ErrorAction SilentlyContinue
}

# ── 2. Icon (if stale) ───────────────────────────────────────────────
$ico = "$root/packaging/yinor.ico"
$needIcon = -not (Test-Path $ico)
if ((Test-Path $ico) -and (Test-Path "$root/packaging/make_icon.py") -and (Test-Path "$root/yinor/static/favicon-32.png")) {
    if ((Get-Item "$root/yinor/static/favicon-32.png").LastWriteTime -gt (Get-Item $ico).LastWriteTime) { $needIcon = $true }
}
if ($needIcon) {
    Write-Host "==> Generating icon" -ForegroundColor Cyan
    & $py "$root/packaging/make_icon.py"
    if ($LASTEXITCODE -ne 0) { throw "Icon generation failed" }
}

# ── 3. version_info.txt (VS_VERSION_INFO) ────────────────────────────
$vp = $Version.Split('.')
$vFile = "$root/packaging/version_info.txt"
@"
# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($($vp[0]), $($vp[1]), $($vp[2]), 0),
    prodvers=($($vp[0]), $($vp[1]), $($vp[2]), 0),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable(u'040904B0', [
        StringStruct(u'CompanyName', u'yinor'),
        StringStruct(u'FileDescription', u'yinor Memory System'),
        StringStruct(u'FileVersion', u'$fourSeg'),
        StringStruct(u'InternalName', u'yinor'),
        StringStruct(u'OriginalFilename', u'yinor.exe'),
        StringStruct(u'ProductName', u'yinor'),
        StringStruct(u'ProductVersion', u'$Version')])]),
    VarFileInfo([VarStruct(u'Translation', [0x409, 1200])])
  ]
)
"@ | Set-Content -Path $vFile -Encoding UTF8 -NoNewline
Write-Host "    version_info.txt -> $fourSeg" -ForegroundColor DarkGray

# ── 4. PyInstaller ───────────────────────────────────────────────────
Write-Host "==> PyInstaller" -ForegroundColor Cyan
if (-not (& $py -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('PyInstaller') else 1)")) {
    Write-Host "    installing pyinstaller (build-only dep)" -ForegroundColor DarkGray
    & $py -m pip install -q pyinstaller 2>$null
    if ($LASTEXITCODE -ne 0) { uv pip install -q --python $py pyinstaller }
}
& $py -m PyInstaller --noconfirm --log-level WARN "$root/yinor.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }
$exe = "$root/dist/yinor.exe"
if (-not (Test-Path $exe)) { throw "Build finished but $exe not produced" }
$exeSizeMB = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "    exe: $exeSizeMB MB" -ForegroundColor Green

if ($Run) {
    Write-Host "==> Smoke launch (10s)" -ForegroundColor Yellow
    $proc = Start-Process $exe -PassThru
    Start-Sleep 10
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
}

if ($ExeOnly) { Write-Host "`nDone: $exe" -ForegroundColor Green; exit 0 }

# ── 5. Inno Setup installer ──────────────────────────────────────────
Write-Host "==> Inno Setup" -ForegroundColor Cyan
$isccCandidates = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
)
$iscc = $null
foreach ($p in $isccCandidates) { if (Test-Path $p) { $iscc = $p; break } }
if (-not $iscc) {
    Write-Warning "Inno Setup (ISCC.exe) not found; skipping installer. Install via: winget install JRSoftware.InnoSetup"
    Write-Host "`nDone: $exe" -ForegroundColor Green
    exit 0
}

# 注入版本号到 iss（覆盖手写值，保持单一来源）
$issPath = "$root/yinor.iss"
$issRaw = Get-Content $issPath -Raw
$issPatched = [regex]::Replace($issRaw, '#define\s+MyAppVersion\s+"[^"]*"', "#define MyAppVersion `"$Version`"")
Set-Content -Path $issPath -Value $issPatched -NoNewline

Remove-Item -Recurse -Force "$root/Output" -ErrorAction SilentlyContinue
& $iscc /Qp "$issPath"
if ($LASTEXITCODE -ne 0) { throw "ISCC failed (exit $LASTEXITCODE)" }

$setupV = "$root/Output/yinor-Setup-v$Version.exe"
Rename-Item "$root/Output/yinor-Setup.exe" $setupV -Force
$setup = Get-Item $setupV
if (-not $setup) { throw "Installer not produced in Output/" }
$setupMB = [math]::Round($setup.Length / 1MB, 1)
Write-Host "    installer: $($setup.Name) ($setupMB MB)" -ForegroundColor Green

if ($ReleaseDir) {
    New-Item $ReleaseDir -ItemType Directory -Force | Out-Null
    Copy-Item $setup.FullName $ReleaseDir -Force
    Write-Host "    copied -> $ReleaseDir"
}

Write-Host "`nDone: $($setup.FullName)" -ForegroundColor Green
