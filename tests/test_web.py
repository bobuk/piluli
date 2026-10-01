"""Optional real-browser checks: PILULI_BROWSER_TESTS=1 python3 -m unittest discover -s tests -p test_web.py -v."""
import os
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
import uuid

import test_core
from piluli import PiluliServer

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("PILULI_BROWSER_TESTS") == "1" and shutil.which("agent-browser"), "Set PILULI_BROWSER_TESTS=1 and install agent-browser to run browser checks")
class WebTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_core.CoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.package(skills=["-skills/two/SKILL.md"])
        self.server = PiluliServer(("127.0.0.1", 0), self.fixture.manager, "browser-test-token")
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(5)
        self.addCleanup(stop)
        self.session = "piluli-test-" + uuid.uuid4().hex[:8]
        self.addCleanup(lambda: self.browser("close"))
        self.browser("open", f"http://127.0.0.1:{self.server.server_port}")

    def browser(self, *args, script=None):
        result = subprocess.run(["agent-browser", "--session", self.session, *args], input=script, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_bulk_and_native_keyboard(self):
        result = self.browser("eval", "--stdin", script=(ROOT / "tests/web_bulk_check.js").read_text())
        self.assertIn("project bulk preserves user settings", result)
        self.browser("press", "Space")
        self.browser("eval", "if (document.querySelectorAll('[data-select]:checked').length !== 1 || document.querySelector('#pending-count').textContent !== '0') throw new Error('Checkbox Space must select, not toggle'); true")
        self.browser("press", "ArrowDown")
        self.browser("press", "Space")
        self.browser("eval", "if (document.querySelector('#pending-count').textContent !== '1') throw new Error('Row Space must toggle'); true")
        self.browser("press", "Enter")
        self.browser("wait", "--fn", "document.querySelector('#pending-count').textContent === '0' && document.querySelector('#list-region').getAttribute('aria-busy') === 'false'")
        skills = self.fixture.manager.state("project")["resources"]["skills"]
        self.assertEqual([row["enabled"] for row in skills], [False, False])
        self.assertTrue(all(row["enabled"] for row in self.fixture.manager.state("user")["resources"]["skills"]))
        self.assertEqual(self.browser("errors").strip(), "")


if __name__ == "__main__":
    unittest.main()
