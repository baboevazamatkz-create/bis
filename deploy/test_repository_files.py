"""Exercise the tracked-file gate and release packager against temporary Git repositories."""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent


class RepositoryFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        for name in ("package-pilot.py", "check_repository_files.py"):
            script = DEPLOY / name
            if script.is_file():
                target = self.repo / "deploy" / name
                target.parent.mkdir(exist_ok=True)
                shutil.copyfile(script, target)
        self.write(".gitignore", b"outputs/\n.env*\n")
        self.write("deploy/.env.example", b"BIS_SECRET_KEY=replace-on-deploy\n")
        self.write("deploy/web/index.html", b"<!doctype html>")
        self.write("backend/bis/demo/demo_contract.txt", b"Synthetic example.")
        self.write("docs/PILOT_WINDOWS.md", b"# Pilot")
        self.write("deploy/pilot/INSTALL-WINDOWS.ps1", b"# Installer")
        self.git("add", "-f", ".")
        self.commit()
        self.output = Path(self.temp.name) / "output"
        self.archive = self.output / "BIS-Pilot-Windows.zip"

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(content)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def commit(self):
        self.git("-c", "user.name=BIS Test", "-c", "user.email=bis@example.invalid", "commit", "-qm", "fixture")

    def script(self, name, *args):
        return subprocess.run(
            [sys.executable, str(self.repo / "deploy" / name), *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )

    def test_gate_blocks_staged_paths_even_when_force_added_or_hidden_from_git_archive(self):
        blocked = (
            "frontend/.ENV.LOCAL",
            "backend/config/.env.production",
            "backend/.env.local/settings.txt",
            "deployment.env",
            "sub/backup.dump",
            "sub/postgres.sql",
            "local.sqlite3-wal",
            "stage/OUTPUTS/scratch.txt",
            "workspace/Uploads/raw.txt",
            "sub/Customer.PDF",
            "sub/case.DOCX",
            "local/id_ed25519",
            "sub/.Ruff_cache/index",
        )
        for path in blocked:
            self.write(path, b"example")
        self.write(".gitattributes", b"sub/backup.dump export-ignore\n")
        self.git("add", "-f", ".")
        self.commit()
        result = self.script("check_repository_files.py")
        self.assertEqual(result.returncode, 1, result.stderr)
        for path in blocked:
            with self.subTest(path=path):
                self.assertIn(path, result.stderr)

    def test_gate_checks_staged_content_instead_of_untracked_worktree_bytes(self):
        fake_key = "gh" + "p_" + "a" * 36
        self.write("deploy/public.json", ('{"credential":"' + fake_key + '"}').encode())
        self.git("add", "deploy/public.json")
        self.write("deploy/public.json", b'{"credential":"placeholder"}')
        result = self.script("check_repository_files.py")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("deploy/public.json", result.stderr)
        self.assertNotIn(fake_key, result.stderr + result.stdout)

    def test_gitignore_excludes_mixed_case_local_files_and_preserves_public_example(self):
        shutil.copyfile(DEPLOY.parent / ".gitignore", self.repo / ".gitignore")
        for path in ("sub/BackUp.DuMp", "sub/UpLoAds/example.txt", "sub/Case.DoCx", "sub/.ENV.LOCAL"):
            with self.subTest(path=path):
                result = self.git("check-ignore", "--no-index", path)
                self.assertEqual(result.stdout.decode().strip(), path)
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", "deploy/.env.example"], cwd=self.repo
        )
        self.assertEqual(result.returncode, 1)

    def test_gate_rejects_high_confidence_telegram_and_yandex_credentials(self):
        telegram = "1234567890" + ":" + "t" * 35
        yandex = "AQ" + "VN" + "y" * 38
        self.write("deploy/public-yandex.json", ('{"token":"' + yandex + '"}').encode())
        self.write("deploy/public-telegram.json", ('{"token":"' + telegram + '"}').encode())
        self.write("docs/placeholders.md", b"Example: AQVN... and 1234567890:BOT_TOKEN")
        self.git("add", ".")
        result = self.script("check_repository_files.py")
        self.assertEqual(result.returncode, 1, result.stderr)
        for path in ("deploy/public-yandex.json", "deploy/public-telegram.json"):
            self.assertIn(path, result.stderr)
        self.assertNotIn("docs/placeholders.md", result.stderr)
        for secret in (telegram, yandex):
            self.assertNotIn(secret, result.stderr + result.stdout)

    def test_gate_rejects_a_credential_in_a_filename_without_disclosing_it(self):
        fake_key = "gh" + "p_" + "b" * 36
        self.write(f"backend/{fake_key}.txt", b"example")
        self.git("add", ".")
        result = self.script("check_repository_files.py")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("backend/<redacted>.txt", result.stderr)
        self.assertNotIn(fake_key, result.stderr + result.stdout)

    def test_packager_rejects_private_committed_data_without_creating_a_zip(self):
        self.write("workspace/.ENV.LOCAL", b"private-local-config")
        self.git("add", "-f", "workspace/.ENV.LOCAL")
        self.commit()
        (self.repo / "workspace/.ENV.LOCAL").unlink()
        result = self.script("package-pilot.py", str(self.output))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("workspace/.ENV.LOCAL", result.stderr)
        self.assertNotIn("private-local-config", result.stderr + result.stdout)
        self.assertFalse(self.archive.exists())

    def test_packager_checks_committed_secret_content_and_hides_values(self):
        marker = "-----BEGIN " + "OPENSSH " + "PRIVATE KEY-----"
        self.write("public.txt", marker.encode())
        self.git("add", "public.txt")
        self.commit()
        result = self.script("package-pilot.py", str(self.output))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("public.txt", result.stderr)
        self.assertNotIn(marker, result.stderr + result.stdout)
        self.assertFalse(self.archive.exists())

    def test_clean_tree_preserves_pilot_package_contract(self):
        self.write("outputs/local-not-tracked.txt", b"ignored")
        gate = self.script("check_repository_files.py")
        self.assertEqual(gate.returncode, 0, gate.stderr)
        result = self.script("package-pilot.py", str(self.output))
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(self.archive) as archive:
            self.assertIsNone(archive.testzip())
            for path in (
                "deploy/.env.example",
                "deploy/web/index.html",
                "backend/bis/demo/demo_contract.txt",
                "INSTALL-WINDOWS.ps1",
                "INSTALL.cmd",
                "FIRST-START.md",
                "SHA256SUMS.txt",
            ):
                self.assertIn(f"bis-install/{path}", archive.namelist())
            self.assertNotIn("bis-install/outputs/local-not-tracked.txt", archive.namelist())
            manifest = json.loads(archive.read("bis-install/RELEASE.json"))
            self.assertEqual(manifest["source_commit"], self.git("rev-parse", "HEAD").stdout.decode().strip())
        self.assertTrue(
            self.archive.with_suffix(".zip.sha256").read_text().startswith(
                hashlib.sha256(self.archive.read_bytes()).hexdigest()
            )
        )


if __name__ == "__main__":
    unittest.main()
