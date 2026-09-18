# Xyrus setup - run install.bat (or: powershell -ExecutionPolicy Bypass -File setup.ps1)
# Creates the Python env, installs packages, puts the offline speech models in place, and adds Xyrus to
# Windows startup. Safe to re-run: it skips whatever is already there.
#   -NoStartup            don't add to Windows startup, no desktop shortcut, don't launch it
#   -ModelSource <dir>    copy the Vosk speech model from this folder instead of downloading it (~40 MB)
#   -WhisperSource <dir>  copy the Whisper model (the folder with model.bin) instead of downloading it (~480 MB)
#   -SkipWhisper          don't download Whisper now (song names then use the small model; re-run later)
#   -SkipPip              don't run pip (offline install: venv\ was copied from another PC with the packages in it)
param(
    [switch]$NoStartup,
    [string]$ModelSource = "",
    [string]$WhisperSource = "",
    [switch]$SkipWhisper,
    [switch]$SkipPip
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

Write-Host ""
Write-Host "=== Xyrus setup ===" -ForegroundColor Magenta
Write-Host "folder: $here"

# 1. Python
$py = $null
foreach ($cmd in @('python', 'py')) {
    try {
        $v = & $cmd --version 2>&1
        if ($v -match 'Python 3\.(\d+)') {
            if ([int]$Matches[1] -ge 10) { $py = $cmd; break }
        }
    } catch {}
}
if (-not $py) {
    Write-Host "Python 3.10+ not found." -ForegroundColor Red
    Write-Host "Install it from https://www.python.org/downloads/  (tick 'Add python.exe to PATH'), then run this again."
    exit 1
}
Write-Host "[1/7] Python: $(& $py --version)"

# 2. venv + packages
if (-not (Test-Path "$here\venv\Scripts\python.exe")) {
    Write-Host "[2/7] creating virtual environment..."
    & $py -m venv "$here\venv"
} else {
    Write-Host "[2/7] virtual environment exists"
}
$venvPy = "$here\venv\Scripts\python.exe"
if ($SkipPip) {
    Write-Host "      packages: pip skipped (-SkipPip)"
    & $venvPy -c "import vosk, sounddevice, comtypes, pystray, customtkinter, psutil, pycaw, win32com, keyboard"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "The packages are not in venv\ - run setup without -SkipPip (needs the internet)." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "      installing packages (this takes a few minutes the first time)..."
    & $venvPy -m pip install --quiet --disable-pip-version-check -r "$here\requirements.txt"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Package install failed - check the internet connection and run this again." -ForegroundColor Red
        exit 1
    }
}

# 3. Vosk speech model (wake word + commands)
function Find-VoskModel([string]$dir) {
    if (-not $dir -or -not (Test-Path $dir)) { return $null }
    if (Test-Path (Join-Path $dir 'am\final.mdl')) { return (Resolve-Path $dir).Path }
    foreach ($sub in Get-ChildItem -Path $dir -Directory -ErrorAction SilentlyContinue) {
        if (Test-Path (Join-Path $sub.FullName 'am\final.mdl')) { return $sub.FullName }
    }
    return $null
}
if (Test-Path "$here\model\am\final.mdl") {
    Write-Host "[3/7] speech model present"
} elseif ($ModelSource) {
    $src = Find-VoskModel $ModelSource
    if (-not $src) {
        Write-Host "No Vosk model (am\final.mdl) in $ModelSource" -ForegroundColor Red
        exit 1
    }
    Write-Host "[3/7] copying the speech model from $src ..."
    if (Test-Path "$here\model") { Remove-Item "$here\model" -Recurse -Force }
    Copy-Item -Path $src -Destination "$here\model" -Recurse
} else {
    Write-Host "[3/7] downloading offline speech model (~40 MB)..."
    $zip = "$here\model.zip"
    Invoke-WebRequest -Uri 'https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip' -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $here -Force
    if (Test-Path "$here\model") { Remove-Item "$here\model" -Recurse -Force }
    Rename-Item "$here\vosk-model-small-en-us-0.15" "$here\model"
    Remove-Item $zip
}

# 4. Whisper model (song names and other free text; Xyrus works without it)
$wdir = "$here\models\whisper-small"
if (Test-Path "$wdir\model.bin") {
    Write-Host "[4/7] Whisper model present"
} elseif ($WhisperSource) {
    if (-not (Test-Path (Join-Path $WhisperSource 'model.bin'))) {
        Write-Host "No Whisper model (model.bin) in $WhisperSource" -ForegroundColor Red
        exit 1
    }
    Write-Host "[4/7] copying the Whisper model from $WhisperSource ..."
    New-Item -ItemType Directory -Force "$here\models" | Out-Null
    if (Test-Path $wdir) { Remove-Item $wdir -Recurse -Force }
    Copy-Item -Path $WhisperSource -Destination $wdir -Recurse
} elseif ($SkipWhisper) {
    Write-Host "[4/7] Whisper skipped (-SkipWhisper) - run setup again later for better song names"
} else {
    Write-Host "[4/7] downloading the Whisper model for song names (~480 MB, one time)..."
    New-Item -ItemType Directory -Force "$here\models" | Out-Null
    & $venvPy -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-small', local_dir=r'$wdir')"
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path "$wdir\model.bin")) {
        Write-Host "      Whisper download failed - Xyrus still works; run setup again later for better song names." -ForegroundColor Yellow
    }
}

# 5. data folder (calendar, memories)
New-Item -ItemType Directory -Force "$here\data" | Out-Null
Write-Host "[5/7] data folder ready"

# 6. icon + shortcuts - the app's own code writes them, so they match the in-app "Start with Windows" switch.
#    -NoStartup only makes Xyrus.lnk in this folder (never touches the desktop or the Startup folder).
Write-Host "[6/7] creating shortcuts..."
$pyCmd = "import sys; sys.path.insert(0, r'$here'); from xyrus import tray, autostart; tray.ensure_icon_file()"
if ($NoStartup) {
    $pyCmd += "; autostart.create_desktop_shortcut([autostart.BASE])"
} else {
    $pyCmd += "; autostart.create_desktop_shortcut(); autostart.set_enabled(True)"
}
& $venvPy -c $pyCmd

# 7. checks + launch
Write-Host "[7/7] checking..."
& $venvPy -c "import sys; sys.path.insert(0, r'$here'); from xyrus import audio; m = audio.list_mics(); print('      mic:', m[0].name if m else 'none found - plug one in')"
& $venvPy "$here\arc.py" --selftest
if ($LASTEXITCODE -eq 0) {
    Write-Host "      self-test passed"
} else {
    Write-Host "      self-test failed - see the lines above and arc.log (Xyrus may still run)" -ForegroundColor Yellow
}
& $venvPy -c "import sys; sys.path.insert(0, r'$here')`ntry:`n    import hearing; hearing.load_async(); ok = hearing.available(wait=180)`n    print('      whisper:', 'ready' if ok else 'not available (' + str(hearing.error) + ')')`nexcept Exception as e:`n    print('      whisper: not available (' + repr(e) + ')')"
Write-Host ""
if ($NoStartup) {
    Write-Host "Done. Double-click Xyrus.lnk in this folder to start it." -ForegroundColor Green
} else {
    Write-Host "Done. Xyrus will start with Windows. Launching it now..." -ForegroundColor Green
    Start-Process -FilePath "$here\venv\Scripts\pythonw.exe" -ArgumentList "`"$here\arc.py`"" -WorkingDirectory $here
    Write-Host 'Say: "Xyrus, what can you do"'
}
