from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from piluli_core import PiManager, PiluliError, Draft, filtered, identity, metadata


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.agent = self.root / "agent"
        self.project = self.root / "project"
        self.home = self.root / "home"
        for path in (self.agent, self.project, self.home):
            path.mkdir()
        (self.project / ".git").mkdir()
        self.manager = PiManager(self.agent, self.project, home=self.home)

    def settings(self, scope, data):
        path = self.manager.paths[scope]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def package(self, scope="user", name="demo", **filters):
        root = self.manager.bases[scope] / "npm/node_modules" / name
        root.mkdir(parents=True)
        (root / "package.json").write_text(json.dumps({"name": name, "version": "1.0.0", "pi": {"extensions": ["./index.ts"], "skills": ["./skills"]}}))
        (root / "index.ts").write_text("export default () => {};")
        for skill in ("one", "two"):
            self.skill(root / "skills" / skill / "SKILL.md", skill)
        entry = {"source": "npm:" + name, **filters} if filters else "npm:" + name
        self.settings(scope, {"packages": [entry], "theme": "keep-me"})
        return root

    def skill(self, path, name="demo"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {name}\ndescription: >\n  First line\n  second line\n---\n# Instructions")
        return path

    def rows(self, scope="project", kind="extensions"):
        return self.manager.state(scope)["resources"][kind]

    def toggle(self, scope, row, value):
        state = self.manager.state(scope)
        return self.manager.apply(scope, [{"id": row["id"], "enabled": value}], state["revision"])

    def test_user_and_project_package_paths_are_separate(self):
        user_root = self.package("user")
        project_root = self.package("project")
        self.assertEqual(self.manager.state("user")["packages"][0]["path"], str(user_root))
        self.assertEqual(self.manager.state("project")["packages"][0]["path"], str(project_root))
        self.assertEqual(len([r for r in self.rows() if r["source"] == "npm:demo"]), 1)

    def test_inherited_extension_disables_only_project_and_reenables(self):
        self.package()
        before = self.manager.paths["user"].read_bytes()
        row = next(r for r in self.rows() if r["source"] == "npm:demo")
        self.toggle("project", row, False)
        data = json.loads(self.manager.paths["project"].read_text())
        self.assertEqual(data["packages"], [{"source": "npm:demo", "autoload": False, "extensions": ["-index.ts"]}])
        self.assertEqual(self.manager.paths["user"].read_bytes(), before)
        self.assertFalse(next(r for r in self.rows() if r["id"] == row["id"])["enabled"])
        self.toggle("project", row, True)
        self.assertTrue(next(r for r in self.rows() if r["id"] == row["id"])["enabled"])

    def test_user_toggle_preserves_project_override(self):
        self.package()
        row = next(r for r in self.rows() if r["source"] == "npm:demo")
        self.toggle("project", row, True)
        before = self.manager.paths["project"].read_bytes()
        self.toggle("user", row, False)
        self.assertEqual(self.manager.paths["project"].read_bytes(), before)
        self.assertFalse(next(r for r in self.rows("user") if r["id"] == row["id"])["enabled"])
        self.assertTrue(next(r for r in self.rows() if r["id"] == row["id"])["enabled"])

    def test_empty_filter_stays_disabled_except_explicit_skill(self):
        self.package(skills=[], extensions=[])
        state = self.manager.state("user")
        skills = state["resources"]["skills"]
        self.assertFalse(any(r["enabled"] for r in skills))
        self.toggle("user", skills[0], True)
        self.assertEqual([r["enabled"] for r in self.rows("user", "skills")], [True, False])
        entry = json.loads(self.manager.paths["user"].read_text())["packages"][0]
        self.assertEqual(entry["extensions"], [])
        self.assertEqual(entry["skills"], ["!**", "+skills/one/SKILL.md"])

    def test_delta_inherits_disabled_skills_and_can_force_enable(self):
        self.package(skills=[])
        self.settings("project", {"packages": [{"source": "npm:demo", "autoload": False, "extensions": ["!**"]}]})
        skills = self.rows(kind="skills")
        self.assertFalse(any(r["enabled"] for r in skills))
        self.toggle("project", skills[0], True)
        self.assertEqual([r["enabled"] for r in self.rows(kind="skills")], [True, False])

    def test_standalone_user_skill_project_override_has_plain_path(self):
        file = self.skill(self.agent / "skills/a/SKILL.md")
        row = self.rows(kind="skills")[0]
        self.toggle("project", row, False)
        self.assertEqual(json.loads(self.manager.paths["project"].read_text())["skills"], [str(file), "-" + str(file)])
        self.assertFalse(self.rows(kind="skills")[0]["enabled"])
        self.assertTrue(self.rows("user", "skills")[0]["enabled"])
        self.toggle("project", row, True)
        self.assertTrue(self.rows(kind="skills")[0]["enabled"])

    def test_agent_skill_directory_and_symlinks(self):
        file = self.skill(self.home / ".agents/skills/test/SKILL.md")
        target = self.root / "target"
        self.skill(target / "SKILL.md", "linked")
        (self.agent / "skills").mkdir()
        (self.agent / "skills/linked").symlink_to(target, target_is_directory=True)
        skills = self.rows("user", "skills")
        self.assertEqual(len(skills), 2)
        self.assertIn(str(file), [r["path"] for r in skills])
        self.assertIn(str(self.agent / "skills/linked/SKILL.md"), [r["path"] for r in skills])

    def test_project_agents_scope_does_not_leak_ancestors_above_repo(self):
        self.skill(self.root / ".agents/skills/outside/SKILL.md")
        self.skill(self.project / ".agents/skills/inside/SKILL.md")
        self.assertEqual(len(self.rows(kind="skills")), 1)

    def test_manifest_exclusions_are_not_togglable(self):
        root = self.package()
        (root / "package.json").write_text(json.dumps({"pi": {"skills": ["skills", "!skills/two/**"]}}))
        skills = self.rows(kind="skills")
        self.assertEqual([r["name"] for r in skills], ["one"])
        self.assertFalse(any(r["source"] == "npm:demo" for r in self.rows()))

    def test_atomic_batch_and_revision_conflict(self):
        self.package()
        state = self.manager.state("user")
        row = state["resources"]["skills"][0]
        before = self.manager.paths["user"].read_bytes()
        with self.assertRaises(PiluliError):
            self.manager.apply("user", [{"id": row["id"], "enabled": False}, {"id": "missing", "enabled": True}], state["revision"])
        self.assertEqual(self.manager.paths["user"].read_bytes(), before)
        self.settings("project", {"theme": "changed elsewhere"})
        with self.assertRaisesRegex(PiluliError, "another process"):
            self.manager.apply("user", [{"id": row["id"], "enabled": False}], state["revision"])
        self.assertEqual(self.manager.paths["user"].read_bytes(), before)

    def test_batch_preserves_both_types_and_unrelated_settings(self):
        self.package()
        state = self.manager.state("user")
        rows = [next(r for r in state["resources"]["extensions"] if r["source"] == "npm:demo"), state["resources"]["skills"][0]]
        self.manager.apply("user", [{"id": r["id"], "enabled": False} for r in rows], state["revision"])
        data = json.loads(self.manager.paths["user"].read_text())
        self.assertEqual(data["theme"], "keep-me")
        self.assertEqual(data["packages"][0]["extensions"], ["-index.ts"])
        self.assertEqual(data["packages"][0]["skills"], ["-skills/one/SKILL.md"])

    def test_builtin_toggle_scoped(self):
        row = next(r for r in self.rows() if r["path"] == "builtin:mcp")
        self.toggle("project", row, False)
        self.assertFalse(next(r for r in self.rows() if r["id"] == row["id"])["enabled"])
        self.assertTrue(next(r for r in self.rows("user") if r["id"] == row["id"])["enabled"])

    def test_draft_toggles_twice_and_discards_without_writes(self):
        state = self.manager.state()
        before = copy.deepcopy(state)
        draft = Draft(state)
        row = state["resources"]["extensions"][0]
        draft.toggle(row)
        self.assertEqual(len(draft.payload()), 1)
        draft.toggle(row)
        self.assertEqual(draft.payload(), [])
        draft.toggle(row)
        draft.discard()
        self.assertEqual(state, before)
        self.assertFalse(self.manager.paths["project"].exists())

    def test_invalid_scope_and_boolean_are_rejected(self):
        with self.assertRaises(PiluliError):
            self.manager.state("oops")
        state = self.manager.state()
        with self.assertRaises(PiluliError):
            self.manager.apply("project", [{"id": state["resources"]["extensions"][0]["id"], "enabled": 1}], state["revision"])

    def test_package_operations_use_scope_flags_and_no_broad_update(self):
        self.package()
        state = self.manager.state("user")
        with patch.object(self.manager, "_run", return_value="ok") as run:
            self.manager.package_action("user", "update", package_id=state["packages"][0]["id"], revision=state["revision"])
            run.assert_called_once_with(["install", "npm:demo", "--no-approve"])
        with patch.object(self.manager, "_run", return_value="ok") as run:
            self.manager.package_action("project", "install", source="npm:new", revision=state["revision"])
            run.assert_called_once_with(["install", "npm:new", "--local", "--approve"])
        with patch.object(self.manager, "_run") as run, self.assertRaises(PiluliError):
            self.manager.package_action("project", "remove", package_id=state["packages"][0]["id"], revision=state["revision"])
            run.assert_not_called()

    def test_source_argument_injection_rejected(self):
        for source in ("--all", "\n", "npm:x\x00", "npm:x\n--all"):
            with self.assertRaises(PiluliError):
                self.manager.validate_source(source)

    def test_local_package_identity_relative_to_its_scope(self):
        self.assertEqual(identity("../x", self.agent), identity(str(self.root / "x"), self.project))
        self.assertEqual(identity("npm:@scope/demo@1.0", self.agent), identity("npm:@scope/demo@2", self.project))
        self.assertEqual(identity("git:git@github.com:org/repo.git@v1", self.agent), "git:github.com/org/repo")

    def test_regular_filter_precedence_and_recursive_glob(self):
        file = str(self.agent / "skills/one/SKILL.md")
        self.assertFalse(filtered(file, ["-skills/one", "+skills/one", "!**"], self.agent))
        self.assertTrue(filtered(file, ["!**", "+skills/one"], self.agent))
        self.assertFalse(filtered(file, ["skills/*.md"], self.agent))
        self.assertTrue(filtered(file, ["skills/**/*.md"], self.agent))

    def test_frontmatter_block_scalars(self):
        file = self.skill(self.project / ".pi/skills/a/SKILL.md")
        self.assertEqual(metadata(file)["description"], "First line second line")

    def skills_cli_lock(self, agents_dir, *names):
        agents_dir.mkdir(parents=True, exist_ok=True)
        data = {"version": 3, "skills": {name: {"source": "org/repo-" + name} for name in names}}
        (agents_dir / ".skill-lock.json").write_text(json.dumps(data))

    def test_skills_cli_lock_marks_managed_skills_only(self):
        self.skill(self.home / ".agents/skills/managed/SKILL.md", "managed")
        self.skill(self.home / ".agents/skills/plain/SKILL.md", "plain")
        self.skill(self.agent / "skills/local/SKILL.md", "local")
        self.skills_cli_lock(self.home / ".agents", "managed")
        rows = {r["name"]: r for r in self.rows("user", "skills")}
        self.assertEqual(rows["managed"]["skillsCli"], "org/repo-managed")
        self.assertEqual(rows["plain"]["skillsCli"], "")
        self.assertEqual(rows["local"]["skillsCli"], "")

    def test_skills_cli_symlink_marks_once_and_project_lock(self):
        managed = self.skill(self.home / ".agents/skills/managed/SKILL.md", "managed")
        self.skill(self.project / ".agents/skills/local-skill/SKILL.md", "local-skill")
        self.skills_cli_lock(self.home / ".agents", "managed")
        self.skills_cli_lock(self.project / ".agents", "local-skill")
        (self.agent / "skills").mkdir()
        (self.agent / "skills/managed").symlink_to(managed.parent, target_is_directory=True)
        rows = self.rows(kind="skills")
        self.assertEqual(len([r for r in rows if r["name"] == "managed"]), 1)
        self.assertEqual({r["name"]: r["skillsCli"] for r in rows},
                         {"managed": "org/repo-managed", "local-skill": "org/repo-local-skill"})

    def test_skill_action_removes_through_skills_cli(self):
        self.skill(self.home / ".agents/skills/managed/SKILL.md", "managed")
        self.skill(self.project / ".agents/skills/local-skill/SKILL.md", "local-skill")
        self.skills_cli_lock(self.home / ".agents", "managed")
        self.skills_cli_lock(self.project / ".agents", "local-skill")
        state = self.manager.state("user")
        row = state["resources"]["skills"][0]
        with patch.object(self.manager, "_run", return_value="ok") as run:
            self.manager.skill_action("user", "remove", resource_id=row["id"], revision=state["revision"])
            run.assert_called_once_with(["-y", "skills", "remove", "managed", "-y", "--global"], executable="npx")
        state = self.manager.state("project")
        row = next(r for r in state["resources"]["skills"] if r["name"] == "local-skill")
        with patch.object(self.manager, "_run", return_value="ok") as run:
            self.manager.skill_action("project", "remove", resource_id=row["id"], revision=state["revision"])
            run.assert_called_once_with(["-y", "skills", "remove", "local-skill", "-y"], executable="npx")

    def test_skill_action_rejects_unmanaged_unknown_and_stale(self):
        self.skill(self.agent / "skills/plain/SKILL.md", "plain")
        state = self.manager.state("user")
        row = state["resources"]["skills"][0]
        with patch.object(self.manager, "_run") as run:
            with self.assertRaisesRegex(PiluliError, "skills CLI"):
                self.manager.skill_action("user", "remove", resource_id=row["id"], revision=state["revision"])
            with self.assertRaises(PiluliError):
                self.manager.skill_action("user", "remove", resource_id="missing", revision=state["revision"])
            with self.assertRaises(PiluliError):
                self.manager.skill_action("user", "update", resource_id=row["id"], revision=state["revision"])
            self.settings("project", {"theme": "changed elsewhere"})
            self.skill(self.home / ".agents/skills/managed/SKILL.md", "managed")
            self.skills_cli_lock(self.home / ".agents", "managed")
            managed = next(r for r in self.manager.state("user")["resources"]["skills"] if r["name"] == "managed")
            with self.assertRaisesRegex(PiluliError, "Refresh"):
                self.manager.skill_action("user", "remove", resource_id=managed["id"], revision=state["revision"])
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
