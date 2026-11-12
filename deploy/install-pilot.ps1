# Windows 10/11 x64: download the published source and prebuilt interface.
# irm https://raw.githubusercontent.com/baboevazamatkz-create/bis/codex/pilot-readiness/deploy/install-pilot.ps1 | iex
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$SourceCommit = '5a8be052a00c70c8d04175b4c40a61a19cfdf856'
$Expected = 'acbf818783a15811bce0f7a1a34fb5c253400c477532d2fe86620a029929dc8d'
$SourceUrl = "https://github.com/baboevazamatkz-create/bis/archive/$SourceCommit.zip"
$TempDir = Join-Path ([IO.Path]::GetTempPath()) ('bis-pilot-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $TempDir | Out-Null
$Archive = Join-Path $TempDir 'BIS-Pilot-Windows.zip'
Write-Host 'Downloading BIS PTO pilot from GitHub...' -ForegroundColor Cyan
Invoke-WebRequest $SourceUrl -OutFile $Archive -UseBasicParsing
if ((Get-FileHash $Archive -Algorithm SHA256).Hash -ne $Expected) {
    throw 'The package checksum does not match. Installation was stopped.'
}
Expand-Archive -Path $Archive -DestinationPath $TempDir
$Package = Join-Path $TempDir "bis-$SourceCommit"
$Installer = Join-Path $Package 'INSTALL-WINDOWS.ps1'
Copy-Item (Join-Path $Package 'deploy\pilot\INSTALL-WINDOWS.ps1') $Installer
Copy-Item (Join-Path $Package 'docs\PILOT_WINDOWS.md') (Join-Path $Package 'FIRST-START.md')
$Manifest = @{
    release_id = ('pto_' + $SourceCommit.Substring(0, 12))
    source_commit = $SourceCommit
    frontend_prebuilt = $true
    python_auto_install = $true
    windows_installer_runtime_tested = $false
} | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $Package 'RELEASE.json'), $Manifest, (New-Object Text.UTF8Encoding $false))
$BisRoot = 'C:\BIS-Pilot'
try { New-Item -ItemType Directory -Path $BisRoot -Force | Out-Null }
catch { $BisRoot = Join-Path $env:PUBLIC 'BIS-Pilot' }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Installer -BisRoot $BisRoot
if ($LASTEXITCODE -ne 0) {
    throw "Installation failed. Keep the error text shown above. Downloaded package: $TempDir"
}
Remove-Item $TempDir -Recurse -Force
