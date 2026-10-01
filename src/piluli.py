#!/usr/bin/env python3
"""Piluli web entry point. Build with build.py for the single-file version."""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from piluli_core import PiManager, PiluliError, add_common_args  # build:core

EMBEDDED_INDEX_HTML = None  # build:embed-index
EMBEDDED_STYLES_CSS = None  # build:embed-styles
EMBEDDED_APP_JS = None  # build:embed-app


def load_asset(name):
    value = {"index.html": EMBEDDED_INDEX_HTML, "styles.css": EMBEDDED_STYLES_CSS, "app.js": EMBEDDED_APP_JS}[name]
    return value if value is not None else (Path(__file__).resolve().parent.parent / "web" / name).read_text(encoding="utf-8")


class PiluliServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, manager, token, scope="project"):
        super().__init__(address, PiluliRequestHandler)
        self.manager, self.token, self.default_scope = manager, token, scope


class PiluliRequestHandler(BaseHTTPRequestHandler):
    # Close each connection so rejected POST bodies cannot become another request.
    protocol_version = "HTTP/1.0"

    def log_message(self, format, *args):
        sys.stderr.write(f"[piluli] {format % args}\n")

    def send_bytes(self, status, content, kind):
        payload = content.encode("utf-8")
        self.send_response(status)
        for name, value in {
            "Content-Type": kind + "; charset=utf-8", "Content-Length": str(len(payload)),
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        }.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, status, **data):
        self.send_bytes(status, json.dumps(data, ensure_ascii=False), "application/json")

    def local_request(self):
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.headers.get("Host", "").lower() not in allowed:
            return False
        origin = self.headers.get("Origin")
        return not origin or origin in {"http://" + host for host in allowed}

    def authorized(self):
        supplied = self.headers.get("X-Piluli-Token", "")
        return supplied.isascii() and secrets.compare_digest(supplied, self.server.token)

    def do_GET(self):
        if not self.local_request():
            self.send_json(403, ok=False, error="Invalid Host or Origin")
            return
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/api/state":
                if not self.authorized():
                    self.send_json(403, ok=False, error="Invalid token")
                    return
                scope = parse_qs(parsed.query).get("scope", [self.server.default_scope])[0]
                self.send_json(200, ok=True, data=self.server.manager.state(scope))
                return
            assets = {"/": ("index.html", "text/html"), "/styles.css": ("styles.css", "text/css"), "/app.js": ("app.js", "text/javascript")}
            if parsed.path not in assets:
                self.send_json(404, ok=False, error="Not found")
                return
            name, mime = assets[parsed.path]
            text = load_asset(name)
            if name == "index.html":
                text = text.replace("__PILULI_TOKEN__", self.server.token).replace("__PILULI_SCOPE__", self.server.default_scope)
            self.send_bytes(200, text, mime)
        except (PiluliError, OSError, ValueError) as error:
            self.send_json(400, ok=False, error=str(error))

    def do_POST(self):
        if not self.local_request() or not self.authorized():
            self.send_json(403, ok=False, error="Request rejected")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 512 * 1024:
                raise PiluliError("Invalid request size")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise PiluliError("Expected a JSON object")
            scope = body.get("scope")
            if self.path == "/api/apply":
                message = self.server.manager.apply(scope, body.get("changes"), body.get("revision"))
            elif self.path == "/api/packages/action":
                message = self.server.manager.package_action(scope, body.get("action"), package_id=body.get("id"), source=body.get("source"), revision=body.get("revision"))
            else:
                self.send_json(404, ok=False, error="Not found")
                return
            self.send_json(200, ok=True, message=message)
        except (PiluliError, OSError, ValueError) as error:
            self.send_json(400, ok=False, error=str(error))
        except Exception as error:
            self.log_error("%r", error)
            self.send_json(500, ok=False, error="Internal error; check the server log")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Piluli — Pi resource manager (web)")
    add_common_args(parser)
    parser.add_argument("--host", choices=("127.0.0.1", "localhost"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    manager = PiManager(args.agent_dir, args.project_dir, args.pi_command)
    try:
        server = PiluliServer((args.host, args.port), manager, secrets.token_urlsafe(32), args.scope)
    except OSError as error:
        print(f"Piluli: {error}", file=sys.stderr)
        return 1
    url = f"http://{args.host}:{server.server_port}/"
    print(f"Piluli: {url}\nProject: {manager.project_dir}\nCtrl+C to stop", flush=True)
    if not args.no_browser:
        timer = threading.Timer(.3, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
