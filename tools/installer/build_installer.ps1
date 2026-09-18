# Builds dist\Xyrus-Setup.exe - ONE file that installs Xyrus on another Windows 10/11 PC.
#   powershell -ExecutionPolicy Bypass -File tools\installer\build_installer.ps1
# Uses IExpress (built into Windows). IExpress can only package flat files, so the program goes in as one
# payload.zip; install.cmd -> bootstrap.ps1 unpack it into %LOCALAPPDATA%\Programs\Xyrus and run setup.ps1,
# which downloads Python, the packages and the speech models on that PC.
# The payload is the program only: never venv\, the models, data\ (the user's calendar and memories),
# config.json (their settings), logs, tests, backups or legacy\.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$installer = Join-Path $root 'tools\installer'
$dist = Join-Path $root 'dist'
$stage = Join-Path $dist 'stage'
$payload = Join-Path $stage 'payload'
$out = Join-Path $dist 'Xyrus-Setup.exe'

if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force $payload | Out-Null

# program files
foreach ($f in @('arc.py', 'hearing.py', 'setup.ps1', 'install.bat', 'requirements.txt', 'requirements-gpu.txt',
                 'README.md', 'xyrus.ico')) {
    Copy-Item (Join-Path $root $f) (Join-Path $payload $f)
}
foreach ($d in @('xyrus', 'docs')) {
    robocopy (Join-Path $root $d) (Join-Path $payload $d) /E /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "copying $d failed (robocopy $LASTEXITCODE)" }
}
New-Item -ItemType Directory -Force (Join-Path $payload 'tools') | Out-Null
Copy-Item (Join-Path $root 'tools\convert_bangla.py') (Join-Path $payload 'tools\convert_bangla.py')

Compress-Archive -Path (Join-Path $payload '*') -DestinationPath (Join-Path $stage 'payload.zip') -CompressionLevel Optimal
Remove-Item $payload -Recurse -Force
Copy-Item (Join-Path $installer 'install.cmd') $stage
Copy-Item (Join-Path $installer 'bootstrap.ps1') $stage

# IExpress recipe (ANSI)
$sed = Join-Path $stage 'xyrus.sed'
$prompt = 'Install Xyrus, the offline voice assistant? It downloads Python and the speech models (about 3-9 GB) and takes 10-40 minutes.'
@"
[Version]
Class=IEXPRESS
SEDVersion=3
[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=1
HideExtractAnimation=0
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=%InstallPrompt%
DisplayLicense=%DisplayLicense%
FinishMessage=%FinishMessage%
TargetName=%TargetName%
FriendlyName=%FriendlyName%
AppLaunched=%AppLaunched%
PostInstallCmd=%PostInstallCmd%
AdminQuietInstCmd=%AdminQuietInstCmd%
UserQuietInstCmd=%UserQuietInstCmd%
SourceFiles=SourceFiles
[Strings]
InstallPrompt=$prompt
DisplayLicense=
FinishMessage=
TargetName=$out
FriendlyName=Xyrus setup
AppLaunched=cmd /c install.cmd
PostInstallCmd=<None>
AdminQuietInstCmd=
UserQuietInstCmd=
FILE0="install.cmd"
FILE1="bootstrap.ps1"
FILE2="payload.zip"
[SourceFiles]
SourceFiles0=$stage\
[SourceFiles0]
%FILE0%=
%FILE1%=
%FILE2%=
"@ -replace "`r?`n", "`r`n" | Set-Content -Path $sed -Encoding ASCII   # IExpress reads the recipe as an INI file: CRLF on EVERY line, the last one too

if (Test-Path $out) { Remove-Item $out -Force }
# the recipe path goes in UNQUOTED: IExpress takes quotes as part of the file name and fails (exit 1)
if ($sed -match ' ') { throw "IExpress can't take a recipe path with spaces: $sed" }
$p = Start-Process -FilePath "$env:WINDIR\System32\iexpress.exe" -ArgumentList @('/N', '/Q', $sed) -Wait -PassThru
if (-not (Test-Path $out)) { throw "IExpress did not build $out (exit $($p.ExitCode))" }
Remove-Item $stage -Recurse -Force
$size = [math]::Round((Get-Item $out).Length / 1MB, 2)
Write-Host "Built $out ($size MB)"
