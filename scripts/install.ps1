$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw 'Install Python 3.12 (64-bit) from python.org, then run INSTALL.cmd again.'
}
$selected = $null
foreach ($version in @('-3.12', '-3.13', '-3.11')) {
    $ErrorActionPreference = 'Continue'
    & py $version -c 'import sys; assert sys.maxsize > 2**32' 2>$null
    $probeExit = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($probeExit -eq 0) { $selected = $version; break }
}
if (-not $selected) { throw 'Python 3.11 / 3.12 / 3.13 (64-bit) is required. Recommended: Python 3.12.' }
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    & py $selected -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}
& '.\.venv\Scripts\python.exe' -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip update failed.' }
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check your network.' }
New-Item -ItemType Directory -Path runtime -Force | Out-Null
Write-Host 'Installed. Next: START.cmd, add caster-share.lua to OBS, and enter public relay settings in the dock.'
