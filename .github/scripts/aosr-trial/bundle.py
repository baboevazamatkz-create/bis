import hashlib
import json
import re
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

stage, repo, downloads = map(Path, sys.argv[1:])
with ZipFile(stage / 'BIS-Pilot-Windows.zip') as base:
    files = {name.removeprefix('bis-install/'): base.read(name) for name in base.namelist()}

release = json.loads(files['RELEASE.json'])
assert release['source_commit'] == 'dbaae7720b2ec6f964da02dc536c859949006638'
assert files['backend/bis/services/library_forms.py'] == (repo / 'backend/bis/services/library_forms.py').read_bytes()
release.update(trial=True, trial_port=8082, trial_python='3.12.10-win-amd64',
               python_auto_install=False, offline_dependencies=True, bundled_ffmpeg=False)
for name in tuple(files):
    if name.startswith('deploy/web/'):
        del files[name]
frontend = repo / 'frontend/dist'
for path in frontend.rglob('*'):
    if path.is_file():
        files['deploy/web/' + path.relative_to(frontend).as_posix()] = path.read_bytes()
assert any('Настроить АОСР'.encode() in content for name, content in files.items() if name.startswith('deploy/web/assets/') and name.endswith('.js'))
files['RELEASE.json'] = (json.dumps(release, indent=2) + '\n').encode()
runtime = (stage / 'python-3.12.10.nupkg').read_bytes()
files['python-runtime.zip'] = runtime
for wheel in sorted((stage / 'wheels').glob('*.whl')):
    files['wheels/' + wheel.name] = wheel.read_bytes()

stock = files['INSTALL-WINDOWS.ps1'].decode('utf-8')
stock = stock[stock.index('$ErrorActionPreference'):].replace("$Source = $PSScriptRoot", """$Source = $PSScriptRoot
$BisRoot = 'C:\\BIS-AOSR-Trial'
$Port = 8082
Get-ChildItem Env:BIS_* | ForEach-Object { Remove-Item -LiteralPath ('Env:' + $_.Name) }
if (-not [Environment]::Is64BitOperatingSystem) { throw 'This trial requires 64-bit Windows.' }""", 1)
start = stock.index("$Marker = Join-Path")
end = stock.index("$App = Join-Path", start)
stock = stock[:start] + """$Marker = Join-Path $BisRoot '.aosr-trial-installation'
$MarkerText = 'BIS isolated AOSR trial'
if (Test-Path -LiteralPath $BisRoot) {
    $RootItem = Get-Item -LiteralPath $BisRoot -Force
    if (-not $RootItem.PSIsContainer -or ($RootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        -not (Test-Path -LiteralPath $Marker -PathType Leaf) -or
        (Get-Content -LiteralPath $Marker -Raw).Trim() -ne $MarkerText) {
        throw 'C:\\BIS-AOSR-Trial already exists and is not a BIS AOSR trial folder. Setup stopped.'
    }
    foreach ($Name in @('python', 'releases', 'data', 'library', '.env')) {
        $Path = Join-Path $BisRoot $Name
        if ((Test-Path -LiteralPath $Path) -and ((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Trial path contains a link: $Path"
        }
    }
}
$Listener = New-Object Net.Sockets.TcpListener ([Net.IPAddress]::Loopback), $Port
try { $Listener.Start() } catch {
    throw 'Port 8082 is in use. Close the BIS AOSR trial window and try again.'
} finally { $Listener.Stop() }
$PythonFolder = Join-Path $BisRoot 'python'
$Python = Join-Path $PythonFolder 'python.exe'
$Wheelhouse = Join-Path $Source 'wheels'
$RuntimeArchive = Join-Path $Source 'python-runtime.zip'
if (-not (Test-Path -LiteralPath $Wheelhouse -PathType Container) -or
    -not (Test-Path -LiteralPath $RuntimeArchive -PathType Leaf)) { throw 'Extract the complete ZIP first.' }
""" + stock[end:]
stock = stock.replace("Set-Content -Path $Marker -Value 'BIS isolated pilot installation' -Encoding ASCII",
                      "Set-Content -LiteralPath $Marker -Value $MarkerText -Encoding ASCII")
start = stock.index("Write-Host '2/5")
end = stock.index("Write-Host '3/5", start)
stock = stock[:start] + """Write-Host '2/5 Preparing bundled Python and offline dependencies...'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $ExpectedRuntimeHash = '""" + hashlib.sha256(runtime).hexdigest() + """'
    if ((Get-FileHash -LiteralPath $RuntimeArchive -Algorithm SHA256).Hash -ne $ExpectedRuntimeHash) {
        throw 'Python archive failed its SHA-256 check.'
    }
    if (Test-Path -LiteralPath $PythonFolder) { throw 'Incomplete bundled Python directory. Setup stopped.' }
    $ExpandDir = Join-Path ([IO.Path]::GetTempPath()) ('bis-aosr-python-' + [guid]::NewGuid().ToString('N'))
    try {
        Expand-Archive -LiteralPath $RuntimeArchive -DestinationPath $ExpandDir
        Move-Item -LiteralPath (Join-Path $ExpandDir 'tools') -Destination $PythonFolder
    } finally {
        if (Test-Path -LiteralPath $ExpandDir) { Remove-Item -LiteralPath $ExpandDir -Recurse -Force }
    }
}
& $Python -I -c 'import sysconfig; assert sysconfig.get_platform() == ''win-amd64'''
if ($LASTEXITCODE -ne 0) { throw 'Bundled Python did not start.' }
& $Python -I -m pip --isolated --disable-pip-version-check install --no-index --find-links $Wheelhouse --only-binary ':all:' --upgrade 'bis==0.1.0' 'pgserver==0.1.4' colorama tzdata
if ($LASTEXITCODE -ne 0) { throw 'Offline dependency installation failed. Keep the error text shown above.' }
& $Python -I -m pip --isolated --disable-pip-version-check check
if ($LASTEXITCODE -ne 0) { throw 'The local Python dependencies failed verification.' }
""" + stock[end:]
stock = stock.replace('    $LibraryJson =', '''    $RootPath = $BisRoot.Replace('\\', '/')
    $LibraryJson =''')
