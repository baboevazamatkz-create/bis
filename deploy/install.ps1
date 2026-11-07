# Установка BIS на компьютер с Windows 10/11 (через Docker Desktop).
#
# Откройте PowerShell (Win+R → powershell → Enter) и выполните:
#   irm https://raw.githubusercontent.com/baboevazamatkz-create/bis/claude/construction-ai-automation-system-lya9lz/deploy/install.ps1 | iex
#
# Повторный запуск обновляет BIS и сохраняет настройки и данные.

$ErrorActionPreference = "Stop"
$Branch = "claude/construction-ai-automation-system-lya9lz"
$Zip = "https://github.com/baboevazamatkz-create/bis/archive/refs/heads/$Branch.zip"
$Dir = Join-Path $env:USERPROFILE "BIS"
$EnvFile = Join-Path $Dir "deploy\.env"
$Url = "http://localhost:8080"

function Say($t) { Write-Host "`n>> $t" -ForegroundColor Yellow }
function RandHex($n) {
  $b = New-Object byte[] $n
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
  ($b | ForEach-Object { $_.ToString("x2") }) -join ""
}

Say "1/6 Docker Desktop"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  Write-Host "Docker Desktop не установлен. Устанавливаю через winget (понадобится подтверждение и, возможно, перезагрузка)..."
  winget install -e --id Docker.DockerDesktop --accept-source-agreements --accept-package-agreements
  Write-Host "`nПосле установки запустите Docker Desktop (значок кита), дождитесь «Engine running»," -ForegroundColor Cyan
  Write-Host "при необходимости перезагрузите компьютер — и выполните эту команду установки ещё раз." -ForegroundColor Cyan
  return
}
docker info *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Запускаю Docker Desktop..."
  $dd = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
  if (Test-Path $dd) { Start-Process $dd }
  for ($i = 0; $i -lt 60; $i++) { Start-Sleep 5; docker info *> $null; if ($LASTEXITCODE -eq 0) { break } }
  docker info *> $null
  if ($LASTEXITCODE -ne 0) { Write-Host "Docker не запустился. Откройте Docker Desktop вручную и повторите." -ForegroundColor Red; return }
}

Say "2/6 Скачиваю BIS"
$tmp = Join-Path $env:TEMP "bis-download"
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $tmp | Out-Null
Invoke-WebRequest $Zip -OutFile (Join-Path $tmp "bis.zip") -UseBasicParsing
Expand-Archive (Join-Path $tmp "bis.zip") $tmp -Force
$src = Get-ChildItem $tmp -Directory | Select-Object -First 1
$savedEnv = if (Test-Path $EnvFile) { Get-Content $EnvFile -Raw } else { $null }
New-Item -ItemType Directory $Dir -Force | Out-Null
Copy-Item (Join-Path $src.FullName "*") $Dir -Recurse -Force
if ($savedEnv) { [IO.File]::WriteAllText($EnvFile, $savedEnv, (New-Object Text.UTF8Encoding $false)) }

if (-not (Test-Path $EnvFile)) {
  Say "3/6 Настройки"
  Write-Host "Ключ Yandex AI Studio. Вставьте правой кнопкой мыши и нажмите Enter (ключ будет виден — это нормально)."
  $key = (Read-Host "Секретный API-ключ (AQVN...)").Trim()
  $folder = (Read-Host "Идентификатор каталога (b1g...)").Trim()
  $tg = (Read-Host "Токен Telegram-бота (Enter — пропустить)").Trim()
  $tgName = ""
  if ($tg) { $tgName = (Read-Host "Имя бота без @").Trim() }
  $envText = @"
BIS_ENV=prod
BIS_SECRET_KEY=$(RandHex 32)
POSTGRES_PASSWORD=$(RandHex 16)
BIS_HTTP_PORT=8080
BIS_CORS_ORIGINS=["$Url"]
BIS_PUBLIC_URL=$Url
BIS_EMBEDDING_PROVIDER=hash

BIS_LLM_CONFIG_FILE=/config/llm.yandex.json
YANDEX_API_KEY=$key
YANDEX_FOLDER_ID=$folder
BIS_STT_PROVIDER=yandex
BIS_OCR_PROVIDER=yandex

BIS_TELEGRAM_BOT_TOKEN=$tg
BIS_TELEGRAM_BOT_USERNAME=$tgName
"@
  [IO.File]::WriteAllText($EnvFile, ($envText -replace "`r`n", "`n"), (New-Object Text.UTF8Encoding $false))
} else {
  Say "3/6 Настройки уже есть ($EnvFile) — оставляю"
}

Say "4/6 Сборка и запуск (первый раз 5–15 минут)"
Set-Location (Join-Path $Dir "deploy")
$profiles = @()
if ((Get-Content $EnvFile) -match "^BIS_TELEGRAM_BOT_TOKEN=.+") { $profiles = @("--profile", "telegram") }
docker compose @profiles up -d --build
if ($LASTEXITCODE -ne 0) { Write-Host "Сборка не удалась — пришлите текст ошибки выше." -ForegroundColor Red; return }

Say "5/6 Проверка"
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
  try { Invoke-WebRequest "$Url/api/health" -UseBasicParsing -TimeoutSec 3 | Out-Null; $ok = $true; break } catch { Start-Sleep 3 }
}
if (-not $ok) { Write-Host "BIS не ответил. Журнал: docker compose logs api" -ForegroundColor Red; return }
docker compose exec -T api bis llm-check

$marker = Join-Path $Dir "deploy\.installed"
if (-not (Test-Path $marker)) {
  Say "6/6 Организация"
  $demo = Read-Host "Загрузить демо-данные для пробы? (y/n)"
  if ($demo -eq "y") {
    docker compose exec -T api bis seed-demo
    Write-Host "Вход: admin@demo.bis / demo12345" -ForegroundColor Cyan
  } else {
    $org = Read-Host "Название организации"
    $inn = Read-Host "ИНН"
    $email = Read-Host "Email администратора"
    $name = Read-Host "ФИО администратора"
    $pass = Read-Host "Пароль администратора (от 8 символов)"
    docker compose exec -T api bis create-org --name "$org" --inn "$inn" --admin-email "$email" --admin-password "$pass" --admin-name "$name"
  }
  New-Item -ItemType File $marker | Out-Null
}

Write-Host "`nГотово: BIS работает — $Url" -ForegroundColor Green
Write-Host "Обновить: выполните команду установки ещё раз.  Остановить: cd $Dir\deploy; docker compose stop"
Start-Process $Url
