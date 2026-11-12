# Read-only six-minute observation of the existing pilot. Never prints API keys.
param([string]$BisRoot = '', [int]$DurationSeconds = 360, [int]$IntervalSeconds = 20)
$ErrorActionPreference = 'Stop'
if ($DurationSeconds -lt 1 -or $DurationSeconds -gt 1800 -or $IntervalSeconds -lt 1) { throw 'Invalid observation duration.' }
if (-not $BisRoot) {
    $Roots = @('C:\BIS-Pilot', 'C:\Users\Public\BIS-Pilot') | Where-Object { Test-Path (Join-Path $_ '.pilot-installation') }
    if (@($Roots).Count -ne 1) { throw 'Specify the pilot folder with -BisRoot.' }
    $BisRoot = @($Roots)[0]
}
$EnvFile = Join-Path $BisRoot '.env'
if (-not (Test-Path $EnvFile)) { throw 'Pilot settings file not found.' }
$Secrets = @()
$Variables = @{}
function Read-Settings {
    $Values = @{}
    foreach ($Line in Get-Content $EnvFile -Encoding UTF8) {
        if ($Line -match '^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=(.*)$') {
            $Values[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
        }
    }
    return $Values
}
$Variables = Read-Settings
$Secrets = @($Variables.Keys | Where-Object { $_ -match 'KEY|TOKEN|PASSWORD|SECRET' } | ForEach-Object { $Variables[$_] } | Where-Object { $_ -and $_.Length -ge 8 })
function Protect([string]$Text) {
    foreach ($Secret in $Secrets) { $Text = $Text.Replace($Secret, '[REDACTED]') }
    return [regex]::Replace($Text, '(?i)(Api-Key|Bearer)\s+[A-Za-z0-9_./+=-]+', '$1 [REDACTED]')
}
$Report = Join-Path $BisRoot ('AI-DIAGNOSTICS-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.txt')
function Record([string]$Text) {
    $Safe = Protect $Text
    $Line = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $Safe
    Add-Content -LiteralPath $Report -Value $Line -Encoding UTF8
    Write-Host $Line
}
function Request([string]$Path, [string]$Method = 'GET', [int]$Timeout = 12) {
    $Watch = [Diagnostics.Stopwatch]::StartNew()
    try {
        $Response = Invoke-WebRequest -Uri ('http://localhost:8081/api' + $Path) -Method $Method -TimeoutSec $Timeout -UseBasicParsing
        $Data = $null
        try { $Data = $Response.Content | ConvertFrom-Json } catch {}
        return @{ ok = $true; code = [int]$Response.StatusCode; seconds = [math]::Round($Watch.Elapsed.TotalSeconds, 1); data = $Data }
    } catch {
        $Code = $null
        if ($_.Exception.Response) { try { $Code = [int]$_.Exception.Response.StatusCode } catch {} }
        $Detail = if ($_.ErrorDetails.Message) { $_.ErrorDetails.Message } else { $_.Exception.Message }
        return @{ ok = $false; code = $Code; seconds = [math]::Round($Watch.Elapsed.TotalSeconds, 1); error = Protect ($Detail.Substring(0, [math]::Min(700, $Detail.Length))) }
    }
}
Record 'BIS AI observation. No settings are changed. Keep BIS running and reproduce the failure in the browser.'
Record 'Configuration changes are reported as yes/no; configuration values are not included.'
try {
    $Launcher = [IO.File]::ReadAllText((Join-Path $BisRoot 'START-BIS.cmd'))
    if ($Launcher -match '(?im)^set "BIS_WEB_DIR=(.+)[\\/]web"') {
        $ManifestFile = Join-Path $Matches[1] 'RELEASE.json'
        if (Test-Path -LiteralPath $ManifestFile) {
            $Manifest = Get-Content -LiteralPath $ManifestFile -Raw | ConvertFrom-Json
            Record ('Installed source commit: ' + $Manifest.source_commit)
        }
    }
} catch { Record 'Installed source commit could not be determined.' }
$InitialSettings = Get-FileHash -LiteralPath $EnvFile -Algorithm SHA256
$InitialConfigHash = $null
$Config = $Variables['BIS_LLM_CONFIG_FILE']
if ($Config -and -not [IO.Path]::IsPathRooted($Config)) { $Config = Join-Path $BisRoot $Config }
if ($Config -and (Test-Path -LiteralPath $Config)) { $InitialConfigHash = (Get-FileHash -LiteralPath $Config -Algorithm SHA256).Hash }
$Clock = [Diagnostics.Stopwatch]::StartNew()
$NextProbe = 0
while ($true) {
    $Current = Read-Settings
    $NewSecrets = @($Current.Keys | Where-Object { $_ -match 'KEY|TOKEN|PASSWORD|SECRET' } | ForEach-Object { $Current[$_] } | Where-Object { $_ -and $_.Length -ge 8 })
    $Secrets = @($Secrets + $NewSecrets | Select-Object -Unique)
    $ConfigExists = $Config -and (Test-Path -LiteralPath $Config)
    $ConfigChanged = $false
    if ($ConfigExists -and $InitialConfigHash) { $ConfigChanged = (Get-FileHash -LiteralPath $Config -Algorithm SHA256).Hash -ne $InitialConfigHash }
    $Snapshot = @{ elapsed_seconds = [int]$Clock.Elapsed.TotalSeconds; api_key_saved = [bool]$Current['YANDEX_API_KEY']; env_changed = ((Get-FileHash -LiteralPath $EnvFile -Algorithm SHA256).Hash -ne $InitialSettings.Hash); model_config_exists = [bool]$ConfigExists; model_config_changed = $ConfigChanged }
    $Status = Request '/workspace/reading-status'
    $Snapshot.status_http = $Status.code; $Snapshot.status_seconds = $Status.seconds
    if ($Status.ok -and $Status.data) {
        foreach ($Name in @('key_configured','text_configured','ocr_configured','model_error','last_model_attempts')) { $Snapshot[$Name] = $Status.data.$Name }
    } else { $Snapshot.status_error = $Status.error }
    Record ($Snapshot | ConvertTo-Json -Depth 5 -Compress)
    if ($Clock.Elapsed.TotalSeconds -ge $NextProbe) {
        $Probe = Request '/ai/models/check' 'POST' 130
        if ($Probe.ok -and $Probe.data) {
            $Checks = @($Probe.data.providers | ForEach-Object { @{ provider = $_.provider; ok = $_.ok; error = $_.error; hint = $_.hint; seconds = $_.seconds } })
            Record ('MODEL_CHECK ' + ($Checks | ConvertTo-Json -Depth 4 -Compress))
        } else { Record ('MODEL_CHECK ' + (@{ ok = $Probe.ok; code = $Probe.code; seconds = $Probe.seconds; error = $Probe.error } | ConvertTo-Json -Compress)) }
        $NextProbe = $Clock.Elapsed.TotalSeconds + 180
    }
    if ($Clock.Elapsed.TotalSeconds -ge $DurationSeconds) { break }
    Start-Sleep -Seconds ([math]::Min($IntervalSeconds, [math]::Max(1, $DurationSeconds - [int]$Clock.Elapsed.TotalSeconds)))
}
Record 'Observation complete.'
Write-Host "Report saved: $Report" -ForegroundColor Green
Write-Host 'Send the report text. Do not send .env or your API key.'
