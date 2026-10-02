from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
import build
import piluli
from piluli_core import PiManager
from pilulit import clip, terminal_text


class WebEndpointTests(unittest.TestCase):
    @staticmethod
    def args(endpoint=(), host=None, port=None):
        return SimpleNamespace(web_endpoint=list(endpoint), host=host, port=port)

    def test_defaults_and_environment_override(self):
        self.assertEqual(piluli.resolve_web_endpoint(self.args(), {}), ("127.0.0.1", 5432))
        self.assertEqual(
            piluli.resolve_web_endpoint(self.args(), {"PILULI_WEB": "devbox.local:8123"}),
            ("devbox.local", 8123),
        )

    def test_cli_endpoint_overrides_environment(self):
        environment = {"PILULI_WEB": "localhost:7000"}
        self.assertEqual(
            piluli.resolve_web_endpoint(self.args(("on", "127.0.0.1", "9000")), environment),
            ("127.0.0.1", 9000),
        )
        self.assertEqual(
            piluli.resolve_web_endpoint(self.args(("on", "9001")), environment),
            ("localhost", 9001),
        )
        self.assertEqual(
            piluli.resolve_web_endpoint(self.args(host="devbox.local", port=9002), environment),
            ("devbox.local", 9002),
        )

    def test_port_must_be_numeric_and_in_range(self):
        for value in ("nope", "0", "65536", "-1", "1.5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                piluli.parse_port(value)
        self.assertEqual(piluli.parse_port("1"), 1)
        self.assertEqual(piluli.parse_port("65535"), 65535)

    def test_environment_rejects_wildcard_and_bad_format(self):
        for value in ("0.0.0.0:5432", "0:5432", "localhost", "localhost:nope"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                piluli.parse_environment_endpoint(value)

    def test_wildcard_requires_interactive_yes(self):
        interactive = SimpleNamespace(isatty=lambda: True)
        noninteractive = SimpleNamespace(isatty=lambda: False)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(piluli.confirm_wildcard("0.0.0.0", stdin=interactive, input_fn=lambda _: "yes"))
            self.assertFalse(piluli.confirm_wildcard("0.0.0.0", stdin=interactive, input_fn=lambda _: "no"))
            self.assertFalse(piluli.confirm_wildcard("0.0.0.0", stdin=noninteractive))
        self.assertTrue(piluli.confirm_wildcard("127.0.0.1", stdin=noninteractive))

    def test_occupied_port_is_rejected_before_serving(self):
        with socket.socket() as occupied, tempfile.TemporaryDirectory() as directory:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = piluli.main(
                    ["--no-browser", "--project-dir", directory, "--agent-dir", directory, "on", "127.0.0.1", str(port)],
                    environ={},
                )
        self.assertEqual(result, 1)
        self.assertIn("already in use", stderr.getvalue())


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

    def test_wildcard_accepts_matching_network_host_and_origin(self):
        self.server.web_host = "0.0.0.0"
        self.server.wildcard_host = True
        authority = f"192.0.2.10:{self.server.server_port}"
        with self.request("/", headers={"Host": authority, "Origin": "http://" + authority}) as response:
            self.assertEqual(response.status, 200)

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

    def test_skills_action_endpoint(self):
        agents = self.root / "home/.agents"
        skill = agents / "skills/managed/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: managed\n---\n# Instructions")
        (agents / ".skill-lock.json").write_text(json.dumps({"version": 3, "skills": {"managed": {"source": "org/repo"}}}))
        with self.request("/api/state?scope=user") as response:
            state = json.load(response)["data"]
        row = state["resources"]["skills"][0]
        self.assertEqual(row["skillsCli"], "org/repo")
        with patch.object(self.manager, "_run", return_value="removed") as run:
            with self.request("/api/skills/action", {"scope": "user", "action": "remove", "id": row["id"], "revision": state["revision"]}) as response:
                self.assertEqual(json.load(response)["message"], "removed")
            run.assert_called_once_with(["-y", "skills", "remove", "managed", "-y", "--global"], executable="npx")
        for body in ({"scope": "user", "action": "remove", "id": "missing", "revision": self.manager.revision()},
                     {"scope": "user", "action": "remove", "id": row["id"], "revision": "stale"}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/skills/action", body)
            self.assertEqual(error.exception.code, 400)


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