stock = stock.replace('BIS_LIBRARY_ROOTS=["$LibraryJson"]', '''BIS_LIBRARY_ROOTS=["$LibraryJson"]
BIS_DATA_DIR=$RootPath/data
BIS_STORAGE_DIR=$RootPath/data/files''')
start = stock.index("$Bootstrap = @'")
end = stock.index("if ($LASTEXITCODE", start)
python_script = stock[start:end].split("\n", 1)[1].split("\n'@", 1)[0]
files['bootstrap-trial.py'] = (python_script + "\n").encode()
stock = stock[:start] + "& $Python -I (Join-Path $Source 'bootstrap-trial.py') $BisRoot\n" + stock[end:]
stock = stock.replace("$Bis = Join-Path $Venv 'Scripts\\bis.exe'\n", "")
stock = stock.replace('set "BIS_WEB_DIR=$Web"\nset "BIS_OPEN_BROWSER=true"\ntitle BIS Pilot\n"$Bis" local',
                       """for /f "tokens=1 delims==" %%V in ('set BIS_ 2^>nul') do set "%%V="
set "BIS_WEB_DIR=$Web"
set "BIS_OPEN_BROWSER=true"
title BIS AOSR Trial
"$Python" -I -m bis.cli local""")
stock = stock.replace("'BIS-Pilot.lnk'", "'BIS-AOSR-Trial.lnk'").replace('shortcut BIS-Pilot.', 'shortcut BIS-AOSR-Trial.')
assert '$Venv' not in stock and 'Invoke-WebRequest' not in stock and 'C:\\BIS-Pilot' not in stock
assert '$Python" -I -m bis.cli local' in stock
files['INSTALL-WINDOWS.ps1'] = ('\ufeff' + '# Installs the extracted BIS AOSR trial into its dedicated folder.\n' + stock).replace('\n', '\r\n').encode()
files['SHA256SUMS.txt'] = ''.join(
    f'{hashlib.sha256(content).hexdigest()}  {name}\n' for name, content in sorted(files.items()) if name != 'SHA256SUMS.txt'
).encode()

cmd = r"""@echo off
setlocal DisableDelayedExpansion
if not exist "%~dp0bis-install\INSTALL-WINDOWS.ps1" goto missing
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0bis-install\INSTALL-WINDOWS.ps1"
if errorlevel 1 goto failed
exit /b 0
:missing
echo Extract all files from the ZIP before starting installation.
:failed
echo BIS setup stopped. See the message above.
pause
exit /b 1
"""
guide = rf"""BIS · пробная версия АОСР для Windows 11 x64

1. Извлеките весь ZIP в одну папку.
2. Дважды щёлкните Install-BIS.cmd. После установки BIS откроется на http://localhost:8082.
3. Для следующего запуска используйте ярлык BIS-AOSR-Trial на рабочем столе.
4. Откройте «Базы данных» → «База организаций» → «Загрузить файлы».
5. Выберите Учебный-бланк-АОСР.docx из папки Примеры. Раскройте «Файлы и ошибки чтения» и нажмите «Настроить АОСР».
6. Проверьте карту, скачайте пробный DOCX и утвердите. Если модель не подключена, назначьте «Выполненные работы» абзацу «Описание результата на участке».

Пробная установка использует C:\BIS-AOSR-Trial и отдельную базу. Для подсказок ИИ настройте модель в пробной копии.
В этом комплекте нет встроенного FFmpeg для голосового режима.
SHA: {release['source_commit']} (также в bis-install\RELEASE.json).
"""
output = downloads / 'BIS-AOSR-Windows11.zip'
temporary = stage / output.name
with ZipFile(temporary, 'w', ZIP_DEFLATED) as z:
    z.writestr('Install-BIS.cmd', cmd.replace('\n', '\r\n').encode('ascii'))
    z.writestr('Начало работы.txt', ('\ufeff' + guide.replace('\n', '\r\n')).encode())
    for name, content in sorted(files.items()):
        method = ZIP_STORED if name.endswith(('.whl', '.zip')) else ZIP_DEFLATED
        z.writestr('bis-install/' + name, content, compress_type=method)
    for sample in sorted((downloads / 'BIS-AOSR').glob('*.docx')):
        z.writestr('Примеры/' + sample.name, sample.read_bytes(), compress_type=ZIP_STORED)
with ZipFile(temporary) as z:
    assert z.testzip() is None
    assert len(z.namelist()) == len(set(x.casefold() for x in z.namelist()))
    for checksum_entry in z.read('bis-install/SHA256SUMS.txt').decode().splitlines():
        digest, name = checksum_entry.split('  ', 1)
        assert hashlib.sha256(z.read('bis-install/' + name)).hexdigest() == digest, name
    assets = re.findall(rb'(?:src|href)="/([^"]+)"', z.read('bis-install/deploy/web/index.html'))
    assert assets and all('bis-install/deploy/web/' + a.decode() in z.namelist() for a in assets)
    z.extract('bis-install/INSTALL-WINDOWS.ps1', stage)
    z.extract('bis-install/RELEASE.json', stage)
    z.extract('bis-install/bootstrap-trial.py', stage)
print(f'{temporary} {temporary.stat().st_size} bytes | {hashlib.sha256(temporary.read_bytes()).hexdigest()}')
