$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
if (-not (Test-Path '.build-venv\Scripts\python.exe')) {
    py -3.12 -m venv .build-venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 venv failed' }
}
$Python = (Resolve-Path '.build-venv\Scripts\python.exe').Path
& $Python -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
Push-Location source\taskboard
try {
    & $Python -m unittest -v test_server test_startup_data test_features
    if ($LASTEXITCODE -ne 0) { throw 'Regression tests failed' }
    & $Python test_storage_v13.py
    if ($LASTEXITCODE -ne 0) { throw 'Storage regression failed' }
} finally { Pop-Location }
& $Python -m PyInstaller --noconfirm --clean --windowed --onedir --name TaskBoard --icon source\taskboard\taskboard.ico --add-data 'source\taskboard\web;web' --add-data 'source\taskboard\taskboard.ico;.' --collect-all openpyxl source\taskboard\launcher.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
# Probe the actual frozen executable, not the Python source, with isolated data.
& $Python scripts\smoke-packaged.py dist\TaskBoard\TaskBoard.exe
if ($LASTEXITCODE -ne 0) { throw 'Packaged application smoke test failed' }
$Compiler = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $Compiler) { $Compiler = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe' }
if (-not (Test-Path $Compiler)) { throw 'Install Inno Setup 6 and add ISCC.exe to PATH.' }
& $Compiler source\installer\TaskBoard.iss
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed' }
Get-FileHash dist\Setup_TaskBoard_2.1.0.exe -Algorithm SHA256 | Format-List
