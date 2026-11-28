"""Wrap the repository ZIP in a pinned offline updater using a verified wheelhouse."""
from pathlib import Path
import hashlib
import sys
from zipfile import ZIP_DEFLATED, ZipFile

source, standard, previous, wheel, output = map(Path, sys.argv[1:])
commit = "a32e07613f1cdfda1fbd7b657b71f3524cf2031c"
prior_sha = "98e66b9f0612c8fc284c720d398c8517d107209b2ca3d3ec0e03eebb162a654d"
assert hashlib.sha256(previous.read_bytes()).hexdigest() == prior_sha
with ZipFile(standard) as base:
    assert base.testzip() is None
    files = {name: base.read(name) for name in base.namelist() if name != "bis-install/SHA256SUMS.txt"}
assert commit.encode() in files["bis-install/RELEASE.json"]
with ZipFile(previous) as old:
    for name in old.namelist():
        if name.startswith("bis-install/wheels/") and name.endswith(".whl") and not Path(name).name.startswith("bis-"):
            assert len(Path(name).parts) == 3
            files[name] = old.read(name)
files[f"bis-install/wheels/{wheel.name}"] = wheel.read_bytes()
files["bis-install/start_release.py"] = (source / "start_release.py").read_bytes()
files["bis-install/SHA256SUMS.txt"] = "".join(
    f"{hashlib.sha256(data).hexdigest()}  {name.removeprefix('bis-install/')}\n"
    for name, data in sorted(files.items())
).encode("utf-8")
files["Update-BIS.ps1"] = (source / "Update-BIS.ps1").read_bytes()
files["Install-BIS.cmd"] = (
    b'@echo off\r\nsetlocal\r\n'
    b'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Update-BIS.ps1"\r\n'
    b'if errorlevel 1 pause\r\n'
)
output.parent.mkdir(parents=True, exist_ok=True)
with ZipFile(output, "w", ZIP_DEFLATED) as bundled:
    for name, data in sorted(files.items()):
        bundled.writestr(name, data)
with ZipFile(output) as bundled:
    assert bundled.testzip() is None
digest = hashlib.sha256(output.read_bytes()).hexdigest()
output.with_suffix(".zip.sha256").write_text(f"{digest}  {output.name}\n", encoding="ascii")
