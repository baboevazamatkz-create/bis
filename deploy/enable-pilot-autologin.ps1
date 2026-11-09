# Enable password-free access in the local BIS-Pilot installation.
param([string]$BisRoot = '')
$ErrorActionPreference = 'Stop'
if (-not $BisRoot) {
    $Candidates = @('C:\BIS-Pilot', 'C:\Users\Public\BIS-Pilot') | Where-Object {
        Test-Path (Join-Path $_ '.pilot-installation')
    }
    if (@($Candidates).Count -ne 1) { throw 'Specify the pilot folder with -BisRoot.' }
    $BisRoot = @($Candidates)[0]
}
$EnvFile = Join-Path $BisRoot '.env'
if (-not (Test-Path (Join-Path $BisRoot '.pilot-installation')) -or -not (Test-Path $EnvFile)) {
    throw 'Local BIS-Pilot installation was not found. No settings were changed.'
}
$Lines = @(Get-Content $EnvFile -Encoding UTF8 | Where-Object { $_ -notmatch '^\s*BIS_AUTO_LOGIN\s*=' })
$Lines += 'BIS_AUTO_LOGIN=true'
[IO.File]::WriteAllText($EnvFile, (($Lines -join "`r`n") + "`r`n"), (New-Object Text.UTF8Encoding $false))
Write-Host 'Password-free access enabled. Stop BIS-Pilot with Ctrl+C and start the desktop shortcut again.' -ForegroundColor Green
