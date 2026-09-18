# Xyrus setup - run install.bat (or: powershell -ExecutionPolicy Bypass -File setup.ps1)
# Creates the Python env, installs packages, downloads the offline speech model,
# and adds Xyrus to Windows startup. Safe to re-run.
param([switch]$NoStartup)   # -NoStartup: don't add to Windows startup, don't launch
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
Write-Host "[1/5] Python: $(& $py --version)"

# 2. venv + packages
if (-not (Test-Path "$here\venv\Scripts\python.exe")) {
    Write-Host "[2/5] creating virtual environment..."
    & $py -m venv "$here\venv"
} else {
    Write-Host "[2/5] virtual environment exists"
}
$venvPy = "$here\venv\Scripts\python.exe"
Write-Host "      installing packages (this takes a minute)..."
& $venvPy -m pip install --quiet --disable-pip-version-check -r "$here\requirements.txt"

# 3. speech model
if (-not (Test-Path "$here\model\am\final.mdl")) {
    Write-Host "[3/5] downloading offline speech model (~40 MB)..."
    $zip = "$here\model.zip"
    Invoke-WebRequest -Uri 'https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip' -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $here -Force
    if (Test-Path "$here\model") { Remove-Item "$here\model" -Recurse -Force }
    Rename-Item "$here\vosk-model-small-en-us-0.15" "$here\model"
    Remove-Item $zip
} else {
    Write-Host "[3/5] speech model present"
}

# 4. icon + shortcuts (the app's own code builds the startup shortcut, so it's identical to the in-app toggle)
Write-Host "[4/5] creating shortcuts..."
$pyCmd = "import sys; sys.path.insert(0, r'$here'); import config; config.load(); import arc; arc.ensure_icon_file()"
if (-not $NoStartup) { $pyCmd += "; arc.set_autostart(True)" }
& $venvPy -c $pyCmd
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut("$here\Xyrus.lnk")
$lnk.TargetPath = "$here\venv\Scripts\pythonw.exe"
$lnk.Arguments = "`"$here\arc.py`""
$lnk.WorkingDirectory = $here
$lnk.IconLocation = "$here\xyrus.ico"
$lnk.Description = 'Xyrus voice assistant'
$lnk.Save()
if (-not $NoStartup) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    Copy-Item "$here\Xyrus.lnk" "$desktop\Xyrus.lnk" -Force
}

# 5. quick self-check + launch
Write-Host "[5/5] checking microphone..."
& $venvPy -c "import sounddevice as sd; d = sd.query_devices(kind='input'); print('      mic:', d['name'])"
& $venvPy -c "import sys; sys.path.insert(0, r'$here'); import config, arc, ui; print('      code ok, model ok' if arc.MODEL_DIR.exists() else 'model missing')"
Write-Host ""
if ($NoStartup) {
    Write-Host "Done. Double-click Xyrus.lnk to start it." -ForegroundColor Green
} else {
    Write-Host "Done. Xyrus will start with Windows. Launching it now..." -ForegroundColor Green
    Start-Process -FilePath "$here\venv\Scripts\pythonw.exe" -ArgumentList "`"$here\arc.py`"" -WorkingDirectory $here
    Write-Host 'Say: "Xyrus, what can you do"'
}
