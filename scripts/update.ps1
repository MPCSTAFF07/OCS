param([Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{40}$')][string]$Ref)
$ErrorActionPreference = 'Stop'
$ocsRoot = Split-Path $PSScriptRoot -Parent
Set-Location $ocsRoot
$listener = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort 8790 -State Listen -ErrorAction SilentlyContinue
if ($listener) { throw 'Stop sharing, then close OCS with Ctrl+C in the START.cmd window before updating.' }
$files = @('server.py', 'shared_video.py', 'publisher.py', 'caster-share.lua', 'web/dock.html', 'web/dock.js', 'web/style.css', 'web/av-controls.js')
$stage = Join-Path ([IO.Path]::GetTempPath()) ('ocs-update-' + [guid]::NewGuid().ToString('N'))
$backup = Join-Path $ocsRoot ('runtime/update-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$changed = @()
try {
    foreach ($file in $files) {
        $destination = Join-Path $stage $file
        New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force | Out-Null
        Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/MPCSTAFF07/OCS/$Ref/$file" -OutFile $destination
    }
    foreach ($file in $files) {
        $destination = Join-Path $ocsRoot $file
        $saved = Join-Path $backup $file
        if (Test-Path $destination) {
            New-Item -ItemType Directory -Path (Split-Path $saved -Parent) -Force | Out-Null
            Copy-Item $destination $saved
        }
        $changed += $file
        Copy-Item (Join-Path $stage $file) $destination -Force
    }
    Write-Host 'OCS updated. Connection keys and the Python environment were retained.'
    Write-Host 'Reload caster-share.lua in OBS, then run START.cmd and refresh the management page.'
    Write-Host 'For sharper video, start with 1920x1080 / 30 FPS / 8000 kbps. Keep your selected audio sources.'
} catch {
    foreach ($file in $changed) {
        $saved = Join-Path $backup $file
        $destination = Join-Path $ocsRoot $file
        if (Test-Path $saved) { Copy-Item $saved $destination -Force }
        else { Remove-Item $destination -ErrorAction SilentlyContinue }
    }
    throw
} finally {
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
}

