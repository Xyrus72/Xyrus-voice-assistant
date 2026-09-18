# Xyrus-Setup.exe unpacks itself into a temporary folder and runs install.cmd, which runs this.
# It copies Xyrus into %LOCALAPPDATA%\Programs\Xyrus (no administrator rights needed) and runs setup.ps1 there.
# Re-running the setup on a PC that has Xyrus updates the program files and keeps the user's data, settings
# and downloaded models.
#   XYRUS_INSTALL_DIR  (environment) install somewhere else
#   XYRUS_SETUP_ARGS   (environment) options for setup.ps1, e.g. "-NoStartup -Bangla"
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$src = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($env:XYRUS_INSTALL_DIR) { $dest = $env:XYRUS_INSTALL_DIR } else { $dest = Join-Path $env:LOCALAPPDATA 'Programs\Xyrus' }

Write-Host ""
Write-Host "Xyrus - offline voice assistant" -ForegroundColor Magenta
Write-Host "Installing into: $dest"
Write-Host "This downloads Python and the speech models (about 3-9 GB) - keep the internet on. 10-40 minutes."
Write-Host ""

# a running copy of THIS install holds its files open: close it first (it restarts at the end of setup)
$running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$dest\arc.py*" }
foreach ($p in $running) {
    Write-Host "Closing the running Xyrus (pid $($p.ProcessId)) to update it..."
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}

New-Item -ItemType Directory -Force $dest | Out-Null
Expand-Archive -Path (Join-Path $src 'payload.zip') -DestinationPath $dest -Force

$setupArgs = @()
if ($env:XYRUS_SETUP_ARGS) { $setupArgs = $env:XYRUS_SETUP_ARGS -split '\s+' | Where-Object { $_ } }
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dest 'setup.ps1') @setupArgs
exit $LASTEXITCODE
