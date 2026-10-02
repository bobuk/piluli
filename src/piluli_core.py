"""Shared stdlib backend for Piluli and Pilulit. Never imports extension code."""
from __future__ import annotations

import argparse
import copy
import fnmatch
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

RESOURCE_TYPES = ("extensions", "skills", "prompts", "themes")
VIEWS = (*RESOURCE_TYPES, "packages")
BUILTINS = ("mcp", "llama.cpp", "codemode", "tool-search")


class PiluliError(Exception):
    """An actionable, user-visible error."""


def absolute(value, base: Path) -> Path:
    return Path(os.path.abspath(base / Path(value).expanduser()))


def read_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise PiluliError(f"Cannot read {path}: {error}") from error
    if not isinstance(data, dict):
        raise PiluliError(f"Expected a JSON object: {path}")
    return data


def string_list(value, label="filter") -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PiluliError(f"{label}: expected an array of strings")
    return value


def source_of(entry) -> str:
    source = entry if isinstance(entry, str) else entry.get("source") if isinstance(entry, dict) else None
    if not isinstance(source, str) or not source:
        raise PiluliError("packages: each package must have a source")
    return source


def package_entries(settings: dict) -> list:
    entries = settings.get("packages", [])
    if not isinstance(entries, list):
        raise PiluliError("packages: expected an array")
    for entry in entries:
        source_of(entry)
    return entries


def remote_source(source: str) -> bool:
    return source.startswith(("npm:", "git:", "https://", "http://", "ssh://"))


def source_parts(source: str, base: Path) -> tuple[str, str]:
    if source.startswith("npm:"):
        name = source[4:]
        name = name[:name.rfind("@")] if "@" in name[1:] else name
        if not re.fullmatch(r"(?:@[\w.-]+/)?[\w.-]+", name):
            raise PiluliError(f"Unsupported npm source: {source}")
        return "npm", name
    if remote_source(source):
        value = source.removeprefix("git:")
        if "://" not in value:
            value = "ssh://" + value.replace(":", "/", 1) if value.startswith("git@") else "https://" + value
        parsed = urlsplit(value)
        path = parsed.path.lstrip("/").rsplit("@", 1)[0].removesuffix(".git")
        if not parsed.hostname or not path or ".." in Path(path).parts:
            raise PiluliError(f"Unsupported git source: {source}")
        return "git", parsed.hostname + "/" + path
    return "local", str(absolute(source, base).resolve())


def identity(source: str, base: Path) -> str:
    kind, name = source_parts(source, base)
    return kind + ":" + name


def uid(*values) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()[:24]


def expand_braces(pattern: str) -> list[str]:
    match = re.search(r"\{([^{}]+)\}", pattern)
    if not match:
        return [pattern]
    return [result for part in match[1].split(",") for result in expand_braces(pattern[:match.start()] + part + pattern[match.end():])]


def glob_match(value: str, pattern: str) -> bool:
    """Segment-aware *, ?, [], ** and brace alternatives; no shell evaluation."""
    def match(parts, pats):
        if not pats:
            return not parts
        if pats[0] == "**":
            return match(parts, pats[1:]) or bool(parts and match(parts[1:], pats))
        return bool(parts and fnmatch.fnmatchcase(parts[0], pats[0]) and match(parts[1:], pats[1:]))
    return any(match(value.split("/"), p.removeprefix("./").split("/")) for p in expand_braces(pattern))


def pattern_matches(path: str, target: str, base: Path, exact=False) -> bool:
    if path.startswith("builtin:"):
        return path == target if exact else glob_match(path, target)
    p = Path(path)
    candidates = [str(p), os.path.relpath(p, base).replace(os.sep, "/")]
    if not exact:
        candidates.append(p.name)
    if p.name == "SKILL.md":
        candidates += [str(p.parent), os.path.relpath(p.parent, base).replace(os.sep, "/")]
        if not exact:
            candidates.append(p.parent.name)
    target = os.path.expanduser(target).removeprefix("./").rstrip("/")
    return any(candidate == target if exact else glob_match(candidate, target) for candidate in candidates)


