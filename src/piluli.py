#!/usr/bin/env python3
"""Piluli web entry point. Build with build.py for the single-file version."""
from __future__ import annotations

import argparse
import errno
import json
import os
import secrets
import socket
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

DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 5432
WILDCARD_HOSTS = {"0.0.0.0"}


def is_wildcard_host(host):
    if host in WILDCARD_HOSTS:
        return True
    try:
        return socket.inet_aton(host) == b"\0\0\0\0"
    except OSError:
        return False


def parse_port(value):
    text = str(value).strip()
    if not text.isascii() or not text.isdigit():
        raise ValueError("port must be a number between 1 and 65535")
    port = int(text)
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return port


def argparse_port(value):
    try:
        return parse_port(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def normalize_host(value):
    host = str(value).strip()
    if not host or any(char.isspace() for char in host) or "://" in host or "/" in host:
        raise ValueError("host must be a hostname or IPv4 address without a URL scheme")
    return host.lower()


def parse_environment_endpoint(value):
    host, separator, port = value.strip().rpartition(":")
    if not separator or not host or not port:
        raise ValueError("PILULI_WEB must use the format host:port")
    host = normalize_host(host)
    if is_wildcard_host(host):
        raise ValueError("PILULI_WEB cannot use a wildcard host; pass it explicitly with `piluli on 0.0.0.0 PORT`")
    return host, parse_port(port)


def resolve_web_endpoint(args, environ=None):
    environ = os.environ if environ is None else environ
    host, port = DEFAULT_WEB_HOST, DEFAULT_WEB_PORT
    if value := environ.get("PILULI_WEB", "").strip():
        host, port = parse_environment_endpoint(value)

    values = args.web_endpoint
    positional_host = positional_port = None
    if values:
        if values[0] != "on" or len(values) > 3:
            raise ValueError("web address syntax is `piluli on [host] [port]`")
        endpoint = values[1:]
        if len(endpoint) == 1:
            if endpoint[0].isascii() and endpoint[0].isdigit():
                positional_port = parse_port(endpoint[0])
            else:
                positional_host = normalize_host(endpoint[0])
        elif len(endpoint) == 2:
            positional_host = normalize_host(endpoint[0])
            positional_port = parse_port(endpoint[1])

    if args.host is not None and positional_host is not None:
        raise ValueError("host was supplied both after `on` and with `--host`")
    if args.port is not None and positional_port is not None:
        raise ValueError("port was supplied both after `on` and with `--port`")
    host = positional_host or (normalize_host(args.host) if args.host is not None else host)
    port = positional_port or args.port or port
    return host, port


def confirm_wildcard(host, *, stdin=None, input_fn=input):
    if not is_wildcard_host(host):
        return True
    stdin = sys.stdin if stdin is None else stdin
    if not stdin.isatty():
        print("Piluli: wildcard host requires interactive confirmation.", file=sys.stderr)
        return False
    print(
        "WARNING: 0.0.0.0 exposes Piluli to the network. Anyone who can reach it may manage your Pi resources.",
        file=sys.stderr,
    )
    try:
        return input_fn("Type 'yes' to continue: ").strip().lower() == "yes"
    except (EOFError, KeyboardInterrupt):
        return False


def load_asset(name):
    value = {"index.html": EMBEDDED_INDEX_HTML, "styles.css": EMBEDDED_STYLES_CSS, "app.js": EMBEDDED_APP_JS}[name]
    return value if value is not None else (Path(__file__).resolve().parent.parent / "web" / name).read_text(encoding="utf-8")


class PiluliServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, manager, token, scope="project"):
        self.web_host = normalize_host(address[0])
        self.wildcard_host = is_wildcard_host(self.web_host)
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
        authority = self.headers.get("Host", "").strip().lower()
        expected_port = str(self.server.server_port)
        host, separator, port = authority.rpartition(":")
        if not separator or port != expected_port or not host:
            return False
        if self.server.wildcard_host:
            allowed = authority
        else:
            names = {self.server.web_host, str(self.server.server_address[0]).lower()}
            if self.server.web_host in {"127.0.0.1", "localhost"}:
                names.update(("127.0.0.1", "localhost"))
            if host not in names:
                return False
            allowed = authority
        origin = self.headers.get("Origin")
        return not origin or origin.lower() == "http://" + allowed

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


def main(argv=None, *, environ=None, stdin=None, input_fn=input):
    parser = argparse.ArgumentParser(description="Piluli — Pi resource manager (web)")
    add_common_args(parser)
    parser.add_argument("web_endpoint", nargs="*", metavar="WEB", help="on [host] [port]")
    parser.add_argument("--host", help="Bind host (explicit CLI wildcard requires confirmation)")
    parser.add_argument("--port", type=argparse_port, help="Bind port, 1-65535")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    try:
        host, port = resolve_web_endpoint(args, environ)
    except ValueError as error:
        parser.error(str(error))
    if not confirm_wildcard(host, stdin=stdin, input_fn=input_fn):
        print("Piluli: startup cancelled.", file=sys.stderr)
        return 1

    manager = PiManager(args.agent_dir, args.project_dir, args.pi_command)
    try:
        server = PiluliServer((host, port), manager, secrets.token_urlsafe(32), args.scope)
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            print(f"Piluli: {host}:{port} is already in use.", file=sys.stderr)
        else:
            print(f"Piluli: cannot listen on {host}:{port}: {error}", file=sys.stderr)
        return 1
    browser_host = "127.0.0.1" if is_wildcard_host(host) else host
    url = f"http://{browser_host}:{server.server_port}/"
    listening = f"\nListening on: {host}:{server.server_port}" if is_wildcard_host(host) else ""
    print(f"Piluli: {url}{listening}\nProject: {manager.project_dir}\nCtrl+C to stop", flush=True)
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
