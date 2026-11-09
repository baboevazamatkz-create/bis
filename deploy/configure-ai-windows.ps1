# Connect the existing BIS-Pilot installation to YandexGPT. No application update required.
param([string]$BisRoot = '')
$ErrorActionPreference = 'Stop'
if (-not $BisRoot) {
    $Candidates = @('C:\BIS-Pilot', 'C:\Users\Public\BIS-Pilot') | Where-Object {
        Test-Path (Join-Path $_ '.pilot-installation')
    }
    if (@($Candidates).Count -ne 1) { throw 'Specify the pilot folder with -BisRoot. Expected C:\BIS-Pilot.' }
    $BisRoot = @($Candidates)[0]
}
$EnvFile = Join-Path $BisRoot '.env'
$Python = Join-Path $BisRoot 'venv\Scripts\python.exe'
if (-not (Test-Path (Join-Path $BisRoot '.pilot-installation')) -or -not (Test-Path $EnvFile) -or -not (Test-Path $Python)) {
    throw 'BIS-Pilot installation was not found.'
}
$Lines = @(Get-Content $EnvFile -Encoding UTF8)
function Get-Setting([string]$Name) {
    $MatchLine = @($Lines | Where-Object { $_ -match ('^\s*' + [regex]::Escape($Name) + '\s*=') })
    if ($MatchLine.Count) { return ($MatchLine[-1] -split '=', 2)[1].Trim() }
    return ''
}
Write-Host 'Connect YandexGPT to BIS-Pilot' -ForegroundColor Cyan
Write-Host 'Cloud requests send processed document fragments to Yandex Cloud.'
Write-Host 'Required: a paid/active cloud account, Folder ID and an API key with ai.languageModels.user access.'
$Folder = Read-Host 'YANDEX_FOLDER_ID (Enter keeps the existing value)'
if (-not $Folder) { $Folder = Get-Setting 'YANDEX_FOLDER_ID' }
if (-not $Folder) { $Folder = Get-Setting 'BIS_YANDEX_FOLDER_ID' }
if ($Folder -notmatch '^[A-Za-z0-9_-]+$') { throw 'A valid Folder ID is required. Settings were not changed.' }
$SecureKey = Read-Host 'YANDEX_API_KEY (hidden; Enter keeps the existing key)' -AsSecureString
$Key = ''
$Pointer = [IntPtr]::Zero
try {
    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureKey)
    $Key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
} finally {
    if ($Pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer) }
}
if (-not $Key) { $Key = Get-Setting 'YANDEX_API_KEY' }
if (-not $Key -or $Key -match '\s') { throw 'An API key without whitespace is required. Settings were not changed.' }
$ConfigDir = Join-Path $BisRoot 'config'
$ConfigPath = Join-Path $ConfigDir 'llm.yandex-text.json'
$ExistingConfig = Get-Setting 'BIS_LLM_CONFIG_FILE'
if ($ExistingConfig -and [IO.Path]::GetFullPath($ExistingConfig) -ne [IO.Path]::GetFullPath($ConfigPath)) {
    throw 'A different LLM configuration already exists. Keep it and run llm-check; this helper will not overwrite it.'
}
$Config = @'
{
  "providers": [
    {
      "name": "yandexgpt", "kind": "openai_compat",
      "base_url": "https://llm.api.cloud.yandex.net/v1",
      "model": "gpt://${YANDEX_FOLDER_ID}/yandexgpt/latest",
      "api_key_env": "YANDEX_API_KEY", "auth_scheme": "Api-Key",
      "max_sensitivity": "internal", "mask_pii": true, "temperature": 0.1, "timeout": 90
    },
    {"name": "offline", "kind": "offline", "max_sensitivity": "confidential"}
  ],
  "routes": {"default": ["yandexgpt", "offline"]}
}
'@
New-Item -ItemType Directory $ConfigDir -Force | Out-Null
$Encoding = New-Object Text.UTF8Encoding $false
if (-not (Test-Path $ConfigPath)) { [IO.File]::WriteAllText($ConfigPath, $Config, $Encoding) }
$Settings = @{
    BIS_LLM_CONFIG_FILE = $ConfigPath.Replace('\', '/')
    YANDEX_FOLDER_ID = $Folder
    BIS_YANDEX_FOLDER_ID = $Folder
    YANDEX_API_KEY = $Key
}
foreach ($Name in $Settings.Keys) {
    $Lines = @($Lines | Where-Object { $_ -notmatch ('^\s*' + [regex]::Escape($Name) + '\s*=') })
    $Lines += ($Name + '=' + $Settings[$Name])
}
[IO.File]::WriteAllText($EnvFile, (($Lines -join "`r`n") + "`r`n"), $Encoding)
$Key = $null
$Settings = $null
$SecureKey.Dispose()
Write-Host "Saved Folder ID: $Folder"
$Launcher = Join-Path $BisRoot 'START-BIS.cmd'
if (Test-Path $Launcher) {
    $LauncherText = [IO.File]::ReadAllText($Launcher)
    $Begin = 'rem BIS_AI_ENV_BEGIN'
    $End = 'rem BIS_AI_ENV_END'
    $LauncherText = [regex]::Replace($LauncherText, '(?ms)^rem BIS_AI_ENV_BEGIN\r?\n.*?^rem BIS_AI_ENV_END\r?\n?', '')
    $Clear = @($Begin)
    foreach ($Name in @('BIS_LLM_CONFIG_FILE', 'YANDEX_FOLDER_ID', 'BIS_YANDEX_FOLDER_ID', 'YANDEX_API_KEY')) {
        $Clear += ('set "' + $Name + '="')
    }
    $Clear += $End
    $Block = ($Clear -join "`r`n") + "`r`n"
    $LauncherText = [regex]::Replace($LauncherText, '(?im)^setlocal[^\r\n]*\r?\n', ('$0' + $Block))
    [IO.File]::WriteAllText($Launcher, $LauncherText, [Text.Encoding]::Default)
}
Write-Host 'Settings saved. Checking a REAL model response (offline fallback does not pass this check)...'
$Check = @'
import os
import sys
from pathlib import Path
from bis.local import load_env
from bis.cli import main
root = Path(sys.argv[1]).resolve()
os.chdir(root)
for name in ('BIS_LLM_CONFIG_FILE', 'YANDEX_FOLDER_ID', 'BIS_YANDEX_FOLDER_ID', 'YANDEX_API_KEY'):
    os.environ.pop(name, None)
load_env(root / '.env')
from bis.config import get_settings
settings = get_settings()
print('Effective config:', settings.llm_config_file)
for provider in settings.llm_providers():
    if provider.kind != 'offline':
        print('Effective model:', provider.name, provider.model)
raise SystemExit(main(['llm-check']))
'@
& $Python -c $Check $BisRoot
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Copy the complete provider HTTP error ABOVE the hint arrow; do not share your API key.' -ForegroundColor Yellow
    throw 'Model check failed. Settings are saved. Check the effective model and the complete provider HTTP error above.'
}
Write-Host 'YandexGPT responded successfully. Stop and restart BIS-Pilot to apply the settings.' -ForegroundColor Green
Write-Host 'This connects the text model. OCR, voice and visual drawing understanding require separate setup and checks.'
