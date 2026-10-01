from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
import build
import piluli
from piluli_core import PiManager
from pilulit import clip, terminal_text


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        project, agent, home = (self.root / name for name in ("project", "agent", "home"))
        for directory in (project, agent, home):
            directory.mkdir()
        (project / ".git").mkdir()
        self.manager = PiManager(agent, project, home=home)
        self.server = piluli.PiluliServer(("127.0.0.1", 0), self.manager, "test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path, body=None, headers=None):
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers={"X-Piluli-Token": "test-token", "Content-Type": "application/json", **(headers or {})})
        return urllib.request.urlopen(request, timeout=5)

    def test_scopes_and_assets(self):
        for scope in ("user", "project"):
            with self.request("/api/state?scope=" + scope) as response:
                state = json.load(response)["data"]
                self.assertEqual(state["scope"], scope)
                self.assertEqual(state["settingsPath"], str(self.manager.paths[scope]))
        for path in ("/", "/styles.css", "/app.js"):
            with self.request(path) as response:
                self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
                data = response.read().decode()
                if path == "/":
                    self.assertIn('content="test-token"', data)
                    self.assertNotIn("__PILULI", data)
                self.assertTrue(data)

    def test_api_rejects_foreign_host_origin_token_and_scope(self):
        for headers in ({"Host": "evil.test"}, {"Origin": "https://evil.test"}, {"Origin": "http://127.0.0.1:42"}, {"X-Piluli-Token": "wrong"}):
            with self.subTest(headers=headers), self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/state?scope=user", headers=headers)
            self.assertEqual(error.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/state?scope=invalid")
        self.assertEqual(error.exception.code, 400)

    def test_batch_apply_to_requested_scope_only(self):
        with self.request("/api/state?scope=user") as response:
            state = json.load(response)["data"]
        row = state["resources"]["extensions"][0]
        with self.request("/api/apply", {"scope": "user", "revision": state["revision"], "changes": [{"id": row["id"], "enabled": False}]}) as response:
            self.assertTrue(json.load(response)["ok"])
        self.assertTrue(self.manager.paths["user"].exists())
        self.assertFalse(self.manager.paths["project"].exists())
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/apply", {"scope": "project", "changes": [], "revision": "stale"})
        self.assertEqual(error.exception.code, 400)

    def test_malformed_body_and_no_scope_do_not_write(self):
        for body in ([], {"changes": []}, {"scope": "project", "changes": [{"id": "missing", "enabled": False}], "revision": self.manager.revision()}):
            with self.assertRaises(urllib.error.HTTPError):
                self.request("/api/apply", body)
        self.assertFalse(self.manager.paths["project"].exists())
        self.assertFalse(self.manager.paths["user"].exists())


class BuildTests(unittest.TestCase):
    def test_product_copy_is_english_and_web_has_no_taglines(self):
        paths = [*ROOT.joinpath("src").glob("*.py"), *ROOT.joinpath("web").glob("*"), ROOT / "README.md"]
        for path in paths:
            with self.subTest(path=path.name):
                self.assertNotRegex(path.read_text(), r"[\u0400-\u04ff]")
        html = (ROOT / "web/index.html").read_text()
        self.assertIn('<html lang="en">', html)
        self.assertNotIn('class="sidebar-foot"', html)
        self.assertNotIn('class="eyebrow"', html)

    def test_both_artifacts_are_independent_and_deterministic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for tui in (False, True):
                path = root / ("pilulit.py" if tui else "piluli.py")
                build.build(path, tui=tui)
                first = path.read_bytes()
                build.build(path, tui=tui)
                self.assertEqual(path.read_bytes(), first)
                self.assertNotIn("from piluli_core import", first.decode())
                result = subprocess.run([sys.executable, str(path), "--help"], cwd=root, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--scope", result.stdout)
                if not tui:
                    spec = importlib.util.spec_from_file_location("built_piluli", path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    for name in ("index.html", "styles.css", "app.js"):
                        self.assertEqual(module.load_asset(name), (ROOT / "web" / name).read_text())
                else:
                    result = subprocess.run([sys.executable, str(path)], cwd=root, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("interactive terminal", result.stderr)

    def test_terminal_control_sanitization_and_cell_width(self):
        self.assertNotIn("\x1b", terminal_text("\x1b[31mBad\r\n"))
        self.assertEqual(clip("猫AB", 3), "猫A")
        self.assertEqual(clip("Привет", 3), "При")


if __name__ == "__main__":
    unittest.main()
