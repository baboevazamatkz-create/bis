# Установка BIS на Windows БЕЗ Docker и без прав администратора.
# Ставит Python 3.12 (если нет), BIS со встроенной базой PostgreSQL, создаёт ярлык «BIS» на рабочем столе.
#
# PowerShell (Win+R → powershell → Enter):
#   irm https://raw.githubusercontent.com/baboevazamatkz-create/bis/claude/construction-ai-automation-system-lya9lz/deploy/install-windows.ps1 | iex
#
# Повторный запуск обновляет BIS, настройки и данные сохраняются.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Branch = "claude/construction-ai-automation-system-lya9lz"
$Zip = "https://github.com/baboevazamatkz-create/bis/archive/refs/heads/$Branch.zip"
function Say($t) { Write-Host "`n>> $t" -ForegroundColor Yellow }
function RandHex($n) { $b = New-Object byte[] $n; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); ($b | % { $_.ToString("x2") }) -join "" }

# Папка без русских букв в пути (базе данных так надёжнее)
$Root = "C:\BIS"
try { New-Item -ItemType Directory $Root -Force | Out-Null } catch { $Root = "C:\Users\Public\BIS"; New-Item -ItemType Directory $Root -Force | Out-Null }
$App = Join-Path $Root "app"; $Venv = Join-Path $Root "venv"; $EnvFile = Join-Path $Root ".env"
$Bis = Join-Path $Venv "Scripts\bis.exe"
$Data = Join-Path $Root "data"; $Ready = Join-Path $Data ".ready"

# Остановить BIS и встроенную базу, если запущены (обновление, прошлая неудачная попытка)
Get-Process postgres, pg_ctl, bis -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$Root*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep 2
if ((Test-Path $Data) -and -not (Test-Path $Ready)) {
  Write-Host "Удаляю недоустановленную базу от прошлой попытки..."
  Remove-Item $Data -Recurse -Force
}

Say "1/6 Python 3.12"
function Find-Python {
  foreach ($p in @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", "C:\Program Files\Python312\python.exe")) { if (Test-Path $p) { return $p } }
  try { $p = (& py -3.12 -c "import sys;print(sys.executable)" 2>$null); if ($p) { return $p.Trim() } } catch {}
  return $null
}
$Py = Find-Python
if (-not $Py) {
  $inst = Join-Path $env:TEMP "python-3.12.8-amd64.exe"
  Invoke-WebRequest "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe" -OutFile $inst -UseBasicParsing
  Start-Process $inst -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=0 Include_test=0" -Wait
  $Py = Find-Python
  if (-not $Py) { Write-Host "Python не установился. Установите Python 3.12 с python.org и повторите." -ForegroundColor Red; return }
}
Write-Host "Python: $Py"

Say "2/6 Скачиваю BIS"
$tmp = Join-Path $env:TEMP "bis-download"
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $tmp | Out-Null
Invoke-WebRequest $Zip -OutFile (Join-Path $tmp "bis.zip") -UseBasicParsing
Expand-Archive (Join-Path $tmp "bis.zip") $tmp -Force
$src = Get-ChildItem $tmp -Directory | Select-Object -First 1
Remove-Item $App -Recurse -Force -ErrorAction SilentlyContinue
Move-Item $src.FullName $App

Say "3/6 Устанавливаю компоненты (первый раз 3–7 минут)"
if (-not (Test-Path $Bis)) { & $Py -m venv $Venv }
& (Join-Path $Venv "Scripts\python.exe") -m pip install -q --upgrade pip
& (Join-Path $Venv "Scripts\python.exe") -m pip install -q (Join-Path $App "backend") pgserver imageio-ffmpeg
if (-not (Test-Path $Bis)) { Write-Host "Не удалось установить BIS — пришлите текст ошибки выше." -ForegroundColor Red; return }

$first = -not (Test-Path $Ready)
if (-not (Test-Path $EnvFile)) {
  Say "4/6 Настройки"
  Write-Host "Вставляйте правой кнопкой мыши и нажимайте Enter."
  $key = (Read-Host "Секретный API-ключ Yandex AI Studio (AQVN...)").Trim()
  $folder = (Read-Host "Идентификатор каталога (b1g...)").Trim()
  $tg = (Read-Host "Токен Telegram-бота (Enter — пропустить)").Trim()
  $tgName = ""; if ($tg) { $tgName = (Read-Host "Имя бота без @").Trim() }
  $text = @"
BIS_ENV=prod
BIS_SECRET_KEY=$(RandHex 32)
BIS_PUBLIC_URL=http://localhost:8080
BIS_EMBEDDING_PROVIDER=hash
BIS_LLM_CONFIG_FILE=$App\deploy\config\llm.yandex.json
BIS_WEB_DIR=$App\deploy\web
YANDEX_API_KEY=$key
YANDEX_FOLDER_ID=$folder
BIS_STT_PROVIDER=yandex
BIS_OCR_PROVIDER=yandex
BIS_TELEGRAM_BOT_TOKEN=$tg
BIS_TELEGRAM_BOT_USERNAME=$tgName
"@
  [IO.File]::WriteAllText($EnvFile, $text, (New-Object Text.UTF8Encoding $false))
} else { Say "4/6 Настройки уже есть ($EnvFile)" }

Say "5/6 База данных"
if ($first) {
  $demo = Read-Host "Загрузить демо-данные для пробы? (y/n)"
  if ($demo -eq "y") {
    & $Bis local --dir $Root --setup --then seed-demo
    Write-Host "Вход: admin@demo.bis / demo12345" -ForegroundColor Cyan
  } else {
    $org = Read-Host "Название организации"; $inn = Read-Host "ИНН"; $email = Read-Host "Email администратора"
    $name = Read-Host "ФИО администратора"; $pass = Read-Host "Пароль администратора (от 8 символов)"
    & $Bis local --dir $Root --setup --then create-org --name $org --inn $inn --admin-email $email --admin-password $pass --admin-name $name
  }
} else {
  & $Bis local --dir $Root --setup
}
if ($LASTEXITCODE -ne 0) { Write-Host "База данных не подготовилась — пришлите текст выше." -ForegroundColor Red; return }
New-Item -ItemType File $Ready -Force | Out-Null

Say "6/6 Ярлык и запуск"
$cmd = Join-Path $Root "BIS.cmd"
@"
@echo off
chcp 65001 >nul
title BIS
start "" /b cmd /c "timeout /t 8 >nul & start http://localhost:8080"
"$Bis" local --dir "$Root"
pause
"@ | Set-Content -Encoding ASCII $cmd
$desk = [Environment]::GetFolderPath("Desktop")
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path $desk "BIS.lnk"))
$lnk.TargetPath = $cmd; $lnk.WorkingDirectory = $Root; $lnk.Save()

Write-Host "`nГотово. Запускаю BIS — откроется окно сервера и браузер с адресом http://localhost:8080" -ForegroundColor Green
Write-Host "Дальше запускайте ярлыком «BIS» на рабочем столе. Пока окно BIS открыто — система работает."
Start-Process $cmd
