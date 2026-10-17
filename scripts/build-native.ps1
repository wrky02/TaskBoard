param([switch]$SkipInstaller)
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
if (-not (Test-Path '.native-build-venv\Scripts\python.exe')) {
    python -m venv .native-build-venv
    if ($LASTEXITCODE -ne 0) { throw 'Python venv failed' }
}
$Python = (Resolve-Path '.native-build-venv\Scripts\python.exe').Path
& $Python -m pip install -r requirements-native-build.txt
if ($LASTEXITCODE -ne 0) { throw 'Native engine dependencies failed' }
Push-Location source\taskboard
try {
    & $Python -m unittest -v test_native_worker test_server test_startup_data test_features
    if ($LASTEXITCODE -ne 0) { throw 'Application regression failed' }
    & $Python test_storage_v13.py
    if ($LASTEXITCODE -ne 0) { throw 'Legacy data regression failed' }
} finally { Pop-Location }
& $Python -m PyInstaller --noconfirm --clean --console --onedir --name TaskBoard.Engine --icon source\taskboard\taskboard.ico --collect-all openpyxl --exclude-module server --exclude-module launcher --exclude-module desktop --exclude-module webbrowser --exclude-module http.server source\taskboard\native_worker.py
if ($LASTEXITCODE -ne 0) { throw 'Native engine build failed' }
dotnet publish native/TaskBoard/TaskBoard.csproj -c Release -r win-x64 --self-contained true -p:PublishSingleFile=false -o dist/TaskBoardNative
if ($LASTEXITCODE -ne 0) { throw 'WPF publish failed' }
New-Item -ItemType Directory -Force dist/TaskBoardNative/engine | Out-Null
Copy-Item dist/TaskBoard.Engine/* dist/TaskBoardNative/engine -Recurse -Force
$forbidden = Get-ChildItem dist/TaskBoardNative -Recurse -File | Where-Object { $_.Name -match 'WebView2|QtWebEngine|libcef|chrome_elf|msedge|electron' }
if ($forbidden) { throw 'Browser component detected in native package' }
if (-not $SkipInstaller) { & "$PSScriptRoot/compile-native-installer.ps1" }
