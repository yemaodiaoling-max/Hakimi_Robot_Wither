$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvRoot = Split-Path -Parent $projectRoot
$python = Join-Path $venvRoot '.venv\Scripts\python.exe'
$ensureScript = Join-Path $PSScriptRoot 'ensure_pet_running.py'
Set-Location -LiteralPath $projectRoot
$env:PYTHONUTF8 = '1'
& $python $ensureScript --force --direct
exit $LASTEXITCODE
