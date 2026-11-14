# Read-only diagnosis: BIS HTTP response, SDK initialization, one direct model request.
param([string]$BisRoot = '', [int]$Port = 8081)
$ErrorActionPreference = 'Stop'
if (-not $BisRoot) {
    $Roots = @('C:\BIS-Pilot', 'C:\Users\Public\BIS-Pilot') | Where-Object { Test-Path (Join-Path $_ '.pilot-installation') }
    if (@($Roots).Count -ne 1) { throw 'Specify the pilot folder with -BisRoot.' }
    $BisRoot = @($Roots)[0]
}
$Python = Join-Path $BisRoot 'venv\Scripts\python.exe'
$EnvFile = Join-Path $BisRoot '.env'
if (-not (Test-Path $Python) -or -not (Test-Path $EnvFile)) { throw 'BIS installation not found.' }
$Report = Join-Path $BisRoot ('PROVIDER-DIAGNOSTICS-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.txt')
$Secrets = @(Get-Content $EnvFile -Encoding UTF8 | ForEach-Object {
    if ($_ -match '^\s*(?:export\s+)?\w*(?:KEY|TOKEN|PASSWORD|SECRET)\w*\s*=(.*)$') { $Matches[1].Trim().Trim('"').Trim("'") }
} | Where-Object { $_ -and $_.Length -ge 8 })
function Record([string]$Text) {
    foreach ($Secret in $Secrets) { $Text = $Text.Replace($Secret, '[REDACTED]') }
    $Text = [regex]::Replace($Text, '(?i)(Api-Key|Bearer)\s+[A-Za-z0-9_./+=-]+', '$1 [REDACTED]')
    Add-Content -LiteralPath $Report -Value $Text -Encoding UTF8
    Write-Host $Text
}
Record 'BIS provider diagnosis. Settings unchanged. One short direct model request may use API credits.'
try {
    $Launcher = [IO.File]::ReadAllText((Join-Path $BisRoot 'START-BIS.cmd'))
    if ($Launcher -match '(?im)^set "BIS_WEB_DIR=(.+)[\\/]web"') {
        $ManifestPath = Join-Path $Matches[1] 'RELEASE.json'
        if (-not (Test-Path $ManifestPath)) { $ManifestPath = Join-Path (Split-Path $Matches[1] -Parent) 'RELEASE.json' }
        $Manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
        Record ('Installed source: ' + $Manifest.source_commit)
    }
} catch { Record 'Installed source: unknown' }
try {
    $Status = Invoke-WebRequest "http://localhost:$Port/api/workspace/reading-status" -UseBasicParsing -TimeoutSec 6
    Record ('BIS STATUS HTTP ' + $Status.StatusCode)
    $Data = $Status.Content | ConvertFrom-Json
    Record (($Data | Select-Object selected_provider,selected_model,provider_key_configured,text_configured,ocr_provider,model_error,last_model_attempts | ConvertTo-Json -Depth 5))
} catch { Record ('BIS STATUS failed: ' + $_.Exception.Message) }
$Code = @'
import os, time
from pathlib import Path
from importlib.metadata import version
started = time.monotonic()
secrets = []
def note(text):
    text = str(text)
    for value in secrets:
        text = text.replace(value, '[REDACTED]')
    print(f'{time.monotonic()-started:.1f}s {text}', flush=True)
try:
    from bis.local import load_env
    load_env(Path('.env'), prefer_file=True)
    secrets = [v for k,v in os.environ.items() if any(t in k for t in ('KEY','TOKEN','SECRET','PASSWORD')) and len(v)>=8]
    note('SETTINGS loaded')
    from bis.config import get_settings
    settings = get_settings()
    configs = {p.name:p for p in settings.llm_providers() if p.kind != 'offline'}
    routes = settings.llm_routes()
    names = routes.get('chat') or routes.get('default') or list(configs)
    cfg = next((configs[n] for n in names if n in configs), None)
    if cfg is None:
        raise RuntimeError('No online provider selected')
    note(f'PROVIDER {cfg.name}, kind={cfg.kind}, model={cfg.model}, key_saved={bool(cfg.resolved_api_key())}')
    if cfg.resolved_api_key():
        secrets.append(cfg.resolved_api_key())
    if cfg.kind == 'anthropic':
        note('Anthropic SDK ' + version('anthropic'))
    note('CLIENT INITIALIZATION started')
    from bis.llm.providers import build_provider
    provider = build_provider(cfg.model_copy(update={'timeout':20}))
    note('CLIENT INITIALIZATION complete')
    from bis.llm.types import Message
    note('MODEL REQUEST started (20 second network timeout)')
    response = provider.complete([Message(role='user', content='Reply with OK only.')], max_tokens=16)
    note(f'MODEL RESPONSE provider={response.provider}, stop={response.stop_reason}, text={response.message.content[:100]}')
except Exception as error:
    note(f'ERROR {type(error).__name__}: {error}')
    cause = error.__cause__
    for _ in range(3):
        if cause is None:
            break
        note(f'CAUSE {type(cause).__name__}: {cause}')
        cause = cause.__cause__
'@
$Info = New-Object Diagnostics.ProcessStartInfo
$Info.FileName = $Python
$Info.Arguments = '-u -'
$Info.WorkingDirectory = $BisRoot
$Info.UseShellExecute = $false
$Info.CreateNoWindow = $true
$Info.RedirectStandardInput = $true
$Info.RedirectStandardOutput = $true
$Info.RedirectStandardError = $true
$Process = New-Object Diagnostics.Process
$Process.StartInfo = $Info
$null = $Process.Start()
$Output = $Process.StandardOutput.ReadToEndAsync()
$Errors = $Process.StandardError.ReadToEndAsync()
$Process.StandardInput.Write($Code)
$Process.StandardInput.Close()
if (-not $Process.WaitForExit(90000)) {
    $Process.Kill()
    $Process.WaitForExit()
    Record 'DIAGNOSTIC TIMEOUT at 90 seconds. Only the diagnostic process was stopped; BIS keeps running.'
}
Record ($Output.GetAwaiter().GetResult())
$ErrorText = $Errors.GetAwaiter().GetResult()
if ($ErrorText) { Record $ErrorText }
$Process.Dispose()
Write-Host "Report: $Report" -ForegroundColor Green
Write-Host 'Send this report. Do not send .env or your API key.'
