"""Install/upgrade safely without touching the real user home."""

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("skill_install", ROOT / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class Installation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "release"
        self.home = Path(self.tmp.name) / "home"
        files, versions = {}, {}
        for skill in installer.SKILLS:
            folder = self.root / skill
            (folder / "lib").mkdir(parents=True)
            (folder / "SKILL.md").write_text("skill_ver: 1.2.3")
            (folder / "lib" / "pd_runtime.py").write_text("# fixture")
            (folder / "lib" / "pd_commands.py").write_text("# fixture")
            versions[skill] = "1.2.3"
        for path in self.root.rglob("*"):
            if path.is_file():
                files[str(path.relative_to(self.root))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        (self.root / "release-manifest.json").write_text(
            json.dumps({"files": files, "versions": versions})
        )

    def test_workbuddy_check_does_not_change_active_copy(self):
        old = self.home / ".workbuddy" / "skills" / "paperdaily"
        old.mkdir(parents=True)
        (old / "active.txt").write_text("keep running")
        result = installer.install(self.root, self.home, ["workbuddy"], False)
        self.assertEqual((old / "active.txt").read_text(), "keep running")
        self.assertFalse(old.is_symlink())
        self.assertTrue(all(r["status"] == "update_needed" for r in result["results"]))

    def test_check_has_no_side_effects(self):
        result = installer.install(self.root, self.home, ["codex"], False)
        self.assertFalse(self.home.exists())
        self.assertTrue(all(r["status"] == "update_needed" for r in result["results"]))

    def test_apply_backs_up_old_real_copy_outside_scanning_directory(self):
        old = self.home / ".codex" / "skills" / "paperdaily"
        old.mkdir(parents=True)
        (old / "custom.txt").write_text("preserved")
        result = installer.install(self.root, self.home, ["codex"], True)
        backup = Path(result["results"][0]["backup"])
        self.assertEqual((backup / "custom.txt").read_text(), "preserved")
        self.assertNotIn("/.codex/skills/", str(backup))
        self.assertTrue(old.is_symlink())

    def test_stale_symlink_target_is_not_modified(self):
        folder = self.home / ".agents" / "skills"
        folder.mkdir(parents=True)
        target = self.home / "old-external"
        target.mkdir()
        (target / "custom.txt").write_text("keep")
        (folder / "paperdaily").symlink_to(target, target_is_directory=True)
        installer.install(self.root, self.home, ["agents"], True)
        self.assertEqual((target / "custom.txt").read_text(), "keep")

    def test_second_install_is_noop(self):
        installer.install(self.root, self.home, list(installer.AGENTS), True)
        result = installer.install(self.root, self.home, list(installer.AGENTS), True)
        self.assertTrue(all(r["status"] == "current" for r in result["results"]))

    def test_tampered_release_fails_before_any_home_change(self):
        (self.root / "paperdaily" / "SKILL.md").write_text("changed")
        with self.assertRaises(ValueError):
            installer.install(self.root, self.home, ["codex"], True)
        self.assertFalse(self.home.exists())

    def test_symlinked_release_file_refused(self):
        file = self.root / "paperdaily" / "SKILL.md"
        file.unlink()
        file.symlink_to(self.root / "paperdaily-deep-research" / "SKILL.md")
        with self.assertRaises(ValueError):
            installer.verify(self.root)


if __name__ == "__main__":
    unittest.main()
