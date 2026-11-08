# Модель зрения на своём компьютере для «Эрудита»: Ollama + Llama 3.2 Vision 11B.
# Читает схемы, чертежи и фото документов локально — без интернета и без Yandex.
#
# PowerShell:
#   irm https://raw.githubusercontent.com/baboevazamatkz-create/bis/claude/construction-ai-automation-system-lya9lz/deploy/install-vision-windows.ps1 | iex
#
# Нужно: ~8 ГБ на диске, 16 ГБ оперативной памяти; с видеокартой NVIDIA (8+ ГБ) — секунды на лист, без неё — минуты.
# Другая модель: перед запуском $env:BIS_VISION_MODEL = "qwen2.5vl:7b" (лучше читает русский текст на листах).

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Model = if ($env:BIS_VISION_MODEL) { $env:BIS_VISION_MODEL } else { "llama3.2-vision" }
function Say($t) { Write-Host "`n>> $t" -ForegroundColor Yellow }

Say "1/3 Ollama"
$Ollama = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
if (-not (Test-Path $Ollama)) {
  $cmd = Get-Command ollama -ErrorAction SilentlyContinue
  if ($cmd) { $Ollama = $cmd.Source }
}
if (-not (Test-Path $Ollama)) {
  $inst = Join-Path $env:TEMP "OllamaSetup.exe"
  Write-Host "Скачиваю Ollama (~1 ГБ)..."
  Invoke-WebRequest "https://ollama.com/download/OllamaSetup.exe" -OutFile $inst -UseBasicParsing
  Start-Process $inst -ArgumentList "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES" -Wait
  if (-not (Test-Path $Ollama)) { Write-Host "Ollama не установилась — поставьте вручную с ollama.com и повторите." -ForegroundColor Red; return }
}
# сервер Ollama: после установки запускается сам; на всякий случай — запустить
try { Invoke-RestMethod "http://localhost:11434/api/version" -TimeoutSec 3 | Out-Null }
catch { Start-Process $Ollama -ArgumentList "serve" -WindowStyle Hidden; Start-Sleep 5 }

Say "2/3 Модель $Model (первый раз — несколько ГБ, 5–30 минут)"
& $Ollama pull $Model
if ($LASTEXITCODE -ne 0) { Write-Host "Модель не скачалась — пришлите текст выше." -ForegroundColor Red; return }

Say "3/3 Настройки BIS"
$EnvFile = if (Test-Path "C:\BIS\.env") { "C:\BIS\.env" } elseif (Test-Path "C:\Users\Public\BIS\.env") { "C:\Users\Public\BIS\.env" } else { $null }
if ($EnvFile) {
  $text = Get-Content $EnvFile -Raw
  $text = ($text -replace "(?m)^BIS_VISION_MODEL=.*\r?\n?", "").TrimEnd() + "`r`nBIS_VISION_MODEL=$Model`r`n"
  [IO.File]::WriteAllText($EnvFile, $text, (New-Object Text.UTF8Encoding $false))
  Write-Host "Модель зрения прописана в $EnvFile"
}
Write-Host "Проверка: модель отвечает?"
$body = @{ model = $Model; max_tokens = 10; messages = @(@{ role = "user"; content = "Ответь одним словом: да" }) } | ConvertTo-Json -Depth 5
try {
  $r = Invoke-RestMethod "http://localhost:11434/v1/chat/completions" -Method Post -Body ([Text.Encoding]::UTF8.GetBytes($body)) -ContentType "application/json" -TimeoutSec 600
  Write-Host ("Ответ модели: " + $r.choices[0].message.content) -ForegroundColor Cyan
} catch { Write-Host "Модель не ответила: $_" -ForegroundColor Red; return }

Write-Host "`nГотово. Перезапустите BIS (окно BIS → закрыть → ярлык BIS) — «Эрудит» начнёт читать схемы и чертежи сам;" -ForegroundColor Green
Write-Host "сразу — кнопка «Досчитать сейчас» на странице «Эрудит»."
