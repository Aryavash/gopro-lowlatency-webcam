<#
.SYNOPSIS
    Removes the low-latency GoPro webcam's shortcuts and stops it.

.DESCRIPTION
    ffmpeg, Python and OBS are left installed because other programs may use
    them. Remove them from Settings > Apps if you don't need them.

    Run it with:  powershell -ExecutionPolicy Bypass -File uninstall.ps1

.PARAMETER RemoveEnvironment  Also delete the .venv folder and log files.
#>
param([switch]$RemoveEnvironment)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ShortcutName = 'GoPro Webcam (low latency).lnk'

Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'gopro_cam\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host 'Stopped the webcam.'

foreach ($dir in [Environment]::GetFolderPath('Startup'), [Environment]::GetFolderPath('Desktop')) {
    Remove-Item (Join-Path $dir $ShortcutName) -ErrorAction SilentlyContinue
}
Write-Host 'Removed Startup and Desktop shortcuts.'

if ($RemoveEnvironment) {
    Remove-Item (Join-Path $here '.venv') -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $here 'gopro-webcam.log*'), (Join-Path $here 'icon.ico') -Force -ErrorAction SilentlyContinue
    Write-Host 'Removed the Python environment and logs.'
}
Write-Host 'Done. You can delete this folder now.'
