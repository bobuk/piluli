#!/usr/bin/env python3
"""Pilulit — standalone curses resource manager for Pi, stdlib only."""
from __future__ import annotations

import argparse
import locale
import sys
import textwrap
import unicodedata

from piluli_core import PiManager, PiluliError, Draft, VIEWS, add_common_args  # build:core

LABELS = {"extensions": "Extensions", "skills": "Skills", "prompts": "Prompts", "themes": "Themes", "packages": "Packages"}


def terminal_text(value):
    # Never emit terminal controls from package descriptions or command output.
    return "".join(c if c.isprintable() else " " for c in str(value))


def clip(text, cells):
    result, width = [], 0
    for char in terminal_text(text):
        size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if width + size > cells:
            break
        result.append(char)
        width += size
    return "".join(result)


class TerminalApp:
    def __init__(self, screen, curses, manager, scope):
        self.screen, self.curses, self.manager = screen, curses, manager
        self.scope, self.view, self.query, self.index = scope, "extensions", "", 0
        self.notice("Space to toggle; Enter to apply.")
        self.draft = Draft(manager.state(scope))
        screen.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        curses.set_escdelay(35)
        self.init_styles()

    def init_styles(self):
        c = self.curses
        self.styles = {
            "accent": c.A_BOLD, "enabled": c.A_NORMAL, "muted": c.A_DIM,
            "pending": c.A_BOLD, "error": c.A_BOLD,
            "selected": c.A_REVERSE, "dialog": c.A_REVERSE | c.A_BOLD,
        }
        try:
            if not c.has_colors():
                return
            c.start_color()
        except c.error:
            return
        try:
            c.use_default_colors()
            background = -1
        except c.error:
            background = c.COLOR_BLACK
        # Use the terminal's own ANSI palette; no hard-coded RGBs or background.
        pairs = (
            ("accent", c.COLOR_CYAN, background),
            ("enabled", c.COLOR_GREEN, background),
            ("pending", c.COLOR_YELLOW, background),
            ("error", c.COLOR_RED, background),
            ("selected", c.COLOR_WHITE, c.COLOR_BLUE),
            ("dialog", c.COLOR_BLACK, c.COLOR_YELLOW),
        )
        for number, (role, foreground, bg) in enumerate(pairs, 1):
            try:
                c.init_pair(number, foreground, bg)
                self.styles[role] = c.color_pair(number) | c.A_BOLD
            except (c.error, ValueError):
                # Limited-color terminals keep the monochrome style for this role.
                pass

    def notice(self, message, tone="accent"):
        self.message, self.message_tone = message, tone

    def key(self):
        key = self.screen.get_wch()
        if key != "\x1b":
            return key
        # Accept CSI arrows too: some terminal wrappers send them even after
        # curses enables application-cursor mode (whose arrows start with ESC O).
        sequence = []
        self.screen.timeout(35)
        try:
            while len(sequence) < 4:
                char = self.screen.get_wch()
                sequence.append(char)
                if not isinstance(char, str) or (len(sequence) > 1 and (char.isalpha() or char == "~")):
                    break
        except self.curses.error:
            pass
        finally:
            self.screen.timeout(-1)
        codes = {"A": self.curses.KEY_UP, "B": self.curses.KEY_DOWN, "C": self.curses.KEY_RIGHT, "D": self.curses.KEY_LEFT, "H": self.curses.KEY_HOME, "F": self.curses.KEY_END, "5~": self.curses.KEY_PPAGE, "6~": self.curses.KEY_NPAGE}
        if sequence and sequence[0] in ("[", "O"):
            code = codes.get("".join(str(char) for char in sequence[1:]))
            if code is not None:
                return code
        for char in reversed(sequence):
            self.curses.unget_wch(char)
        return key

    def put(self, y, text, attr=0, x=2):
        height, width = self.screen.getmaxyx()
        if not 0 <= y < height or width <= x + 1:
            return
        try:
            self.screen.addstr(y, x, clip(text, width - x - 1), attr)
        except self.curses.error:
            pass

    def put_parts(self, y, parts):
        # Only fixed UI labels go here (one terminal cell per character).
        x = 2
        for text, attr in parts:
            self.put(y, text, attr, x=x)
            x += len(text)

    def help_line(self, y, text, attr=0):
        parts = []
        for index, item in enumerate(text.split(" · ")):
            if index:
                parts.append((" · ", self.styles["muted"]))
            key, _, label = item.partition(" ")
            parts.extend(((key, self.styles["accent"]), (" " + label, attr)))
        self.put_parts(y, parts)

    def rows(self):
        data = self.draft.state
        values = data["packages"] if self.view == "packages" else data["resources"][self.view]
        return [r for r in values if self.query.casefold() in " ".join(str(r.get(k, "")) for k in ("name", "source", "path", "description")).casefold()]

    def selected(self):
        rows = self.rows()
        self.index = max(0, min(self.index, len(rows) - 1))
        return rows[self.index] if rows else None

    def render(self):
        c, s = self.curses, self.styles
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 17 or width < 64:
            self.put(0, "Pilulit: resize to at least 64×17. Q to quit.", s["pending"])
            self.screen.refresh()
            return
        scope_name = "USER / all projects" if self.scope == "user" else "CURRENT PROJECT"
        self.put_parts(0, (
            ("PILULIT    ", s["accent"]), (scope_name, s["accent"]),
            (f"    unsaved: {len(self.draft.changes)}", s["pending"] if self.draft.changes else s["muted"]),
        ))
        self.put(1, self.draft.state["settingsPath"], s["muted"])
        tabs = []
        for view in VIEWS:
            tabs.append((f"[{LABELS[view]}]" if view == self.view else f" {LABELS[view]} ", s["selected"] if view == self.view else s["muted"]))
            tabs.append(("  ", c.A_NORMAL))
        self.put_parts(3, tabs)
        values = self.rows()
        self.selected()
        self.put(4, f"Search: {self.query or '—'}    {len(values)} resources    {self.index + 1 if values else 0}/{len(values)}", s["muted"])
        self.put(5, "  STATUS   RESOURCE / SOURCE", s["accent"])
        count = height - 14
        start = max(0, min(self.index - count + 1, len(values) - count))
        for offset, row in enumerate(values[start:start + count]):
            pending = row["id"] in self.draft.changes
            enabled = row.get("installed") if self.view == "packages" else self.draft.enabled(row)
            marker = "*" if pending else " "
            origin = "user" if row["origin"] == "user" else row["origin"]
            status = "[x]" if enabled else "[ ]"
            text = f"{marker} {status}  {row['name']}  · {origin}  · {row['source']}"
            attr = s["selected"] if start + offset == self.index else c.A_NORMAL if enabled else s["muted"]
            if pending:
                attr |= c.A_BOLD
            self.put(6 + offset, text.ljust(max(1, width - 4)), attr)
            # Status keeps its semantic color even on the blue selected row.
            self.put(6 + offset, f"{marker} {status}", s["pending" if pending else "enabled" if enabled else "muted"])
        if not values:
            self.put(7, "No resources found. / to search; I to install.", s["muted"])
        row = self.selected()
        self.put(height - 8, row["path"] if row else "", s["accent"])
        self.put(height - 7, row.get("description", "") if row else "", s["muted"])
        self.put(height - 5, self.message, s[self.message_tone])
        self.help_line(height - 3, "↑↓ row · ←→ section · Space toggle · Enter apply · G/Tab scope")
        self.help_line(height - 2, "/ search · Esc clear search · X discard changes · R refresh · Q quit")
        self.help_line(height - 1, "I install · u update · U update all · D remove · ? help", s["muted"])
        self.screen.refresh()

    def ask(self, title, *, confirm=False, tone=None):
        """Plain stdlib modal. Esc cancels, Enter submits; confirmation requires y."""
        value = ""
        while True:
            self.render()
            height, width = self.screen.getmaxyx()
            lines = textwrap.wrap(terminal_text(title), max(20, width - 8))
            first = max(1, height // 2 - min(len(lines), 5))
            panel = self.styles["error"] | self.curses.A_REVERSE if tone == "error" else self.styles["dialog" if confirm else "selected"]
            for offset, line in enumerate(lines[:6]):
                self.put(first + offset, line.ljust(max(1, width - 4)), panel)
            prompt = "Y to confirm / N or Esc to cancel" if confirm else value + "_   (Enter / Esc)"
            self.put(min(height - 2, first + min(len(lines), 6) + 1), prompt.ljust(max(1, width - 4)), self.styles["pending" if confirm else "accent"])
            self.screen.refresh()
            key = self.key()
            if key == "\x1b" or (confirm and key in ("n", "N", "\n", "\r")):
                return None
            if confirm and key in ("y", "Y"):
                return True
            if confirm:
                continue
            if key in ("\n", "\r", self.curses.KEY_ENTER):
                return value
            if key in ("\x7f", "\b", self.curses.KEY_BACKSPACE):
                value = value[:-1]
            elif isinstance(key, str) and key.isprintable() and len(value) < 2000:
                value += key

    def allow_discard(self, reason):
        if not self.draft.changes:
            return True
        if self.ask(f"{reason}. Discard {len(self.draft.changes)} unsaved changes?", confirm=True):
            self.draft.discard()
            return True
        return False

    def load(self, scope=None):
        scope = scope or self.scope
        data = self.manager.state(scope)
        self.scope = scope
        self.draft = Draft(data)
        self.index = 0

    def operation(self, action):
        self.notice("Working… Ctrl+C to interrupt and exit.")
        self.render()
        try:
            action()
            return True
        except (PiluliError, OSError, ValueError) as error:
            self.notice(str(error), "error")
            self.ask(f"Error: {error}", tone="error")
            return False

    def apply(self):
        if not self.draft.changes:
            self.notice("No unsaved changes.", "muted")
            return
        message = self.manager.apply(self.scope, self.draft.payload(), self.draft.state["revision"])
        self.draft.discard()
        self.load()
        self.notice(message, "enabled")

    def package_action(self, action):
        if self.draft.changes:
            self.notice("Apply (Enter) or discard (X) pending changes first.", "pending")
            return
        row = self.selected()
        source = None
        if action == "install":
            source = self.ask("Package source (npm:, git: or local path; packages may execute code):")
            if not source:
                return
        elif action != "update-all" and (self.view != "packages" or not row or not row["manageable"]):
            self.notice("Choose a package in Packages. Manage inherited packages in user scope.", "pending")
            return
        target = "USER: all projects" if self.scope == "user" else "CURRENT PROJECT"
        label = source or ("all packages in this scope" if action == "update-all" else row["name"])
        verb = {"install": "Install", "remove": "Remove", "update": "Reinstall", "update-all": "Reinstall"}[action]
        if not self.ask(f"{verb}: {label}. Scope: {target}. Runs immediately and may execute package code. Continue?", confirm=True):
            return
        def run():
            message = self.manager.package_action(self.scope, action, package_id=row["id"] if row else None, source=source, revision=self.draft.state["revision"])
            self.load()
            self.notice("Done. Run /reload in Pi. " + terminal_text(message)[-180:], "enabled")
        self.operation(run)

    def run(self):
        c = self.curses
        while True:
            self.render()
            key = self.key()
            if key in ("q", "Q"):
                if self.allow_discard("Quit"):
                    return
            elif key in (c.KEY_UP, "k"):
                self.index = max(0, self.index - 1)
            elif key in (c.KEY_DOWN, "j"):
                self.index = min(max(0, len(self.rows()) - 1), self.index + 1)
            elif key in (c.KEY_PPAGE, c.KEY_NPAGE):
                self.index += (1 if key == c.KEY_NPAGE else -1) * max(1, self.screen.getmaxyx()[0] - 14)
            elif key in (c.KEY_HOME, c.KEY_END):
                self.index = 0 if key == c.KEY_HOME else max(0, len(self.rows()) - 1)
            elif key in (c.KEY_LEFT, c.KEY_RIGHT):
                self.view = VIEWS[(VIEWS.index(self.view) + (1 if key == c.KEY_RIGHT else -1)) % len(VIEWS)]
                self.index = 0
            elif key == " ":
                row = self.selected()
                if row and self.view != "packages":
                    self.draft.toggle(row)
                    self.notice(f"Unsaved: {len(self.draft.changes)}. Enter to apply, X to discard.", "pending" if self.draft.changes else "muted")
            elif key in ("\n", "\r", c.KEY_ENTER):
                self.operation(self.apply)
            elif key in ("g", "G", "\t"):
                if self.allow_discard("Switch scope"):
                    if self.operation(lambda: self.load("user" if self.scope == "project" else "project")):
                        self.notice("Scope: " + self.scope)
            elif key in ("x", "X"):
                self.draft.discard()
                self.notice("Changes discarded. No files changed.", "muted")
            elif key in ("r", "R"):
                if self.allow_discard("Refresh list") and self.operation(self.load):
                    self.notice("List refreshed.", "enabled")
            elif key == "/":
                query = self.ask("Search by name, source, description or path:")
                if query is not None:
                    self.query, self.index = query, 0
            elif key == "\x1b":
                self.query, self.index = "", 0
            elif key in ("i", "I"):
                self.package_action("install")
            elif key == "u":
                self.package_action("update")
            elif key == "U":
                self.package_action("update-all")
            elif key in ("d", "D"):
                self.package_action("remove")
            elif key == "?":
                self.ask("↑↓ row; ←→ section. Space stages a toggle. Enter saves all changes atomically. G or Tab switches scope. / searches. X discards changes. I installs, U updates all, u/d updates/removes the selected package. Project overrides take precedence over user settings. Run /reload in Pi to load changes.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pilulit — Pi resource manager (stdlib curses TUI)")
    add_common_args(parser)
    args = parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Pilulit requires an interactive terminal. Use piluli.py for the web UI.", file=sys.stderr)
        return 1
    try:
        import curses
    except ImportError:
        print("This Python has no stdlib curses. Use Python with curses (macOS/Linux) or the web UI.", file=sys.stderr)
        return 1
    locale.setlocale(locale.LC_ALL, "")
    manager = PiManager(args.agent_dir, args.project_dir, args.pi_command)
    try:
        curses.wrapper(lambda screen: TerminalApp(screen, curses, manager, args.scope).run())
    except KeyboardInterrupt:
        return 130
    except (PiluliError, OSError, curses.error) as error:
        print(f"Pilulit: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
