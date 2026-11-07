# Полная очистка BIS на Windows: удаляет базу и файлы (демо-данные и всё загруженное), затем ставит BIS заново
# и создаёт вашу организацию. Настройки (.env: ключ Yandex, Telegram) сохраняются.
#
# PowerShell:
#   irm https://raw.githubusercontent.com/baboevazamatkz-create/bis/claude/construction-ai-automation-system-lya9lz/deploy/reset-windows.ps1 | iex

$ErrorActionPreference = "Stop"
$Branch = "claude/construction-ai-automation-system-lya9lz"
$Root = if (Test-Path "C:\BIS\venv") { "C:\BIS" } elseif (Test-Path "C:\Users\Public\BIS\venv") { "C:\Users\Public\BIS" } else { $null }
if (-not $Root) { Write-Host "BIS не найден в C:\BIS — очищать нечего." -ForegroundColor Yellow; return }
$Data = Join-Path $Root "data"

Write-Host "`nБудут УДАЛЕНЫ безвозвратно: база данных и все файлы BIS ($Data)." -ForegroundColor Red
Write-Host "Ключ Yandex и Telegram-бот останутся."
$ok = Read-Host "Чтобы продолжить, напечатайте DELETE"
if ($ok.Trim().ToUpper() -ne "DELETE") { Write-Host "Отменено, ничего не удалено."; return }

Write-Host "Останавливаю BIS..."
Get-Process postgres, pg_ctl, bis -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$Root*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep 3
if (Test-Path $Data) { Remove-Item $Data -Recurse -Force }
Write-Host "База очищена. Ставлю BIS заново — на вопрос про демо-данные ответьте n и введите данные своей организации." -ForegroundColor Green

irm "https://raw.githubusercontent.com/baboevazamatkz-create/bis/$Branch/deploy/install-windows.ps1" | iex
