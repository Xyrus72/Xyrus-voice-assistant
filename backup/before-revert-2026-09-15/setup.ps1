# Xyrus setup - run by Xyrus-Setup.exe, or by hand: install.bat (powershell -ExecutionPolicy Bypass -File setup.ps1)
# Installs everything Xyrus needs into this folder: Python (when the PC has no usable one), a private Python
# environment with the packages, the offline speech models, the shortcuts, and (unless -NoStartup) start with
# Windows. Safe to re-run: it skips whatever is already there. Needs the internet unless the -*Source options
# point at copies.
#   -NoStartup            no Windows startup entry, no desktop shortcut, don't launch it at the end
#   -CpuOnly              don't use an NVIDIA graphics card even if there is one (small Whisper model on the CPU)
#   -Bangla               also build the Bengali speech model for Bangla song names (NVIDIA card only; downloads
#                         ~6 GB plus ~2 GB of conversion tools and takes 30-60 minutes; the tools are removed after)
#   -ModelSource <dir>    copy the Vosk model from this folder instead of downloading it (~40 MB)
#   -WhisperSource <dir>  copy Whisper models from this folder (it holds whisper-small / whisper-large-v3 /
#                         whisper-large-v3-bn subfolders, like D:\arc\models on the first PC) instead of downloading
#   -SkipWhisper          don't download Whisper now (Xyrus then hears commands with the small Vosk model only)
#   -SkipPip              don't run pip (offline install: venv\ was copied from another PC with the packages in it)
param(
    [switch]$NoStartup,
    [switch]$CpuOnly,
    [switch]$Bangla,
    [string]$ModelSource = "",
    [string]$WhisperSource = "",
    [switch]$SkipWhisper,
    [switch]$SkipPip
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'     # Invoke-WebRequest is many times slower while it draws a progress bar
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
$PyVersion = '3.13.6'                        # the version the pinned packages in requirements.txt were tested on
$Steps = 9

function Step([int]$n, [string]$text) { Write-Host "[$n/$Steps] $text" }
function Fail([string]$text) { Write-Host ""; Write-Host $text -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "=== Xyrus setup ===" -ForegroundColor Magenta
Write-Host "folder: $here"

# 1. graphics card + disk space
$useGpu = $false
if (-not $CpuOnly) {
    $card = Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match 'NVIDIA' } | Select-Object -First 1
    if ($card) { $useGpu = $true }
}
if ($useGpu) {
    Step 1 "NVIDIA graphics card found ($($card.Name)): the large speech model runs on it"
    $needGB = 9
} else {
    Step 1 "no NVIDIA graphics card (or -CpuOnly): the small speech model runs on the processor"
    $needGB = 4
}
if ($Bangla -and $useGpu) { $needGB += 12 }
$driveName = (Get-Item $here).PSDrive.Name
$freeGB = [math]::Round((Get-PSDrive $driveName).Free / 1GB, 1)
Write-Host "      disk ${driveName}: $freeGB GB free, about $needGB GB needed"
if ($freeGB -lt $needGB) {
    Fail "Not enough disk space on ${driveName}: - free some space (or install to another drive) and run this again."
}

# 2. Python 3.11-3.13 (the packages are pinned for 3.13; 3.14 has no wheels for some of them yet)
function Test-Python([string]$exe, [string[]]$pre) {
    try { $v = & $exe @pre --version 2>&1 } catch { return $false }
    return ("$v" -match 'Python 3\.(11|12|13)\.')
}
$pyExe = $null
$pyPre = @()
$known = @("$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
           "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
           "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe")
foreach ($p in $known) {
    if ((Test-Path $p) -and (Test-Python $p @())) { $pyExe = $p; break }
}
if (-not $pyExe) {
    foreach ($try in @(@('py', @('-3.13')), @('py', @('-3.12')), @('py', @('-3.11')), @('python', @()))) {
        if (Test-Python $try[0] $try[1]) { $pyExe = $try[0]; $pyPre = $try[1]; break }
    }
}
if (-not $pyExe) {
    Step 2 "installing Python $PyVersion (just for this user, ~30 MB download)..."
    $inst = Join-Path $env:TEMP "python-$PyVersion-amd64.exe"
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-amd64.exe" -OutFile $inst
    $p = Start-Process -FilePath $inst -Wait -PassThru -ArgumentList @('/quiet', 'InstallAllUsers=0',
        'PrependPath=0', 'Include_launcher=0', 'Include_test=0', 'Shortcuts=0', 'AssociateFiles=0')
    Remove-Item $inst -ErrorAction SilentlyContinue
    $pyExe = "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe"
    if ($p.ExitCode -ne 0 -or -not (Test-Path $pyExe)) {
        Fail "Python could not be installed (exit $($p.ExitCode)). Install Python 3.13 from https://www.python.org/downloads/ and run this again."
    }
} else {
    Step 2 "Python: $(& $pyExe @pyPre --version 2>&1)"
}

# 3. private environment + packages
$venvPy = "$here\venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Step 3 "creating the private Python environment..."
    & $pyExe @pyPre -m venv "$here\venv"
    if ($LASTEXITCODE -ne 0) { Fail "Could not create the Python environment in $here\venv." }
} else {
    Step 3 "Python environment exists"
}
if ($SkipPip) {
    Write-Host "      packages: pip skipped (-SkipPip)"
    & $venvPy -c "import vosk, sounddevice, comtypes, pystray, customtkinter, psutil, pycaw, win32com, keyboard, faster_whisper"
    if ($LASTEXITCODE -ne 0) { Fail "The packages are not in venv\ - run setup without -SkipPip (needs the internet)." }
} else {
    Write-Host "      installing packages (a few minutes the first time)..."
    & $venvPy -m pip install --disable-pip-version-check --no-input -r "$here\requirements.txt"
    if ($LASTEXITCODE -ne 0) { Fail "Package install failed - check the internet connection and run this again." }
    if ($useGpu) {
        Write-Host "      installing the graphics card runtime (~1 GB)..."
        & $venvPy -m pip install --disable-pip-version-check --no-input -r "$here\requirements-gpu.txt"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "      graphics card runtime failed - Xyrus will use the processor instead." -ForegroundColor Yellow
            $useGpu = $false
        }
    }
}

