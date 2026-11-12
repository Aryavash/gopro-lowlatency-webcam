<#
.SYNOPSIS
    One-step installer for the low-latency GoPro webcam (Windows 10/11).

.DESCRIPTION
    Installs ffmpeg, Python and OBS Studio (for its virtual camera) with winget
    if they are missing, creates a private Python environment next to this
    script, adds Startup + Desktop shortcuts and starts the webcam in the tray.

    Run it with:  powershell -ExecutionPolicy Bypass -File install.ps1

.PARAMETER NoStartup          Don't start automatically when you log in.
.PARAMETER NoDesktopShortcut  Don't create a Desktop shortcut.
.PARAMETER NoLaunch           Don't start the webcam after installing.
#>
param(
    [switch]$NoStartup,
    [switch]$NoDesktopShortcut,
    [switch]$NoLaunch
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$OBS_VCAM_CLSID = '{A3FCE0F5-3493-419F-958A-ABA1250EC20B}'
$ShortcutName = 'GoPro Webcam (low latency).lnk'

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok($msg) { Write-Host "    $msg" -ForegroundColor Green }

function Install-WithWinget($id) {
    winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements --disable-interactivity | Out-Host
    return $LASTEXITCODE
}

function Find-Python {
    $candidates = @()
    foreach ($v in '313', '312', '311', '310') {
        $candidates += "$env:LOCALAPPDATA\Programs\Python\Python$v\python.exe"
        $candidates += "$env:ProgramFiles\Python$v\python.exe"
    }
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $p = & py -3 -c "import sys; print(sys.executable if sys.version_info >= (3, 10) else '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $p) { return $p.Trim() }
    }
    return $null  # the 'python' on PATH is often just the Microsoft Store stub, so we don't trust it
}

function Find-FFmpeg {
    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) { return $true }
    $hit = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Directory -Filter 'Gyan.FFmpeg*' -ErrorAction SilentlyContinue |
        Get-ChildItem -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    return [bool]$hit
}

function New-Shortcut($path, $target, $arguments, $icon) {
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($path)
    $lnk.TargetPath = $target
    $lnk.Arguments = $arguments
    $lnk.WorkingDirectory = $here
    $lnk.IconLocation = $icon
    $lnk.Description = 'Low-latency GoPro webcam'
    $lnk.Save()
}

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "winget was not found. Install 'App Installer' from the Microsoft Store, then run this again."
}

# ---------------------------------------------------------------- ffmpeg
Step 'ffmpeg'
if (Find-FFmpeg) { Ok 'already installed' }
else {
    Install-WithWinget 'Gyan.FFmpeg' | Out-Null
    if (-not (Find-FFmpeg)) { throw 'ffmpeg install failed. Try: winget install Gyan.FFmpeg' }
    Ok 'installed'
}

# ---------------------------------------------------------------- Python
Step 'Python 3.10+'
$python = Find-Python
if ($python) { Ok "found $python" }
else {
    Install-WithWinget 'Python.Python.3.12' | Out-Null
    $python = Find-Python
    if (-not $python) { throw 'Python install failed. Install Python 3.12 from python.org and run this again.' }
    Ok "installed $python"
}

# ---------------------------------------------------------------- OBS virtual camera
Step 'OBS Virtual Camera'
if (Test-Path "Registry::HKEY_CLASSES_ROOT\CLSID\$OBS_VCAM_CLSID") { Ok 'already registered' }
else {
    $code = Install-WithWinget 'OBSProject.OBSStudio'
    if (-not (Test-Path "Registry::HKEY_CLASSES_ROOT\CLSID\$OBS_VCAM_CLSID")) {
        Write-Host @"

    OBS failed to install (winget exit code $code).
    If it said 'files ... are currently in use', another program has OBS's
    game-capture hook loaded - usually Steam or a running game. Quit Steam
    completely (tray icon > Exit Steam) and any games, then run this again.
"@ -ForegroundColor Yellow
        exit 1
    }
    Ok 'installed'
}

# ---------------------------------------------------------------- venv
Step 'Python environment'
$venvPy = Join-Path $here '.venv\Scripts\python.exe'
$venvPyw = Join-Path $here '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $venvPy)) { & $python -m venv (Join-Path $here '.venv') }
& $venvPy -m pip install --quiet --disable-pip-version-check --upgrade pip
& $venvPy -m pip install --quiet --disable-pip-version-check -r (Join-Path $here 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
$icon = Join-Path $here 'icon.ico'
& $venvPy (Join-Path $here 'gopro_cam.py') --write-icon $icon
Ok 'ready'

# ---------------------------------------------------------------- shortcuts
Step 'Shortcuts'
$args_ = "`"$(Join-Path $here 'gopro_cam.py')`" --background"
$startupLnk = Join-Path ([Environment]::GetFolderPath('Startup')) $ShortcutName
$desktopLnk = Join-Path ([Environment]::GetFolderPath('Desktop')) $ShortcutName
if (-not $NoStartup) { New-Shortcut $startupLnk $venvPyw $args_ $icon; Ok 'starts automatically at login' }
if (-not $NoDesktopShortcut) { New-Shortcut $desktopLnk $venvPyw $args_ $icon; Ok 'Desktop shortcut created' }

# ---------------------------------------------------------------- launch
if (-not $NoLaunch) {
    Step 'Starting'
    # stop an older copy so the new version takes over
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'gopro_cam\.py' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
    Start-Process $venvPyw -ArgumentList $args_ -WorkingDirectory $here
    Ok 'running - look for the camera icon in the system tray'
}

Write-Host @"

Done! Next:
  1. Fully quit Discord/Zoom/Teams once (tray icon > Quit) and reopen it.
  2. Pick 'OBS Virtual Camera' as your camera.
  3. Plug in the GoPro and turn it on - the picture appears in a few seconds.
"@ -ForegroundColor Green
