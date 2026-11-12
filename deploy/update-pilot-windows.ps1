# Update the existing local pilot, including the web interface. Preserve database and AI keys.
param([string]$BisRoot = '')
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$SourceCommit = '5a8be052a00c70c8d04175b4c40a61a19cfdf856'
$Expected = 'acbf818783a15811bce0f7a1a34fb5c253400c477532d2fe86620a029929dc8d'
if (-not $BisRoot) {
    $Candidates = @('C:\BIS-Pilot', 'C:\Users\Public\BIS-Pilot') | Where-Object { Test-Path (Join-Path $_ '.pilot-installation') }
    if (@($Candidates).Count -ne 1) { throw 'Specify the pilot folder with -BisRoot.' }
    $BisRoot = @($Candidates)[0]
}
$BisRoot = [IO.Path]::GetFullPath($BisRoot).TrimEnd('\')
$EnvFile = Join-Path $BisRoot '.env'
$Python = Join-Path $BisRoot 'venv\Scripts\python.exe'
$Launcher = Join-Path $BisRoot 'START-BIS.cmd'
foreach ($File in @($EnvFile, $Python, $Launcher, (Join-Path $BisRoot '.pilot-installation'))) {
    if (-not (Test-Path $File)) { throw "Incomplete pilot installation: $File" }
}
$Running = @(Get-Process bis, python, postgres -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -and $_.Path.StartsWith($BisRoot + '\', [StringComparison]::OrdinalIgnoreCase)
})
if ($Running.Count) { throw 'Stop BIS-Pilot with Ctrl+C and close its window before updating. No data were changed.' }
$LauncherText = [IO.File]::ReadAllText($Launcher)
$WebPattern = '(?im)^set "BIS_WEB_DIR=[^"\r\n]*"'
if (-not [regex]::IsMatch($LauncherText, $WebPattern)) { throw 'The pilot launcher has an unsupported format. No data were changed.' }
$TempDir = Join-Path ([IO.Path]::GetTempPath()) ('bis-update-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $TempDir | Out-Null
$Archive = Join-Path $TempDir 'source.zip'
Write-Host '1/3 Downloading the fixed BIS version...' -ForegroundColor Cyan
Invoke-WebRequest "https://github.com/baboevazamatkz-create/bis/archive/$SourceCommit.zip" -OutFile $Archive -UseBasicParsing
if ((Get-FileHash $Archive -Algorithm SHA256).Hash -ne $Expected) { throw 'Archive checksum mismatch. Update stopped.' }
Expand-Archive $Archive (Join-Path $TempDir 'source')
$Directories = @(Get-ChildItem (Join-Path $TempDir 'source') -Directory)
if ($Directories.Count -ne 1) { throw 'Unexpected source archive structure.' }
$Source = $Directories[0].FullName
foreach ($File in @('backend\pyproject.toml', 'deploy\web\index.html')) {
    if (-not (Test-Path (Join-Path $Source $File))) { throw "Incomplete source archive: $File" }
}
$App = Join-Path $BisRoot ('releases\pto_' + $SourceCommit.Substring(0, 12))
New-Item -ItemType Directory $App -Force | Out-Null
foreach ($Name in @('backend', 'deploy', 'docs', 'README.md')) { Copy-Item (Join-Path $Source $Name) $App -Recurse -Force }
Write-Host '2/3 Updating the application and required dependencies...'
& $Python -m pip install --upgrade (Join-Path $App 'backend')
if ($LASTEXITCODE -ne 0) { throw 'Package update failed. Keep the error above. Settings and database were preserved.' }
$Web = Join-Path $App 'deploy\web'
$LauncherText = [regex]::Replace($LauncherText, $WebPattern, [Text.RegularExpressions.MatchEvaluator]{
    param($Match)
    return 'set "BIS_WEB_DIR=' + $Web + '"'
})
[IO.File]::WriteAllText($Launcher, $LauncherText, [Text.Encoding]::Default)
$Encoding = New-Object Text.UTF8Encoding $false
$Lines = @(Get-Content $EnvFile -Encoding UTF8 | Where-Object { $_ -notmatch '^\s*BIS_AUTO_LOGIN\s*=' })
$Lines += 'BIS_AUTO_LOGIN=true'
[IO.File]::WriteAllText($EnvFile, (($Lines -join "`r`n") + "`r`n"), $Encoding)
$Manifest = @{ release_id = ('pto_' + $SourceCommit.Substring(0, 12)); source_commit = $SourceCommit; frontend_prebuilt = $true } | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $App 'RELEASE.json'), $Manifest, $Encoding)
Remove-Item $TempDir -Recurse -Force
Write-Host '3/3 Updated. Database, documents and AI keys preserved. Local login is automatic.' -ForegroundColor Green
Write-Host 'BIS-Pilot is starting. Refresh the browser with Ctrl+F5.'
Start-Process $Launcher
