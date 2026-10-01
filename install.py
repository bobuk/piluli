#!/usr/bin/env python3
"""Install one of Piluli's standalone scripts from GitHub."""
from __future__ import annotations

import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPOSITORY = "bobuk/piluli"
BRANCH = "main"
TOOLS = {
    "piluli": "Browser UI",
    "pilulit": "Terminal UI",
}
INSTALL_CANDIDATES = (
    Path("~/.local/bin").expanduser(),
    Path("~/bin").expanduser(),
    Path("~/.bin").expanduser(),
)
DEFAULT_INSTALL_DIR = INSTALL_CANDIDATES[0]


class InstallError(Exception):
    """A friendly installation failure."""


def download(tool: str) -> str:
    if tool not in TOOLS:
        raise InstallError(f"Unknown script: {tool}")
    url = f"https://raw.githubusercontent.com/{REPOSITORY}/{BRANCH}/dist/{tool}.py"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read().decode("utf-8")
    except (urllib.error.URLError, UnicodeError) as error:
        raise InstallError(f"Could not download {tool} from GitHub: {error}") from error


def choose_install_dir(path: str | None = None) -> tuple[Path, bool]:
    """Prefer a conventional local bin directory that is already on PATH."""
    path_dirs = {Path(item).expanduser() for item in (path or os.environ.get("PATH", "")).split(os.pathsep) if item}
    for candidate in INSTALL_CANDIDATES:
        if candidate in path_dirs:
            return candidate, True
    return DEFAULT_INSTALL_DIR, False


def install(tool: str, *, source: str | None = None, install_dir: Path | None = None) -> Path:
    if tool not in TOOLS:
        raise InstallError(f"Unknown script: {tool}")
    source = download(tool) if source is None else source
    try:
        compile(source, f"{tool}.py", "exec")
    except (SyntaxError, ValueError) as error:
        raise InstallError(f"Downloaded {tool} is not valid Python: {error}") from error

    selected_dir, on_path = choose_install_dir()
    destination_dir = Path(install_dir).expanduser() if install_dir is not None else selected_dir
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / tool
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{tool}.", dir=destination_dir)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(source)
        os.chmod(temporary, 0o755)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    print(f"Installed {tool} ({TOOLS[tool]}) to {destination}")
    if install_dir is None and not on_path:
        print(f"Note: {destination_dir} is not in your PATH.")
        print(f'  Add it with: export PATH="{destination_dir}:$PATH"')
    return destination


def usage() -> str:
    choices = "\n".join(f"  {name:<8} {description}" for name, description in TOOLS.items())
    return f"""Usage: python3 - <script>

Choose exactly one script:
{choices}

Examples:
  curl .../install.py | python3 - piluli
  curl .../install.py | python3 - pilulit
"""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args in (["-h"], ["--help"]):
        print(usage())
        return 0
    if len(args) != 1 or args[0] not in TOOLS:
        print(usage(), file=sys.stderr)
        return 2
    try:
        install(args[0])
    except (InstallError, OSError) as error:
        print(f"Install failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
