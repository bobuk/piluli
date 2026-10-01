"""Optional integration against the locally installed Pi resolver (no models/extensions run)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

import test_core


def installed_dist():
    executable = shutil.which("pi")
    if not executable or not shutil.which("node"):
        return None
    for parent in Path(executable).resolve().parents:
        if (parent / "core/package-manager.js").is_file():
            return parent
    return None


DIST = installed_dist()


@unittest.skipUnless(DIST, "Installed Pi resolver and node not available")
class PiContractTests(unittest.TestCase):
    def test_both_scopes_against_pi_resolver(self):
        self.check_contract("index.ts")

    def test_directory_extensions_against_pi_resolver(self):
        self.check_contract(".")

    def check_contract(self, extension_entry):
        fixture = test_core.CoreTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        root = fixture.package(skills=[])
        (root / "package.json").write_text(json.dumps({"name": "demo", "version": "1.0.0", "pi": {"extensions": [extension_entry], "skills": ["skills"]}}))
        smart = fixture.agent / "extensions/smart"
        (smart / "src").mkdir(parents=True)
        (smart / "src/plugin.ts").write_text("export default () => {};")
        (smart / "package.json").write_text(json.dumps({"pi": {"extensions": ["src/plugin.ts"]}}))
        fixture.skill(fixture.agent / "skills/standalone/SKILL.md", "standalone")
        fixture.settings("project", {"packages": [{"source": "npm:demo", "autoload": False, "extensions": ["!**"]}]})
        manager = fixture.manager
        script = r"""
import { pathToFileURL } from 'node:url';
const [dist, agent, project, scope] = process.argv.slice(1);
const { SettingsManager } = await import(pathToFileURL(dist + '/core/settings-manager.js'));
const { DefaultPackageManager } = await import(pathToFileURL(dist + '/core/package-manager.js'));
const settingsManager = SettingsManager.create(project, agent);
settingsManager.setProjectTrusted(scope === 'project');
const pm = new DefaultPackageManager({cwd:project, agentDir:agent, settingsManager, builtinExtensions:['mcp','llama.cpp','codemode','tool-search']});
console.log(JSON.stringify(await pm.resolve()));
"""
        def compare(scope):
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script, str(DIST), str(fixture.agent), str(fixture.project), scope],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "HOME": str(fixture.home), "PI_OFFLINE": "1", "PI_CODING_AGENT_DIR": str(fixture.agent)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            native = json.loads(result.stdout)
            own = manager.state(scope)["resources"]
            for kind, items in own.items():
                expected = {r["path"]: r["enabled"] for r in native[kind]}
                actual = {r["path"]: r["enabled"] for r in items}
                self.assertEqual(actual, expected, (scope, kind))
        for scope in ("user", "project"):
            compare(scope)
            state = manager.state(scope)
            changes = [{"id": row["id"], "enabled": not row["enabled"]} for rows in state["resources"].values() for row in rows]
            manager.apply(scope, changes, state["revision"])
            compare(scope)
            state = manager.state(scope)
            manager.apply(scope, [{"id": row["id"], "enabled": not row["enabled"]} for rows in state["resources"].values() for row in rows], state["revision"])
            compare(scope)


if __name__ == "__main__":
    unittest.main()
