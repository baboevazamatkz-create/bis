param([string]$BisRoot = '', [switch]$NoStart)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Payload = Join-Path $PSScriptRoot 'bis-install'
$Release = Get-Content -LiteralPath (Join-Path $Payload 'RELEASE.json') -Raw | ConvertFrom-Json
if ($Release.source_commit -notmatch '^[a-f0-9]{40}$' -or $Release.release_id -ne ('pto_' + $Release.source_commit.Substring(0, 12))) {
    throw 'Invalid BIS release metadata.'
}

function Assert-RegularPath([string]$Path, [string]$Type) {
    if (-not (Test-Path -LiteralPath $Path -PathType $Type)) { throw "Missing $Path" }
    $Item = Get-Item -LiteralPath $Path -Force
    if ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Unexpected junction or link: $Path" }
}

if (-not $BisRoot) {
    $Roots = @('C:\BIS-Pilot', 'C:\BIS-AOSR-Trial', 'C:\Users\Public\BIS-Pilot') | Where-Object {
        (Test-Path -LiteralPath (Join-Path $_ '.pilot-installation')) -or
        (Test-Path -LiteralPath (Join-Path $_ '.aosr-trial-installation'))
    }
    if (@($Roots).Count -ne 1) { throw 'Specify the existing BIS folder with -BisRoot.' }
    $BisRoot = @($Roots)[0]
}
$BisRoot = [IO.Path]::GetFullPath($BisRoot).TrimEnd('\')
if ($BisRoot -notmatch '^[A-Za-z]:\\.+' -or $BisRoot -match '[%!"\r\n]') { throw 'Invalid BIS folder.' }
Assert-RegularPath $BisRoot Container
$Classic = Join-Path $BisRoot '.pilot-installation'
$Trial = Join-Path $BisRoot '.aosr-trial-installation'
if (Test-Path -LiteralPath $Classic) {
    Assert-RegularPath $Classic Leaf
    $Python = Join-Path $BisRoot 'venv\Scripts\python.exe'
} elseif (Test-Path -LiteralPath $Trial) {
    Assert-RegularPath $Trial Leaf
    $Python = Join-Path $BisRoot 'python\python.exe'
} else { throw 'The selected folder is not a recognized BIS pilot.' }
$Launcher = Join-Path $BisRoot 'START-BIS.cmd'
foreach ($File in @($Launcher, (Join-Path $BisRoot '.env'), $Python)) { Assert-RegularPath $File Leaf }
$Releases = Join-Path $BisRoot 'releases'
Assert-RegularPath $Releases Container
Assert-RegularPath (Split-Path $Python -Parent) Container
if ($Python -like '*\venv\*') { Assert-RegularPath (Join-Path $BisRoot 'venv') Container }

function Assert-PilotStopped {
    $Location = '(?i)(?<![A-Za-z0-9_])' + [regex]::Escape($BisRoot) + '(?=\\|["\s]|$)'
    $Running = @(Get-CimInstance Win32_Process -Filter "Name='bis.exe' OR Name='python.exe' OR Name='pythonw.exe' OR Name='postgres.exe'" |
        Where-Object { ($_.ExecutablePath -replace '/', '\') -match $Location -or
                       ($_.CommandLine -replace '/', '\') -match $Location })
    if ($Running.Count) { throw 'Close the BIS window and wait for PostgreSQL to stop before updating.' }
}
Assert-PilotStopped
$OldLauncher = [IO.File]::ReadAllText($Launcher, [Text.Encoding]::Default)
$WebLine = '(?im)^set "BIS_WEB_DIR=[^"\r\n]*"'
$StartLine = '(?im)^"[^"\r\n]+\\(?:bis|python)\.exe"(?: (?:-I )?(?:-m bis\.cli|"[^"\r\n]+\\start_release\.py"))? local --dir "[^"\r\n]+" --host 127\.0\.0\.1 --port ([0-9]{1,5})[ \t]*\r?$'
$Start = [regex]::Matches($OldLauncher, $StartLine)
if ($Start.Count -ne 1 -or [regex]::Matches($OldLauncher, $WebLine).Count -ne 1) {
    throw 'The existing START-BIS.cmd has an unsupported format.'
}
$Port = [int]$Start[0].Groups[1].Value
if ($Port -lt 1 -or $Port -gt 65535) { throw 'The existing BIS port is invalid.' }

foreach ($Line in [IO.File]::ReadAllLines((Join-Path $Payload 'SHA256SUMS.txt'))) {
    if ($Line -notmatch '^([a-f0-9]{64})  (.+)$') { throw 'Invalid SHA256SUMS.txt.' }
    $Expected = $Matches[1]
    $Name = $Matches[2]
    if ($Name -match '(^|[\\/])\.\.([\\/]|$)|^[/\\]|:') { throw 'Invalid packaged path.' }
    $File = Join-Path $Payload $Name
    Assert-RegularPath $File Leaf
    if ((Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash -ne $Expected) { throw "Damaged or incomplete ZIP: $Name" }
}

& $Python -I -c 'import sys, sysconfig; assert sys.version_info[:2] == (3, 12) and sysconfig.get_platform() == ''win-amd64'''
if ($LASTEXITCODE -ne 0) { throw 'The existing installation requires Python 3.12 for Windows x64.' }
$App = Join-Path $Releases $Release.release_id
if (Test-Path -LiteralPath $App) { Assert-RegularPath $App Container }
$Stage = Join-Path $Releases ('.u-' + [guid]::NewGuid().ToString('N').Substring(0, 12))
New-Item -ItemType Directory -Path $Stage | Out-Null
try {
    $Packages = Join-Path $Stage 'packages'
    $Wheels = Join-Path $Payload 'wheels'
    & $Python -I -m pip --isolated --disable-pip-version-check install --no-index --only-binary ':all:' --ignore-installed --find-links $Wheels --target $Packages 'bis==0.1.0' 'pgserver==0.1.4' colorama tzdata
    if ($LASTEXITCODE -ne 0) { throw 'Offline dependency preparation failed.' }
    Copy-Item -LiteralPath (Join-Path $Payload 'deploy\web') -Destination (Join-Path $Stage 'web') -Recurse
    Copy-Item -LiteralPath (Join-Path $Payload 'RELEASE.json') -Destination $Stage
    Copy-Item -LiteralPath (Join-Path $Payload 'start_release.py') -Destination $Stage
    & $Python -I (Join-Path $Stage 'start_release.py') --verify
    if ($LASTEXITCODE -ne 0) { throw 'BIS verification failed before activation.' }

    Assert-PilotStopped
    # Each attempt gets its own immutable release directory. Previous launchers and files stay available.
    if (Test-Path -LiteralPath $App) { $App += '-' + [guid]::NewGuid().ToString('N').Substring(0, 8) }
    Move-Item -LiteralPath $Stage -Destination $App
    $Web = Join-Path $App 'web'
    $Main = Join-Path $App 'start_release.py'
    $NewLauncher = [regex]::Replace($OldLauncher, $WebLine, [Text.RegularExpressions.MatchEvaluator]{
        param($Match)
        return 'set "BIS_WEB_DIR=' + $Web + '"'
    })
    $NewLauncher = [regex]::Replace($NewLauncher, '(?im)^set "PYTHONPATH=[^"\r\n]*"\r?\n', '')
    $NewLauncher = [regex]::Replace($NewLauncher, $StartLine, [Text.RegularExpressions.MatchEvaluator]{
        param($Match)
        return '"' + $Python + '" -I "' + $Main + '" local --dir "' + $BisRoot + '" --host 127.0.0.1 --port ' + $Port + "`r"
    })
    $PendingLauncher = Join-Path $App 'START-BIS.next.cmd'
    [IO.File]::WriteAllText($PendingLauncher, $NewLauncher, [Text.Encoding]::Default)
    [IO.File]::Replace($PendingLauncher, $Launcher, (Join-Path $App 'START-BIS.previous.cmd'))
} finally {
    if (Test-Path -LiteralPath $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force }
}
Write-Host "Updated to $($Release.source_commit). Database, documents and settings preserved."
if (-not $NoStart) { Start-Process -FilePath $Launcher -WorkingDirectory $BisRoot }