def filtered(path: str, patterns, base: Path, *, inherited=True, delta=False, top=False) -> bool:
    if patterns is None:
        return inherited
    patterns = string_list(patterns)
    if delta:
        result = inherited
        for item in patterns:
            prefix = item[:1] if item.startswith(("+", "-", "!")) else ""
            if pattern_matches(path, item[1:] if prefix else item, base, prefix in ("+", "-")):
                result = prefix not in ("-", "!")
        return result
    if not patterns and not top:
        return False
    includes = [p for p in patterns if not p.startswith(("+", "-", "!"))] if not top else []
    result = not includes or any(pattern_matches(path, p, base) for p in includes)
    # Pi regular filters have fixed precedence: include -> glob exclusion -> + -> -.
    for prefix in ("!", "+", "-"):
        if any(pattern_matches(path, p[1:], base, prefix != "!") for p in patterns if p.startswith(prefix)):
            result = prefix == "+"
    return result


def metadata(path: Path) -> dict:
    """Read display fields only; supports plain, quoted and block YAML scalars."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return {}
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return {}
    result = {}
    for index, line in enumerate(lines[1:], 1):
        if line == "---":
            break
        match = re.match(r"^(name|description):\s*(.*)$", line)
        if not match:
            continue
        key, value = match.groups()
        if value in ("|", ">", "|-", ">-"):
            block = []
            for following in lines[index + 1:]:
                if following and not following[0].isspace():
                    break
                block.append(following.strip())
            value = " ".join(block)
        result[key] = value.strip("\"'")
    return result


def extension_entries(root: Path):
    """Pi's smart extension directories prefer a manifest, then an index file."""
    manifest = read_object(root / "package.json")
    pi = manifest.get("pi")
    if isinstance(pi, dict) and pi.get("extensions"):
        entries = [absolute(value, root) for value in string_list(pi["extensions"], "pi.extensions")]
        entries = [path for path in entries if path.exists()]
        if entries:
            return entries
    for name in ("index.ts", "index.js"):
        if (root / name).is_file():
            return [root / name]
    return None


def discover(root: Path, kind: str, *, agents=False, seen=None) -> list[Path]:
    if root.is_file():
        return [root]
    if not root.is_dir():
        return []
    seen = set() if seen is None else seen
    canonical = root.resolve()
    if canonical in seen:
        return []
    seen.add(canonical)
    try:
        children = sorted(root.iterdir())
    except OSError:
        return []
    if kind == "skills" and (root / "SKILL.md").is_file():
        return [root / "SKILL.md"]
    if kind == "extensions":
        entries = extension_entries(root)
        if entries:
            return entries
    result = []
    for child in children:
        if child.name.startswith(".") or child.name == "node_modules":
            continue
        if child.is_file():
            allowed = child.suffix in (".ts", ".js") if kind == "extensions" else child.suffix == (".json" if kind == "themes" else ".md")
            if allowed and (kind != "skills" or not agents):
                result.append(child)
        elif child.is_dir():
            if kind == "extensions":
                result.extend(extension_entries(child) or [])
            else:
                # SKILL.md directories take priority and do not recurse into references/.
                result.extend(discover(child, kind, agents=True if kind == "skills" else agents, seen=seen))
    return result


def manifest_files(root: Path, kind: str) -> list[Path]:
    if root.is_file():
        return [root] if kind == "extensions" else []
    manifest = read_object(root / "package.json")
    pi = manifest.get("pi")
    if isinstance(pi, dict):
        entries = string_list(pi.get(kind, []), f"pi.{kind}")
    else:
        return discover(root / kind, kind)
    paths = []
    for entry in entries:
        if entry.startswith(("!", "+", "-")):
            continue
        for pattern in expand_braces(entry):
            for expanded in glob.glob(str(root / pattern), recursive=True):
                paths.extend(discover(absolute(expanded, root), kind))
    rules = [p for p in entries if p.startswith(("!", "+", "-"))]
    return list(dict.fromkeys(p for p in paths if filtered(str(p), rules, root, top=True)))


