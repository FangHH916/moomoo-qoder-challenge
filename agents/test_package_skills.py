import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from agents import package_skills
from live.contracts import ROOT


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.folders = sorted(path for path in (ROOT / "skills").iterdir() if path.is_dir())
        self.lock = json.loads((ROOT / "agents" / "release-lock.json").read_text(encoding="utf-8"))

    def test_five_skills_match_locked_sources_and_build_identical_archives(self):
        self.assertEqual([folder.name for folder in self.folders], sorted(self.lock["skills"]))
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            for folder in self.folders:
                entry = package_skills.check_skill(folder)
                self.assertEqual(entry["sources"]["SKILL.md"],
                                 self.lock["source_sha256"][f"skills/{folder.name}/SKILL.md"],
                                 f"{folder.name}: source drifted from the release lock")
                archive = package_skills.build(entry, folder, Path(first))
                other = package_skills.build(entry, folder, Path(second))
                self.assertEqual(archive.read_bytes(), other.read_bytes(), f"{folder.name}: nondeterministic archive")
                self.assertLess(len(archive.read_bytes()), package_skills.MAX_ARCHIVE_BYTES)
                with zipfile.ZipFile(archive) as handle:
                    self.assertEqual(handle.namelist(), [f"{folder.name}/SKILL.md"])
                    self.assertIsNone(handle.testzip())

    def test_folder_name_must_match_frontmatter(self):
        with tempfile.TemporaryDirectory() as work:
            moved = Path(work) / "renamed-skill"
            shutil.copytree(ROOT / "skills" / "paper-trading-gate", moved)
            with self.assertRaisesRegex(ValueError, "must match the folder"):
                package_skills.check_skill(moved)

    def test_missing_description_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            copy = Path(work) / "paper-trading-gate"
            shutil.copytree(ROOT / "skills" / "paper-trading-gate", copy)
            source = copy / "SKILL.md"
            source.write_text("\n".join(line for line in source.read_text(encoding="utf-8").splitlines()
                                        if not line.startswith("description:")), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "description is required"):
                package_skills.check_skill(copy)

    def test_conflicting_existing_archive_is_refused(self):
        entry = package_skills.check_skill(self.folders[0])
        with tempfile.TemporaryDirectory() as work:
            target = Path(work) / f"{entry['name']}.zip"
            target.write_bytes(b"not-an-archive")
            with self.assertRaisesRegex(ValueError, "existing archive differs"):
                package_skills.build(entry, ROOT / "skills" / entry["name"], Path(work))
            self.assertEqual(target.read_bytes(), b"not-an-archive", "refusal must not clobber")


if __name__ == "__main__":
    unittest.main()
