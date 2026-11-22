"""Reject common private files and high-confidence secret markers before release."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from fnmatch import fnmatchcase
from pathlib import Path

PUBLIC_ENV_EXAMPLE = "deploy/.env.example"
PRIVATE_DIRECTORIES = frozenset({
    ".aws", ".cache", ".git", ".gnupg", ".kube", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".ssh", ".tox", ".venv", "__pycache__", "backups", "build",
    "artifacts", "data", "dist", "dist-demo", "dumps", "node_modules", "outputs",
    "secrets", "storage", "temp", "tmp", "uploads", "user-files", "venv",
})
PRIVATE_NAMES = (
    ".env", ".env.*", "*.env", ".envrc", ".pgpass", ".netrc", ".npmrc", ".pypirc",
    "credentials.json", "secrets.json", "service-account*.json",
    "id_rsa", "id_ecdsa", "id_ed25519", "id_dsa",
    ".coverage", ".coverage.*", "*.egg-info", "*.tsbuildinfo",
    "demo-snapshot.json", "bis-demo.html", ".ds_store", "thumbs.db",
)
PRIVATE_SUFFIXES = (
    ".bak", ".backup", ".bkp", ".orig", ".swp", ".tmp", ".log", "~",
    ".dump", ".dmp", ".sql", ".db", ".db-journal", ".db-shm", ".db-wal",
    ".sqlite", ".sqlite3", ".sqlite-journal", ".sqlite-shm", ".sqlite-wal",
    ".sqlite3-journal", ".sqlite3-shm", ".sqlite3-wal",
    ".key", ".pem", ".p12", ".pfx", ".kdbx",
    ".pdf", ".doc", ".docx", ".docm", ".rtf", ".odt",
    ".xls", ".xlsx", ".xlsm", ".ods", ".csv", ".tsv",
    ".ppt", ".pptx", ".dwg", ".dxf", ".ifc",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz",
    ".pid", ".pyc", ".pyo",
)
CREDENTIAL_MARKERS = re.compile(
    rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
    rb"|(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{60,255})"
    rb"|(?<![A-Za-z0-9_])AQVN[A-Za-z0-9_-]{32,124}(?![A-Za-z0-9_-])"
    rb"|(?<![A-Za-z0-9_])[1-9][0-9]{7,11}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"
    rb"|(?<![A-Za-z0-9_-])sk-(?:(?:proj-|svcacct-|ant-api[0-9]+-)[A-Za-z0-9_-]{32,255}|[A-Za-z0-9]{46,255})"
)


def _private_path(name: str) -> bool:
    if name == PUBLIC_ENV_EXAMPLE:
        return False
    if "\\" in name or ":" in name:
        return True
    # Windows ignores case and trailing dots and spaces in ordinary filenames.
    parts = [part.rstrip(" .").casefold() for part in name.split("/")]
    return any(
        not part
        or part in PRIVATE_DIRECTORIES
        or part.endswith(PRIVATE_SUFFIXES)
        or any(fnmatchcase(part, pattern) for pattern in PRIVATE_NAMES)
        for part in parts
    )


def check_files(names: Iterable[str], read_file: Callable[[str], bytes]) -> None:
    blocked = {}
    for name in names:
        if CREDENTIAL_MARKERS.search(os.fsencode(name)):
            blocked[name] = "possible credential in path"
            continue
        if _private_path(name):
            blocked[name] = "restricted path or file type"
            continue
        content = read_file(name)
        if content.startswith((b"PGDMP", b"SQLite format 3\x00", b"%PDF-")) or (
            b"-- PostgreSQL database dump" in content[:2048]
        ):
            blocked[name] = "database dump or PDF signature"
        elif CREDENTIAL_MARKERS.search(content):
            blocked[name] = "possible credential"
    if blocked:
        print("Repository file check failed:", file=sys.stderr)
        for name, reason in sorted(blocked.items()):
            # Quote paths so control characters cannot become terminal or CI commands.
            redacted = os.fsdecode(CREDENTIAL_MARKERS.sub(b"<redacted>", os.fsencode(name)))
            print(f"  {ascii(redacted)} ({reason})", file=sys.stderr)
        raise SystemExit(1)


def main() -> None:
    repo = Path(__file__).resolve().parent.parent
    tracked = subprocess.check_output(["git", "ls-files", "--cached", "-z"], cwd=repo)
    names = [os.fsdecode(name) for name in tracked.split(b"\x00") if name]

    def read_index(name: str) -> bytes:
        return subprocess.check_output(["git", "show", f":{name}"], cwd=repo)

    check_files(names, read_index)
    print(f"Repository file check passed ({len(names)} tracked files).")


if __name__ == "__main__":
    main()