class PiManager:
    def __init__(self, agent_dir: Path, project_dir: Path, pi_command="pi", home: Path | None = None):
        self.agent_dir = agent_dir.expanduser().resolve()
        self.project_dir = project_dir.expanduser().resolve()
        self.home = (home or Path.home()).resolve()
        self.bases = {"user": self.agent_dir, "project": self.project_dir / ".pi"}
        self.paths = {scope: base / "settings.json" for scope, base in self.bases.items()}
        self.pi_command = pi_command
        self._lock = threading.RLock()

    def scope(self, scope):
        if scope not in ("user", "project"):
            raise PiluliError("Scope must be user or project")
        if self.paths["user"].resolve() == self.paths["project"].resolve():
            raise PiluliError("User and project settings point to the same file")
        return scope

    def _settings(self):
        data = {scope: read_object(path) for scope, path in self.paths.items()}
        for settings in data.values():
            package_entries(settings)
            for kind in RESOURCE_TYPES:
                string_list(settings.get(kind, []), kind)
        return data

    def revision(self):
        digest = hashlib.sha256()
        for path in self.paths.values():
            digest.update(str(path).encode())
            digest.update(path.read_bytes() if path.exists() else b"<missing>")
        return digest.hexdigest()

    @contextmanager
    def _writing(self, scope):
        """Serialize this process and cooperating web/TUI processes."""
        with self._lock:
            self.scope(scope)
            base = self.bases[scope]
            base.mkdir(parents=True, exist_ok=True)
            with (base / ".piluli.lock").open("a") as lock:
                try:
                    import fcntl
                except ImportError:
                    fcntl = None
                if fcntl:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl:
                        fcntl.flock(lock, fcntl.LOCK_UN)

    def _save(self, scope, settings, revision):
        if revision != self.revision():
            raise PiluliError("Settings changed in another process. Refresh the list and repeat your changes.")
        path = self.paths[scope]
        if path.is_symlink():
            raise PiluliError(f"Cannot write through a settings.json symlink: {path}")
        temp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
                temp = Path(handle.name)
                handle.write(json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            temp.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
            if revision != self.revision():
                raise PiluliError("Settings changed in another process. Refresh the list.")
            os.replace(temp, path)
        finally:
            if temp:
                temp.unlink(missing_ok=True)

    def _package_groups(self, scope, settings):
        groups = {}
        for origin in (("user",) if scope == "user" else ("user", "project")):
            for entry in package_entries(settings[origin]):
                source = source_of(entry)
                key = identity(source, self.bases[origin])
                groups.setdefault(key, {})[origin] = copy.deepcopy(entry)
        return groups

    def _package_root(self, source, origin):
        kind, value = source_parts(source, self.bases[origin])
        if kind == "local":
            return Path(value)
        return self.bases[origin] / ("npm/node_modules" if kind == "npm" else "git") / value

    def _snapshot(self, scope, settings):
        rows = {kind: [] for kind in RESOURCE_TYPES}
        packages = []
        groups = self._package_groups(scope, settings)
        for key, entries in groups.items():
            local = entries.get(scope)
            personal = entries.get("user")
            delta = isinstance(local, dict) and local.get("autoload") is False
            inherited = scope == "project" and personal is not None and (local is None or delta)
            origin = "user" if inherited else scope
            entry = personal if inherited else local
            source = source_of(entry)
            root = self._package_root(source, origin)
            info = read_object(root / "package.json") if root.is_dir() else {}
            package_id = uid("package", key)
            package = {
                "id": package_id, "name": info.get("name") or source, "source": source,
                "description": info.get("description", ""), "version": info.get("version", ""),
                "path": str(root), "origin": origin, "inherited": inherited,
                "installed": root.exists(), "manageable": not inherited and not delta,
                "resources": [], "_key": key,
            }
            packages.append(package)
            if not root.exists():
                continue
            for kind in RESOURCE_TYPES:
                files = manifest_files(root, kind)
                if files:
                    package["resources"].append(kind)
                base = root.parent if root.is_file() else root
                normal = entry if isinstance(entry, dict) else {}
                for file in files:
                    enabled = filtered(str(file), normal.get(kind), base, delta=normal.get("autoload") is False, inherited=normal.get("autoload") is not False)
                    inherited_enabled = enabled
                    if inherited and local is not None:
                        enabled = filtered(str(file), local.get(kind), base, inherited=enabled, delta=True)
                    display = metadata(file) if kind in ("skills", "prompts") else {}
                    name = display.get("name") or (file.parent.name if file.name in ("SKILL.md", "index.ts", "index.js") else file.stem)
                    rows[kind].append({
                        "id": uid(kind, key, os.path.relpath(file, base)), "kind": kind, "name": name,
                        "description": display.get("description", "") or package["description"],
                        "source": source, "path": str(file), "origin": origin, "inherited": inherited,
                        "enabled": enabled, "inheritedEnabled": inherited_enabled, "packageId": package_id,
                        "_key": key, "_base": str(base), "_mode": "package",
                        "_override": local if inherited else None,
                    })

        # Standalone resources: explicit paths before auto-discovery; project before user.
        standalone = {kind: {} for kind in RESOURCE_TYPES}
        for origin in (("user",) if scope == "user" else ("user", "project")):
            base = self.bases[origin]
            for kind in RESOURCE_TYPES:
                rules = settings[origin].get(kind, [])
                candidates = []
                for value in rules:
                    if not value.startswith(("!", "+", "-")):
                        candidates.extend((p, base) for p in discover(absolute(value, base), kind))
                candidates.extend((p, base) for p in discover(base / kind, kind))
                if kind == "skills":
                    dirs = [self.home / ".agents"] if origin == "user" else self._ancestor_agents()
                    for agents_dir in dirs:
                        candidates.extend((p, agents_dir) for p in discover(agents_dir / "skills", kind, agents=True))
                for file, filter_base in candidates:
                    canonical = str(file.resolve())
                    old = standalone[kind].get(canonical)
                    if old and old["origin"] == origin:
                        continue
                    display = metadata(file) if kind in ("skills", "prompts") else {}
                    inherited = scope == "project" and (origin == "user" or (old and old["origin"] == "user"))
                    standalone[kind][canonical] = {
                        "id": uid(kind, canonical), "kind": kind,
                        "name": display.get("name") or (file.parent.name if file.name in ("SKILL.md", "index.ts", "index.js") else file.stem),
                        "description": display.get("description", ""), "source": "standalone",
                        "path": str(file), "origin": "user" if inherited else origin, "inherited": bool(inherited),
                        "enabled": filtered(str(file), rules, filter_base, top=True),
                        "inheritedEnabled": old["enabled"] if old else True,
                        "packageId": None, "_mode": "standalone", "_base": str(filter_base),
                    }
        for kind in RESOURCE_TYPES:
            rows[kind].extend(standalone[kind].values())
        for name in BUILTINS:
            path = "builtin:" + name
            user_enabled = filtered(path, settings["user"].get("extensions", []), self.agent_dir, top=True)
            enabled = user_enabled if scope == "user" else filtered(path, settings["project"].get("extensions", []), self.bases["project"], inherited=user_enabled, delta=True)
            rows["extensions"].append({
                "id": uid(path), "kind": "extensions", "name": name, "description": "Built-in Pi extension",
                "path": path, "source": path, "origin": "builtin", "inherited": scope == "project",
                "enabled": enabled, "inheritedEnabled": user_enabled, "packageId": None,
                "_mode": "builtin", "_base": str(self.bases[scope]),
            })
        # Match Pi's canonical resource precedence (project before user, packages before top-level).
        for kind in RESOURCE_TYPES:
            unique = {}
            for row in sorted(rows[kind], key=lambda r: (r["origin"] == "user", r["_mode"] != "package")):
                canonical = row["path"] if row["_mode"] == "builtin" else str(Path(row["path"]).resolve())
                unique.setdefault(canonical, row)
            rows[kind] = sorted(unique.values(), key=lambda r: (r["name"].casefold(), r["path"]))
        # Skills CLI installs resolve into <agents>/skills/<name>, including through agent symlinks.
        locks = self._skills_cli_locks()
        for row in rows["skills"]:
            entry = locks.get(str(Path(row["path"]).resolve().parent))
            row["skillsCli"] = entry.get("source", "") if entry else ""
        return rows, sorted(packages, key=lambda p: p["name"].casefold())

    def _ancestor_agents(self):
        result, current = [], self.project_dir
        while True:
            directory = current / ".agents"
            if directory != self.home / ".agents":
                result.append(directory)
            if (current / ".git").exists() or current == current.parent:
                break
            current = current.parent
        return result

    def _skills_cli_locks(self):
        """Skills installed by the skills CLI (npx skills), by resolved skill directory."""
        locks = {}
        for agents_dir in [self.home / ".agents", *self._ancestor_agents()]:
            try:
                data = read_object(agents_dir / ".skill-lock.json")
            except PiluliError:
                continue
            skills = data.get("skills")
            if not isinstance(skills, dict):
                continue
            for name, entry in skills.items():
                if isinstance(entry, dict) and name not in (".", "..") and re.fullmatch(r"[\w.+-]+", name or ""):
                    locks[str((agents_dir / "skills" / name).resolve())] = entry
        return locks

    def state(self, scope="project"):
        with self._lock:
            scope = self.scope(scope)
            revision = self.revision()
            settings = self._settings()
            rows, packages = self._snapshot(scope, settings)
            if revision != self.revision():
                raise PiluliError("Settings changed while reading. Refresh again.")
            def public(row):
                return {key: value for key, value in row.items() if not key.startswith("_")}
            return {
                "scope": scope, "revision": revision, "projectDir": str(self.project_dir),
                "settingsPath": str(self.paths[scope]),
                "resources": {kind: [public(row) for row in items] for kind, items in rows.items()},
                "packages": [public(p) for p in packages],
            }

    def _set_resource(self, scope, settings, row, enabled):
        destination = settings[scope]
        kind, path = row["kind"], row["path"]
        if row["_mode"] == "package":
            entries = destination.setdefault("packages", [])
            index = next((i for i, e in enumerate(entries) if identity(source_of(e), self.bases[scope]) == row["_key"]), None)
            if index is None:
                source = row["source"]
                if not remote_source(source):
                    source = str(absolute(source, self.agent_dir))
                entries.append({"source": source, "autoload": False})
                index = len(entries) - 1
            original = entries[index]
            package = {"source": original} if isinstance(original, str) else original
            entries[index] = package
            current = package.get(kind)
            base = Path(row["_base"])
            target = os.path.relpath(path, base).replace(os.sep, "/")
            if target == ".":
                # Node's path.relative(root, root) is "", Python's is ".".
                # An absolute exact rule is unambiguous for directory extensions.
                target = path
            # [] means no resources for a regular package. Retain that baseline when
            # enabling a single entry; +path is evaluated after !** by Pi.
            rules = ["!**"] if current == [] and package.get("autoload") is not False else list(current or [])
            rules = [p for p in rules if not (p.startswith(("+", "-")) and pattern_matches(path, p[1:], base, True))]
            rules.append(("+" if enabled else "-") + target)
            package[kind] = rules
        else:
            rules = destination.setdefault(kind, [])
            base = Path(row["_base"])
            rules[:] = [p for p in rules if not (p.startswith(("+", "-")) and pattern_matches(path, p[1:], base, True))]
            if row["_mode"] != "builtin" and path not in rules:
                # Plain paths are essential to override inherited top-level resources.
                rules.append(path)
            rules.append(("+" if enabled else "-") + path)

    def apply(self, scope, changes, revision):
        scope = self.scope(scope)
        if not isinstance(changes, list) or len(changes) > 2000:
            raise PiluliError("changes must be an array of at most 2000 changes")
        with self._writing(scope):
            if not isinstance(revision, str) or revision != self.revision():
                raise PiluliError("Settings changed in another process. Refresh the list; pending changes were not applied.")
            settings = self._settings()
            resources, _ = self._snapshot(scope, settings)
            by_id = {r["id"]: r for rows in resources.values() for r in rows}
            seen = set()
            for change in changes:
                if not isinstance(change, dict) or not isinstance(change.get("id"), str) or type(change.get("enabled")) is not bool:
                    raise PiluliError("Invalid change: id and enabled (boolean) are required")
                row = by_id.get(change["id"])
                if row is None or row["id"] in seen:
                    raise PiluliError("Resource not found or duplicated in request")
                seen.add(row["id"])
                self._set_resource(scope, settings, row, change["enabled"])
            # All validation precedes the single atomic write.
            if changes:
                self._save(scope, settings[scope], revision)
        return f"Saved {len(changes)} changes. Run /reload in Pi."

    @staticmethod
    def validate_source(source):
        if not isinstance(source, str) or not source.strip() or len(source) > 2000:
            raise PiluliError("Enter a package source")
        source = source.strip()
        if source.startswith("-") or any(ord(c) < 32 for c in source):
            raise PiluliError("Invalid package source")
        return source

    def _run(self, args, executable=None):
        executable = executable or self.pi_command
        command = shutil.which(executable)
        if not command:
            label = "Pi" if executable == self.pi_command else executable
            raise PiluliError(f"{label} not found: {executable}")
        try:
            result = subprocess.run(
                [command, *args], cwd=self.project_dir, stdin=subprocess.DEVNULL,
                env={**os.environ, "PI_CODING_AGENT_DIR": str(self.agent_dir)},
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                timeout=300, check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise PiluliError("Pi operation exceeded 5 minutes. Check the package state before retrying.") from error
        except OSError as error:
            raise PiluliError(str(error)) from error
        if result.returncode:
            raise PiluliError(result.stdout[-10000:] or f"Pi: exit {result.returncode}")
        return result.stdout.strip()[-10000:]

    def package_action(self, scope, action, *, package_id=None, source=None, revision=None):
        scope = self.scope(scope)
        if action not in ("install", "update", "remove", "update-all"):
            raise PiluliError("Unknown action")
        with self._writing(scope):
            if revision != self.revision():
                raise PiluliError("Settings changed. Refresh the list first.")
            settings = self._settings()
            _, packages = self._snapshot(scope, settings)
            flags = ["--local", "--approve"] if scope == "project" else ["--no-approve"]
            if action == "install":
                source = self.validate_source(source)
                if not remote_source(source):
                    source = str(absolute(source, self.project_dir))
                return self._run(["install", source, *flags]) or "Package installed"
            targets = [p for p in packages if p["manageable"] and (action == "update-all" or p["id"] == package_id)]
            if not targets:
                raise PiluliError("No matching packages in this scope. Manage inherited packages in user scope.")
            output = []
            for package in targets:
                selected = package["source"]
                if not remote_source(selected):
                    selected = str(absolute(selected, self.bases[scope]))
                # pi update has no scope option and can update both installations.
                # Reinstall in the selected scope instead; Pi retains existing filters.
                command = "remove" if action == "remove" else "install"
                try:
                    output.append(self._run([command, selected, *flags]))
                except PiluliError as error:
                    raise PiluliError(f"{package['name']}: {error}\nCompleted before failure: {len(output)}. Refresh the list.") from error
            return "\n".join(output) or "Done"

    def skill_action(self, scope, action, *, resource_id=None, revision=None):
        scope = self.scope(scope)
        if action != "remove":
            raise PiluliError("Unknown action")
        with self._writing(scope):
            if revision != self.revision():
                raise PiluliError("Settings changed. Refresh the list first.")
            resources, _ = self._snapshot(scope, self._settings())
            row = next((r for r in resources["skills"] if r["id"] == resource_id), None)
            if row is None or not row["skillsCli"]:
                raise PiluliError("Only skills installed by the skills CLI can be removed this way")
            skill_dir = Path(row["path"]).resolve().parent
            # Removing through the CLI cleans its lock file and agent links, so
            # `skills update -g` cannot resurrect the skill afterwards.
            args = ["-y", "skills", "remove", skill_dir.name, "-y"]
            if skill_dir.parent.parent == (self.home / ".agents").resolve():
                args.append("--global")
            return self._run(args, executable="npx") or "Skill removed"


class Draft:
    """UI-independent, in-memory staged changes. Never writes on toggle."""
    def __init__(self, state):
        self.state = state
        self.changes = {}

    def enabled(self, row):
        return self.changes.get(row["id"], row["enabled"])

    def toggle(self, row):
        value = not self.enabled(row)
        if value == row["enabled"]:
            self.changes.pop(row["id"], None)
        else:
            self.changes[row["id"]] = value

    def payload(self):
        return [{"id": key, "enabled": value} for key, value in self.changes.items()]

    def discard(self):
        self.changes.clear()


def add_common_args(parser: argparse.ArgumentParser):
    parser.add_argument("--project-dir", type=Path, default=Path.cwd(), help="Project root (default: cwd)")
    parser.add_argument("--agent-dir", type=Path, default=Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent")))
    parser.add_argument("--pi-command", default="pi")
    parser.add_argument("--scope", choices=("project", "user"), default="project")
