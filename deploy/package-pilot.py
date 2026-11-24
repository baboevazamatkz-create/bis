"""Build the Windows pilot ZIP from a committed source revision and its prebuilt UI."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

from check_repository_files import check_files


def main() -> None:
    repo = Path(__file__).resolve().parent.parent
    output = Path(sys.argv[1]).resolve()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    source = subprocess.check_output(["git", "archive", "--format=zip", commit], cwd=repo)
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]
        check_files(names, archive.read)
        files = {name: archive.read(name) for name in names}
    files["INSTALL-WINDOWS.ps1"] = files["deploy/pilot/INSTALL-WINDOWS.ps1"]
    files["FIRST-START.md"] = files["docs/PILOT_WINDOWS.md"]
    files["INSTALL.cmd"] = (
        b'@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass '
        b'-File "%~dp0INSTALL-WINDOWS.ps1"\r\npause\r\n'
    )
    manifest = {
        "release_id": f"pto_{commit[:12]}",
        "source_commit": commit,
        "latest_migration": "0008_project_readings",
        "frontend_prebuilt": True,
        "python_auto_install": True,
        "windows_installer_runtime_tested": False,
    }
    files["RELEASE.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    files["SHA256SUMS.txt"] = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {name}\n" for name, content in sorted(files.items())
    ).encode()
    output.mkdir(parents=True, exist_ok=True)
    target = output / "BIS-Pilot-Windows.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            archive.writestr(f"bis-install/{name}", content)
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise SystemExit("Archive verification failed.")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".zip.sha256").write_text(f"{digest}  {target.name}\n", encoding="ascii")
    print(f"Packaged {len(files)} files from {commit}: {target}")


if __name__ == "__main__":
    main()
