"""Packaging checks: the archives are deterministic, and a skill that ships code ships the whole runtime.

The Executor runtime is the reason `--side cloud` can claim `release_verified` at all: the pinned skill
version is the code that decided the tickets. So these tests assert the archive holds every module the
imports need (resolved from the sources themselves, not from a hand-copied list), that unpacking it gives a
tree `python -m live.golive` actually runs from, and that a manifest naming a file that is not there, or a
second copy of a repository file, fails the build instead of shipping a skill that only breaks in the cloud.
"""
import ast
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from agents import package_skills
from live.contracts import ROOT


class PackagingTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.folders = sorted(path for path in (ROOT / "skills").iterdir() if path.is_dir())
        self.lock = json.loads((ROOT / "agents" / "release-lock.json").read_text(encoding="utf-8"))

    def entry(self, name):
        return package_skills.check_skill(ROOT / "skills" / name)

    def test_every_skill_name_and_frontmatter_match_the_folder(self):
        self.assertEqual([folder.name for folder in self.folders],
                         ["paper-trading-gate", "pstar-executor", "pstar-risk-check",
                          "reconcile-paper-fills", "report-trading-day", "research-factor-evidence",
                          "validate-trading-targets"])
        self.assertEqual(len(self.folders), package_skills.EXPECTED_SKILLS)
        for folder in self.folders:
            with self.subTest(skill=folder.name):
                self.assertTrue(package_skills.NAME_RE.fullmatch(folder.name),
                                f"{folder.name}: the platform's skill name shape is "
                                f"{package_skills.NAME_RE.pattern}")
                entry = package_skills.check_skill(folder)
                self.assertEqual(entry["sources"]["SKILL.md"], self.lock["source_sha256"][
                    f"skills/{folder.name}/SKILL.md"], f"{folder.name}: source drifted from the release lock")
                self.assertLess(len(entry["members"]), 200)

    def test_the_executor_archive_carries_the_whole_import_closure(self):
        entry = self.entry("pstar-executor")
        shipped = {member for member in entry["members"] if "/" in member
                   and not member.startswith(("scripts/", "assets/"))}
        # The modules the Executor runs, named here rather than derived, so adding an import to golive.py
        # without packaging it is a failing test rather than a cloud run that cannot start.
        for module in ("live/golive.py", "live/contracts.py", "live/order_planner.py", "live/pstar.py",
                       "live/pause_switch.py", "live/risk_monitor.py", "live/relay.py",
                       "live/eligibility.py", "journal/store.py", "rules/gate.py", "rules/gate_pstar.py",
                       "rules/trading_calendar.py", "skills/pstar-risk-check/scripts/risk_check.py",
                       "live/config.pstar.json", "live/ops.json",
                       "live/targets/competition/2026-11-19-pstar.json"):
            self.assertIn(module, shipped, f"{module} is not in the Executor archive")
        self.assertEqual(json.loads(entry["bytes"][package_skills.RUNTIME_RECORD].decode())["runtime"],
                         sorted(shipped - {package_skills.RUNTIME_RECORD, "skill.json"}))
        self.assertEqual(entry["requirements"][:1], ["python>=3.11"])
        self.assertIn("exchange-calendars==4.13.3", entry["requirements"],
                      "the calendar check is a third-party dependency and has to be declared")

    def test_the_closure_is_the_imports_themselves_say(self):
        entry = self.entry("pstar-executor")
        runtime = json.loads(entry["bytes"][package_skills.RUNTIME_RECORD].decode())
        seeds = {"live/golive.py", "skills/pstar-risk-check/scripts/risk_check.py"}
        self.assertEqual(set(runtime["seeds"]), seeds)
        shipped = set(runtime["runtime"])
        for module in shipped:
            if not module.endswith(".py"):
                continue
            path = ROOT / module
            tree = ast.parse(package_skills.canonical_bytes(path).decode("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
                else:
                    continue
                for name in names:
                    target = package_skills.module_file(name)
                    if target is not None:
                        self.assertIn(package_skills.relative(target), shipped,
                                      f"{module} imports {name}, which the archive does not carry")

    def test_the_archive_unpacks_to_a_tree_the_cli_runs_from(self):
        entry = self.entry("pstar-executor")
        with tempfile.TemporaryDirectory() as work:
            archive = package_skills.build(entry, Path(work))
            zipfile.ZipFile(archive).extractall(Path(work))
            tree = Path(work) / "pstar-executor"
            self.assertTrue((tree / "live" / "golive.py").is_file())
            self.assertTrue((tree / "live" / "config.pstar.json").is_file())
            # Import it exactly the way a cloud session would, and prove the paths resolve inside the
            # unpacked tree rather than back into this checkout.
            done = subprocess.run(
                [sys.executable, "-B", "-c",
                 "import live.golive as g; from pathlib import Path;"
                 "print(Path(g.__file__).relative_to(Path.cwd()));"
                 "print(g.load_config(None)['variant']);"
                 "print(g.load_switch_ops(g.load_config(None), None, None)['pause_switch']['group'])"],
                cwd=tree, env={**os.environ, "PYTHONPATH": "."}, capture_output=True, text=True, timeout=300)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("live", done.stdout.splitlines()[0], "the module must come from this tree")
            self.assertIn("pstar15", done.stdout)
            self.assertIn("PSTAR-PAUSE", done.stdout)

    def test_a_manifest_that_names_a_missing_file_or_a_second_copy_fails(self):
        with tempfile.TemporaryDirectory() as work:
            copy = Path(work) / "pstar-executor"
            shutil.copytree(ROOT / "skills" / "pstar-executor", copy)
            manifest = json.loads((copy / "skill.json").read_text(encoding="utf-8"))
            manifest["include"] = manifest["include"] + ["live/no_such_config.json"]
            (copy / "skill.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "is not a file here"):
                package_skills.check_skill(copy)
        with tempfile.TemporaryDirectory() as work:
            copy = Path(work) / "pstar-executor"
            shutil.copytree(ROOT / "skills" / "pstar-executor", copy)
            (copy / "rules").mkdir()
            shutil.copy(ROOT / "rules" / "gate_pstar.py", copy / "rules" / "gate_pstar.py")
            with self.assertRaisesRegex(ValueError, "would be packaged twice"):
                package_skills.check_skill(copy)
        with tempfile.TemporaryDirectory() as work:
            copy = Path(work) / "pstar-executor"
            shutil.copytree(ROOT / "skills" / "pstar-executor", copy)
            (copy / "skill.json").write_text(json.dumps({"seeds": ["live/golive.py"],
                                                          "extra": True}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported keys"):
                package_skills.check_skill(copy)

    def test_archives_are_byte_identical_across_runs(self):
        for folder in self.folders:
            with self.subTest(skill=folder.name):
                entry = package_skills.check_skill(folder)
                with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
                    one = package_skills.build(entry, Path(first))
                    two = package_skills.build(entry, Path(second))
                    self.assertEqual(one.read_bytes(), two.read_bytes(), "nondeterministic archive")
                    self.assertLess(len(one.read_bytes()), package_skills.MAX_ARCHIVE_BYTES)
                    with zipfile.ZipFile(one) as handle:
                        self.assertEqual(sorted(handle.namelist()),
                                         sorted(f"{folder.name}/{member}" for member in entry["members"]))
                        self.assertIsNone(handle.testzip())

    def test_the_release_artifacts_pin_the_archives_this_source_builds(self):
        # Part B uploads a zip and then verifies the cloud copy against these digests, so a digest that
        # describes a stale build would make the upload pass and the verification mean nothing.
        templates = json.loads((ROOT / "agents" / "templates.json").read_text(encoding="utf-8"))
        for folder in self.folders:
            with self.subTest(skill=folder.name):
                entry = package_skills.check_skill(folder)
                self.assertEqual(templates["skill_versions"][folder.name]["local_content_sha256"],
                                 entry["content_sha256"],
                                 f"{folder.name}: templates.json pins content the skill folder no longer holds")
                lock_skill = self.lock["skills"][folder.name]
                self.assertEqual(lock_skill["local_content_sha256"], entry["content_sha256"],
                                 f"{folder.name}: release-lock.json pins stale content")
                with tempfile.TemporaryDirectory() as work:
                    archive = package_skills.build(entry, Path(work))
                    self.assertEqual(lock_skill["local_archive_sha256"],
                                     hashlib.sha256(archive.read_bytes()).hexdigest(),
                                     f"{folder.name}: release-lock.json pins an archive this source "
                                     f"does not build")

    def test_a_relative_out_directory_builds_and_reports_project_relative_paths(self):
        scratch = ROOT / ".cache" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="pkg-out-", dir=scratch))
        try:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = package_skills.main(["--out", work.relative_to(ROOT).as_posix()])
            report = json.loads(stdout.getvalue())
            self.assertEqual(code, 0, report["failures"])
            self.assertEqual({entry["archive"] for entry in report["skills"]},
                             {f".cache/tmp/{work.name}/{name}.zip" for name in self.lock["skills"]})
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_an_underscore_in_a_skill_name_is_allowed_and_a_space_is_not(self):
        self.assertTrue(package_skills.NAME_RE.fullmatch("pstar_risk_check"))
        for rejected in ("Pstar-executor", "-leading", "with space", "with.dot", ""):
            with self.subTest(name=rejected):
                self.assertIsNone(package_skills.NAME_RE.fullmatch(rejected))

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
                package_skills.build(entry, Path(work))
            self.assertEqual(target.read_bytes(), b"not-an-archive", "refusal must not clobber")


if __name__ == "__main__":
    unittest.main()
