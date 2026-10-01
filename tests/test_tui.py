"""Drive the real terminal controller with deterministic stdlib-only input."""
import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

try:
    import curses
except ImportError:
    curses = None

import test_core
from pilulit import TerminalApp


class Screen:
    def __init__(self, keys):
        self.keys = iter(keys)
        self.lines = []
        self.calls = []

    def get_wch(self):
        return next(self.keys)

    def getmaxyx(self):
        return 30, 120

    def addstr(self, y, x, text, attr=0):
        self.lines.append(text)
        self.calls.append((y, x, text, attr))

    def erase(self):
        self.lines.clear()
        self.calls.clear()

    def refresh(self):
        pass

    def keypad(self, value):
        pass

    def timeout(self, value):
        pass


@unittest.skipUnless(curses, "stdlib curses not available")
class TuiTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_core.CoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.package()

    def make_app(self, keys=(), *, colors=False, default_error=None, cursor_error=None, pair_error=None):
        with ExitStack() as stack:
            stack.enter_context(patch.object(curses, "curs_set", side_effect=cursor_error))
            stack.enter_context(patch.object(curses, "set_escdelay"))
            stack.enter_context(patch.object(curses, "has_colors", return_value=colors))
            stack.enter_context(patch.object(curses, "start_color"))
            stack.enter_context(patch.object(curses, "use_default_colors", side_effect=default_error))
            self.init_pair = stack.enter_context(patch.object(curses, "init_pair", side_effect=pair_error))
            stack.enter_context(patch.object(curses, "color_pair", side_effect=lambda number: number << 8))
            return TerminalApp(Screen(keys), curses, self.fixture.manager, "project")

    def run_keys(self, keys):
        app = self.make_app(keys)
        app.run()
        return app

    def test_monochrome_fallback(self):
        app = self.make_app()
        self.init_pair.assert_not_called()
        self.assertEqual(app.styles["selected"], curses.A_REVERSE)
        self.assertEqual(app.styles["muted"], curses.A_DIM)
        app.render()
        self.assertTrue(app.screen.calls)

    def test_palette_uses_terminal_colors_and_default_background(self):
        app = self.make_app(colors=True, cursor_error=curses.error("cursor unsupported"))
        self.assertEqual(self.init_pair.call_count, 6)
        self.init_pair.assert_any_call(1, curses.COLOR_CYAN, -1)
        self.init_pair.assert_any_call(5, curses.COLOR_WHITE, curses.COLOR_BLUE)
        self.assertEqual(app.styles["enabled"], (2 << 8) | curses.A_BOLD)
        self.assertEqual(app.styles["error"], (4 << 8) | curses.A_BOLD)

    def test_unsupported_default_background_keeps_color(self):
        app = self.make_app(colors=True, default_error=curses.error("unsupported"))
        self.init_pair.assert_any_call(1, curses.COLOR_CYAN, curses.COLOR_BLACK)
        self.assertEqual(app.styles["accent"], (1 << 8) | curses.A_BOLD)

    def test_pair_limit_preserves_individual_fallbacks(self):
        def limited(number, foreground, background):
            if number > 2:
                raise ValueError("pair limit")
        app = self.make_app(colors=True, pair_error=limited)
        self.assertEqual(app.styles["accent"], (1 << 8) | curses.A_BOLD)
        self.assertEqual(app.styles["selected"], curses.A_REVERSE)
        app.render()

    def test_render_colors_status_selection_tabs_and_messages(self):
        app = self.make_app(colors=True)
        rows = app.rows()
        app.draft.toggle(rows[1])
        rows[2]["enabled"] = False
        app.notice("Validation failed", "error")
        app.render()
        calls, s = app.screen.calls, app.styles
        self.assertIn((6, 2, "  [x]", s["enabled"]), calls)
        self.assertIn((7, 2, "* [ ]", s["pending"]), calls)
        self.assertIn((8, 2, "  [ ]", s["muted"]), calls)
        self.assertTrue(any(y == 6 and attr == s["selected"] for y, _, _, attr in calls))
        self.assertIn((3, 2, "[Extensions]", s["selected"]), calls)
        self.assertIn((25, 2, "Validation failed", s["error"]), calls)
        self.assertIn((27, 2, "↑↓", s["accent"]), calls)

    def test_confirmation_uses_warning_panel(self):
        app = self.make_app(["n"], colors=True)
        self.assertIsNone(app.ask("Remove package?", confirm=True))
        self.assertTrue(any("Remove package?" in text and attr == app.styles["dialog"] for _, _, text, attr in app.screen.calls))

    def test_refresh_failure_keeps_error_message(self):
        app = self.make_app(list("r\nq"), colors=True)
        with patch.object(app, "load", side_effect=OSError("Read failed")):
            app.run()
        self.assertEqual(app.message, "Read failed")
        self.assertEqual(app.message_tone, "error")

    def test_csi_arrows_space_enter_and_scope(self):
        # Two CSI Downs select llama.cpp (after codemode/demo), Right selects Skills. Stage both, apply,
        # switch to user, stage one skill, apply. All without real curses setup.
        self.run_keys(list("\x1b[B\x1b[B \x1b[C \ng \nq"))
        project = json.loads(self.fixture.manager.paths["project"].read_text())
        user = json.loads(self.fixture.manager.paths["user"].read_text())
        self.assertEqual(project["extensions"], ["-builtin:llama.cpp"])
        self.assertEqual(project["packages"][0]["skills"], ["-skills/one/SKILL.md"])
        self.assertEqual(user["packages"][0]["skills"], ["-skills/one/SKILL.md"])
        self.assertNotIn("extensions", user)

    def test_cancel_scope_switch_then_discard_on_quit(self):
        app = self.run_keys(list(" gnqy"))
        self.assertEqual(app.scope, "project")
        self.assertFalse(self.fixture.manager.paths["project"].exists())
        self.assertEqual(app.draft.changes, {})

    def test_install_uses_selected_scope_and_confirmation(self):
        with patch.object(self.fixture.manager, "_run", return_value="done") as run:
            self.run_keys(list("ginpm:new\nyq"))
        run.assert_called_once_with(["install", "npm:new", "--no-approve"])


if __name__ == "__main__":
    unittest.main()
