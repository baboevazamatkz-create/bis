# Windows 10/11 x64: download and install the published PTO pilot.
# irm https://raw.githubusercontent.com/baboevazamatkz-create/bis/pto-pilot-20261110/deploy/install-pilot.ps1 | iex
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ReleaseUrl = 'https://github.com/baboevazamatkz-create/bis/releases/download/pto-pilot-20261110'
$TempDir = Join-Path ([IO.Path]::GetTempPath()) ('bis-pilot-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $TempDir | Out-Null
$Archive = Join-Path $TempDir 'BIS-Pilot-Windows.zip'
Write-Host 'Downloading BIS PTO pilot from GitHub...' -ForegroundColor Cyan
Invoke-WebRequest "$ReleaseUrl/BIS-Pilot-Windows.zip" -OutFile $Archive -UseBasicParsing
$Checksum = Join-Path $TempDir 'BIS-Pilot-Windows.zip.sha256'
Invoke-WebRequest "$ReleaseUrl/BIS-Pilot-Windows.zip.sha256" -OutFile $Checksum -UseBasicParsing
$HashText = Get-Content $Checksum -Raw
$Expected = ([string]$HashText).Trim().Split(' ')[0]
if ($Expected -notmatch '^[a-fA-F0-9]{64}$') { throw 'Invalid package checksum file.' }
if ((Get-FileHash $Archive -Algorithm SHA256).Hash -ne $Expected) {
    throw 'The package checksum does not match. Installation was stopped.'
}
Expand-Archive -Path $Archive -DestinationPath $TempDir
$Installer = Join-Path $TempDir 'bis-install\INSTALL-WINDOWS.ps1'
if (-not (Test-Path $Installer)) { throw 'The downloaded package has no installer.' }
$BisRoot = 'C:\BIS-Pilot'
try { New-Item -ItemType Directory -Path $BisRoot -Force | Out-Null }
catch { $BisRoot = Join-Path $env:PUBLIC 'BIS-Pilot' }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Installer -BisRoot $BisRoot
if ($LASTEXITCODE -ne 0) {
    throw "Installation failed. Keep the error text shown above. Downloaded package: $TempDir"
}
Remove-Item $TempDir -Recurse -Force