# 4. Vosk speech model (the wake word, and commands until Whisper is loaded)
function Find-VoskModel([string]$dir) {
    if (-not $dir -or -not (Test-Path $dir)) { return $null }
    if (Test-Path (Join-Path $dir 'am\final.mdl')) { return (Resolve-Path $dir).Path }
    foreach ($sub in Get-ChildItem -Path $dir -Directory -ErrorAction SilentlyContinue) {
        if (Test-Path (Join-Path $sub.FullName 'am\final.mdl')) { return $sub.FullName }
    }
    return $null
}
if (Test-Path "$here\model\am\final.mdl") {
    Step 4 "wake-word model present"
} elseif ($ModelSource) {
    $src = Find-VoskModel $ModelSource
    if (-not $src) { Fail "No Vosk model (am\final.mdl) in $ModelSource" }
    Step 4 "copying the wake-word model from $src ..."
    if (Test-Path "$here\model") { Remove-Item "$here\model" -Recurse -Force }
    Copy-Item -Path $src -Destination "$here\model" -Recurse
} else {
    Step 4 "downloading the wake-word model (~40 MB)..."
    $zip = "$here\model.zip"
    Invoke-WebRequest -Uri 'https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip' -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $here -Force
    if (Test-Path "$here\model") { Remove-Item "$here\model" -Recurse -Force }
    Rename-Item "$here\vosk-model-small-en-us-0.15" "$here\model"
    Remove-Item $zip
}

# 5. Whisper models: small always (the processor fallback), large-v3 on an NVIDIA card (the accurate one)
function Get-Whisper([string]$name, [string]$repo, [string]$size) {
    $dir = "$here\models\$name"
    if (Test-Path "$dir\model.bin") { Write-Host "      $name present"; return $true }
    New-Item -ItemType Directory -Force "$here\models" | Out-Null
    if ($WhisperSource -and (Test-Path (Join-Path $WhisperSource "$name\model.bin"))) {
        Write-Host "      copying $name from $WhisperSource ..."
        if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
        Copy-Item -Path (Join-Path $WhisperSource $name) -Destination $dir -Recurse
        return (Test-Path "$dir\model.bin")
    }
    if (-not $repo) { return $false }
    Write-Host "      downloading $name ($size, one time)..."
    # Out-Host: the download's own output must not become part of this function's return value
    & $venvPy -c "from huggingface_hub import snapshot_download; snapshot_download('$repo', local_dir=r'$dir')" | Out-Host
    return (Test-Path "$dir\model.bin")
}
if ($SkipWhisper) {
    Step 5 "Whisper skipped (-SkipWhisper) - run setup again later for accurate hearing"
} else {
    Step 5 "speech models"
    if (-not (Get-Whisper 'whisper-small' 'Systran/faster-whisper-small' '~480 MB')) {
        Write-Host "      whisper-small download failed - run setup again later." -ForegroundColor Yellow
    }
    if ($useGpu -and -not (Get-Whisper 'whisper-large-v3' 'Systran/faster-whisper-large-v3' '~3 GB')) {
        Write-Host "      whisper-large-v3 download failed - Xyrus uses the small model; run setup again later." -ForegroundColor Yellow
    }
}

