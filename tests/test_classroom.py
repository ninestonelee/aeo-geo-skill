"""Offline regression checks for the student installation and demo workflow."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RUNNER = '''
import pathlib, runpy, sys
def audit(event, args):
    if event in ("socket.connect", "socket.getaddrinfo", "socket.sendto"):
        raise RuntimeError("FORBIDDEN_NETWORK_ATTEMPT")
sys.addaudithook(audit)
original = pathlib.Path.read_text
def read_text(path, *args, **kwargs):
    if path.name == ".env" or path.parent.name == ".env.central":
        raise RuntimeError("FORBIDDEN_CREDENTIAL_FILE_READ")
    return original(path, *args, **kwargs)
if sys.argv[2] == "demo":
    pathlib.Path.read_text = read_text
script = sys.argv[1]
sys.argv = [script] + sys.argv[3:]
runpy.run_path(script, run_name="__main__")
'''


class ClassroomTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.monitor = self.base / "monitor"
        self.monitor.mkdir()
        shutil.copy2(ROOT / "aeo-geo/scripts/aeo_service.py", self.monitor / "aeo_service.py")
        shutil.copy2(ROOT / "aeo-geo/templates/questions.json", self.monitor / "questions.json")
        self.environment = {
            "PATH": os.defpath,
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        for key in ("SystemRoot", "WINDIR", "TEMP", "TMP"):
            if key in os.environ:
                self.environment[key] = os.environ[key]

    def engine(self, *args, demo_guard=False, env=None):
        result = subprocess.run(
            [sys.executable, "-c", RUNNER, str(self.monitor / "aeo_service.py"),
             "demo" if demo_guard else "normal", *args],
            cwd=self.monitor, env=env or self.environment, input="",
            text=True, capture_output=True, timeout=30,
        )
        self.assertNotIn("FORBIDDEN_", result.stdout + result.stderr)
        return result

    def test_demo_skips_credentials_network_and_telegram(self):
        (self.monitor / ".env").write_text("CLASSROOM_MARKER=unused\n")
        env = dict(self.environment, TELEGRAM_BOT_TOKEN="not-a-real-token", TELEGRAM_CHAT_ID="not-a-real-chat")
        result = self.engine("all", "--demo", "--date", "2026-11-20", demo_guard=True, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("[telegram]", result.stdout)
        path = self.monitor / "data/default/demo-runs/2026-11-20.json"
        data = json.loads(path.read_text())
        self.assertEqual(data["mode"], "demo")
        self.assertEqual(len(data["records"]), 36)
        self.assertFalse(any("error" in record for record in data["records"]))
        self.assertTrue((self.monitor / "reports/default/demo/dashboard.html").is_file())
        self.assertTrue((self.monitor / "reports/default/demo/2026-11-20_AEO리포트.md").is_file())
        self.assertFalse((self.monitor / "data/default/runs").exists())
        report = self.engine("report", "--demo", demo_guard=True, env=env)
        self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
        self.assertNotIn("[telegram]", report.stdout)

    def test_doctor_and_health_do_not_require_deployment(self):
        result = self.engine("doctor")
        self.assertEqual(result.returncode, 1)
        self.assertIn("OPENAI_API_KEY", result.stdout)
        self.assertNotIn("VERCEL", result.stdout)
        self.assertEqual(self.engine("health").returncode, 0)

    def test_question_add_and_duplicate_protection(self):
        question = "서울에서 직장인이 주말에 들을 AI 마케팅 실습 강의를 추천해줘"
        args = ("keyword", "add", "--text", question, "--tier", "니치", "--watch")
        self.assertEqual(self.engine(*args).returncode, 0)
        path = self.monitor / "questions.json"
        data = json.loads(path.read_text())
        self.assertEqual(len(data["questions"]), 4)
        self.assertTrue(data["questions"][-1]["watch"])
        self.assertEqual(self.engine(*args).returncode, 0)
        self.assertEqual(len(json.loads(path.read_text())["questions"]), 4)
        self.assertIn("q04", self.engine("keyword", "list").stdout)

    def test_multiple_panel_output_is_isolated(self):
        panels = self.monitor / "panels"
        panels.mkdir()
        data = json.loads((self.monitor / "questions.json").read_text())
        data["questions"] = data["questions"][:1]
        (panels / "brand-b.json").write_text(json.dumps(data))
        result = self.engine("all", "--demo", "--questions", "panels/brand-b.json", demo_guard=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.monitor / "reports/brand-b/demo/dashboard.html").is_file())
        self.assertFalse((self.monitor / "reports/default").exists())

    def test_installer_copies_skill_and_refuses_overwrite(self):
        target = self.base / "installation"
        command = [sys.executable, str(ROOT / "scripts/install.py"), "--dest", str(target)]
        result = subprocess.run(command, env=self.environment, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        source_hash = hashlib.sha256((ROOT / "aeo-geo/scripts/aeo_service.py").read_bytes()).digest()
        self.assertEqual(hashlib.sha256((target / "scripts/aeo_service.py").read_bytes()).digest(), source_hash)
        marker = target / "student-notes.txt"
        marker.write_text("keep")
        second = subprocess.run(command, env=self.environment, text=True, capture_output=True, timeout=30)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(marker.read_text(), "keep")

    def test_codex_and_claude_default_install_locations(self):
        for app in ("codex", "claude"):
            command = [sys.executable, str(ROOT / "scripts/install.py"), "--app", app]
            result = subprocess.run(command, env=self.environment, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((self.home / ("." + app) / "skills/aeo-geo/SKILL.md").is_file())

    def test_release_is_reproducible_and_contains_only_skill_files(self):
        outputs = [self.base / "first", self.base / "second"]
        for output in outputs:
            command = [sys.executable, str(ROOT / "scripts/build_release.py"),
                       "--version", "v1.0.0", "--output-dir", str(output)]
            result = subprocess.run(command, env=self.environment, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first = (outputs[0] / "aeo-geo-skill.zip").read_bytes()
        self.assertEqual(first, (outputs[1] / "aeo-geo-skill.zip").read_bytes())
        self.assertTrue((outputs[0] / "SHA256SUMS").read_text().startswith(hashlib.sha256(first).hexdigest()))
        with zipfile.ZipFile(outputs[0] / "aeo-geo-skill.zip") as bundle:
            self.assertEqual(len(bundle.namelist()), 9)
            self.assertIn("aeo-geo/SKILL.md", bundle.namelist())
            self.assertIn("aeo-geo/templates/.env.example", bundle.namelist())
            for name in bundle.namelist():
                self.assertTrue(name.startswith("aeo-geo/"))
                self.assertNotIn("..", Path(name).parts)
                self.assertNotIn("INSTALLATION.json", name)


if __name__ == "__main__":
    unittest.main()
