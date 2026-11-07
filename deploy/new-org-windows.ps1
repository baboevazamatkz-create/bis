# Создать организацию и администратора в установленном BIS (Windows).
#   irm https://raw.githubusercontent.com/baboevazamatkz-create/bis/claude/construction-ai-automation-system-lya9lz/deploy/new-org-windows.ps1 | iex

$Root = if (Test-Path "C:\BIS\venv") { "C:\BIS" } elseif (Test-Path "C:\Users\Public\BIS\venv") { "C:\Users\Public\BIS" } else { $null }
if (-not $Root) { Write-Host "BIS не установлен — сначала запустите install-windows.ps1" -ForegroundColor Red; return }
$Bis = Join-Path $Root "venv\Scripts\bis.exe"

Write-Host "`nДанные пилотной организации. Вставка — правой кнопкой мыши, затем Enter." -ForegroundColor Yellow
$org = (Read-Host "Название организации").Trim()
$inn = (Read-Host "ИНН").Trim()
$name = (Read-Host "ФИО администратора").Trim()
$email = (Read-Host "Email администратора (это логин)").Trim()
do {
  $pass = (Read-Host "Пароль администратора (не короче 8 символов)").Trim()
  if ($pass.Length -lt 8) { Write-Host "Слишком короткий — нужно 8 символов или больше." -ForegroundColor Red }
} while ($pass.Length -lt 8)

& $Bis local --dir $Root --setup --then create-org --name $org --inn $inn --admin-email $email --admin-password $pass --admin-name $name
if ($LASTEXITCODE -ne 0) { Write-Host "Не получилось — пришлите текст выше." -ForegroundColor Red; return }
Write-Host "`nГотово. Вход в BIS (http://localhost:8080): логин $email и ваш пароль." -ForegroundColor Green