# 6. Bengali model (optional: -Bangla). There is no ready-made download: the mozilla-ai fine-tune is converted
#    here with throw-away tools (torch on the CPU, transformers, ctranslate2), which are deleted afterwards.
$bnDir = "$here\models\whisper-large-v3-bn"
if (Test-Path "$bnDir\model.bin") {
    Step 6 "Bengali model present"
} elseif ($WhisperSource -and (Get-Whisper 'whisper-large-v3-bn' '' '')) {
    Step 6 "Bengali model copied"
} elseif (-not $Bangla) {
    Step 6 "Bengali model not requested (run setup with -Bangla to add it)"
} elseif (-not $useGpu -or -not (Test-Path "$here\models\whisper-large-v3\tokenizer.json")) {
    Step 6 "Bengali model skipped: it needs an NVIDIA card and the large model"
} else {
    Step 6 "building the Bengali model (~6 GB download, 30-60 minutes)..."
    $cv = "$here\venv-convert"
    $cache = "$here\_download_cache"
    try {
        & $pyExe @pyPre -m venv $cv
        $cvPy = "$cv\Scripts\python.exe"
        & $cvPy -m pip install --disable-pip-version-check --no-input "torch==2.14.0" --index-url https://download.pytorch.org/whl/cpu
        if ($LASTEXITCODE -ne 0) { throw "torch install failed" }
        & $cvPy -m pip install --disable-pip-version-check --no-input "transformers==5.17.0" "ctranslate2==4.8.2" `
            "huggingface_hub==1.31.0" accelerate safetensors "msvc-runtime==14.44.35112"
        if ($LASTEXITCODE -ne 0) { throw "conversion tools install failed" }
        $env:HF_HOME = $cache
        & $cvPy "$here\tools\convert_bangla.py" $bnDir "$here\models\whisper-large-v3\tokenizer.json"
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path "$bnDir\model.bin")) { throw "conversion failed" }
        Write-Host "      Bengali model ready"
    } catch {
        Write-Host "      Bengali model not built ($_) - Xyrus still works; run setup with -Bangla again later." -ForegroundColor Yellow
    } finally {
        Remove-Item Env:\HF_HOME -ErrorAction SilentlyContinue
        foreach ($d in @($cv, $cache)) { if (Test-Path $d) { Remove-Item $d -Recurse -Force -ErrorAction SilentlyContinue } }
    }
}

# 7. data folder (calendar, memories, song history)
New-Item -ItemType Directory -Force "$here\data" | Out-Null
Step 7 "data folder ready"

# 8. icon + shortcuts - the app's own code writes them, so they match the in-app "Start with Windows" switch.
#    -NoStartup only makes Xyrus.lnk in this folder (never touches the desktop or the Startup folder).
Step 8 "creating shortcuts..."
$pyCmd = "import sys; sys.path.insert(0, r'$here'); from xyrus import tray, autostart; tray.ensure_icon_file()"
if ($NoStartup) {
    $pyCmd += "; autostart.create_desktop_shortcut([autostart.BASE])"
} else {
    $pyCmd += "; autostart.create_desktop_shortcut(); autostart.set_enabled(True)"
}
& $venvPy -c $pyCmd

# 9. checks + launch
Step 9 "checking..."
& $venvPy -c "import sys; sys.path.insert(0, r'$here'); from xyrus import audio; m = audio.list_mics(); print('      mic:', m[0].name if m else 'none found - plug one in')"
& $venvPy "$here\arc.py" --selftest
if ($LASTEXITCODE -eq 0) {
    Write-Host "      self-test passed"
} else {
    Write-Host "      self-test failed - see the lines above and arc.log (Xyrus may still run)" -ForegroundColor Yellow
}
# the model check decodes on a worker thread, as the app does (a decode on the main thread of a process that then
# exits can crash at exit with CUDA - harmless, but it would look like a failure here)
$check = "import sys, threading; sys.path.insert(0, r'$here')`n" +
         "def go():`n" +
         "    try:`n" +
         "        import hearing; hearing.load_async(); ok = hearing.available(wait=240)`n" +
         "        print('      hearing:', (hearing.model_name() + ' on ' + hearing.device) if ok else 'not available (' + str(hearing.error) + ')', flush=True)`n" +
         "    except Exception as e:`n" +
         "        print('      hearing: not available (' + repr(e) + ')', flush=True)`n" +
         "t = threading.Thread(target=go); t.start(); t.join()`n" +
         "import os; os._exit(0)"
& $venvPy -c $check
Write-Host ""
if ($NoStartup) {
    Write-Host "Done. Double-click Xyrus.lnk in $here to start it." -ForegroundColor Green
} else {
    Write-Host "Done. Xyrus will start with Windows. Launching it now..." -ForegroundColor Green
    Start-Process -FilePath "$here\venv\Scripts\pythonw.exe" -ArgumentList "`"$here\arc.py`" --tray" -WorkingDirectory $here
    Write-Host 'Say: "Xyrus" - wait for the reply - then your command, e.g. "what can you do".'
}
