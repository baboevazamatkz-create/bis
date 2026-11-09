# Installs THIS extracted package. Does not download application code from GitHub.
# Run: powershell -ExecutionPolicy Bypass -File .\INSTALL-WINDOWS.ps1
param([string]$BisRoot = 'C:\BIS-Pilot', [int]$Port = 8081)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Source = $PSScriptRoot
$Release = Get-Content (Join-Path $Source 'RELEASE.json') -Raw | ConvertFrom-Json
if ($Release.release_id -notmatch '^[A-Za-z0-9_-]+$') { throw 'Invalid release identifier' }
if (-not (Test-Path (Join-Path $Source 'backend\pyproject.toml'))) { throw 'Extract the complete ZIP first.' }
if (-not (Test-Path (Join-Path $Source 'deploy\web\index.html'))) { throw 'The packaged web interface is missing.' }
$BisRoot = [IO.Path]::GetFullPath($BisRoot)
$Marker = Join-Path $BisRoot '.pilot-installation'
if ((Test-Path $BisRoot) -and -not (Test-Path $Marker)) {
    $Existing = (Test-Path (Join-Path $BisRoot '.env')) -or (Test-Path (Join-Path $BisRoot 'data')) -or (Test-Path (Join-Path $BisRoot 'venv'))
    if ($Existing) { throw 'Existing installation detected. Use a NEW empty folder; do not overwrite your working BIS.' }
}
$Running = @(Get-Process bis, python, postgres -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($BisRoot + '\', [StringComparison]::OrdinalIgnoreCase) })
if ($Running.Count) { throw 'Close the running BIS-Pilot window before installing/updating this pilot.' }
$Python = $null
foreach ($Candidate in @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", 'C:\Program Files\Python312\python.exe')) {
    if (Test-Path $Candidate) { $Python = $Candidate; break }
}
if (-not $Python) {
    try {
        $Detected = & py -3.12 -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $Detected) { $Python = ([string]$Detected).Trim() }
    } catch {}
}
if (-not $Python) {
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'A 64-bit Windows installation is required.' }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $PythonVersion = '3.12.10'
    $DownloadDir = Join-Path ([IO.Path]::GetTempPath()) ('bis-python-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $DownloadDir | Out-Null
    $PythonInstaller = Join-Path $DownloadDir 'python-installer.exe'
    Write-Host 'Downloading Python 3.12 from python.org...'
    Invoke-WebRequest "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe" -OutFile $PythonInstaller -UseBasicParsing
    $Signature = Get-AuthenticodeSignature $PythonInstaller
    if ($Signature.Status -ne 'Valid' -or $Signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
        throw 'Python installer signature verification failed. The downloaded file was not executed.'
    }
    $InstallProcess = Start-Process $PythonInstaller -ArgumentList '/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=0 Include_test=0' -Wait -PassThru
    if ($InstallProcess.ExitCode -notin @(0, 3010)) { throw "Python installer failed: $($InstallProcess.ExitCode)" }
    $Python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    if (-not (Test-Path $Python)) { throw 'Python was not found after installation. Install Python 3.12 x64 manually and retry.' }
    Remove-Item $DownloadDir -Recurse -Force
}
$Venv = Join-Path $BisRoot 'venv'
$App = Join-Path (Join-Path $BisRoot 'releases') $Release.release_id
$EnvFile = Join-Path $BisRoot '.env'
$Initialized = Join-Path $BisRoot '.pilot-initialized'
New-Item -ItemType Directory -Path $BisRoot, $App -Force | Out-Null
Set-Content -Path $Marker -Value 'BIS isolated pilot installation' -Encoding ASCII
Write-Host '1/5 Copying the current application release...'
if ([IO.Path]::GetFullPath($Source).TrimEnd('\') -ne $App.TrimEnd('\')) {
    foreach ($Name in @('backend', 'deploy', 'docs', 'README.md', 'RELEASE.json', 'FIRST-START.md')) {
        Copy-Item (Join-Path $Source $Name) $App -Recurse -Force
    }
}
Write-Host '2/5 Installing Python dependencies (Internet is required)...'
if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) {
    & $Python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create the Python environment.' }
}
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed.' }
& $VenvPython -m pip install --upgrade (Join-Path $App 'backend') pgserver imageio-ffmpeg
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Keep the error text shown above.' }
Write-Host '3/5 Preparing local settings and document folders...'
$Library = Join-Path $BisRoot 'library'
foreach ($Dir in @('knowledge\norms', 'knowledge\forms', 'company', 'project-01')) {
    New-Item -ItemType Directory -Path (Join-Path $Library $Dir) -Force | Out-Null
}
if (-not (Test-Path $EnvFile)) {
    $Secret = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
    $LibraryJson = $Library.Replace('\', '/')
    $WebPath = (Join-Path $App 'deploy\web').Replace('\', '/')
    $Text = @"
BIS_ENV=prod
BIS_SECRET_KEY=$Secret
BIS_PUBLIC_URL=http://localhost:$Port
BIS_CORS_ORIGINS=["http://localhost:$Port"]
BIS_WEB_DIR=$WebPath
BIS_LIBRARY_ROOTS=["$LibraryJson"]
BIS_AUTO_LOGIN=true
BIS_VECTOR_ENABLED=false
BIS_EMBEDDING_PROVIDER=hash
"@
    [IO.File]::WriteAllText($EnvFile, $Text, (New-Object Text.UTF8Encoding $false))
}
Write-Host '4/5 Starting the bundled PostgreSQL and applying migrations...'
$Bootstrap = @'
import sys
from pathlib import Path
from bis.local import prepare
from bis.cli import main
root = Path(sys.argv[1])
prepare(root)
if not (root / '.pilot-initialized').exists():
    if main(['seed-demo']) != 0:
        raise SystemExit(1)
    if main(['grant-platform-admin', '--email', 'admin@demo.bis']) != 0:
        raise SystemExit(1)
    (root / '.pilot-initialized').write_text('Synthetic pilot initialized', encoding='utf-8')
'@
& $VenvPython -c $Bootstrap $BisRoot
if ($LASTEXITCODE -ne 0) { throw 'Database preparation failed. Keep the error text shown above.' }
Write-Host '5/5 Creating the launcher...'
$Launcher = Join-Path $BisRoot 'START-BIS.cmd'
$Bis = Join-Path $Venv 'Scripts\bis.exe'
$Web = Join-Path $App 'deploy\web'
$Cmd = @"
@echo off
setlocal
set "BIS_WEB_DIR=$Web"
title BIS Pilot
start "" /b cmd /c "timeout /t 8 /nobreak >nul & start http://localhost:$Port"
"$Bis" local --dir "$BisRoot" --host 127.0.0.1 --port $Port
pause
"@
[IO.File]::WriteAllText($Launcher, $Cmd, [Text.Encoding]::Default)
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'BIS-Pilot.lnk'))
$Shortcut.TargetPath = $Launcher
$Shortcut.WorkingDirectory = $BisRoot
$Shortcut.Save()
Write-Host "Ready: http://localhost:$Port" -ForegroundColor Green
Write-Host 'Local pilot opens automatically without a login form.'
Write-Host "Settings: $EnvFile"
Write-Host "Documents: $Library"
Write-Host 'No AI credentials are included. Without a configured model, the app uses rules/offline mode.'
Write-Host 'Start the desktop shortcut BIS-Pilot. Keep its window open while working.'
Start-Process $Launcher
