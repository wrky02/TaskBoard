$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
$Compiler = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $Compiler) { $Compiler = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe' }
if (-not (Test-Path $Compiler)) { throw 'Install Inno Setup 6.' }
& $Compiler source/installer/TaskBoardNative.iss
if ($LASTEXITCODE -ne 0) { throw 'Native installer build failed' }
