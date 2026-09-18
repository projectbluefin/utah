"""The syntax gate must cover every script, including ones added later.

The gate replaced a hand-maintained list inside `just check`. These tests
assert the two properties that made the list fail: that the inventory is
derived from the tree rather than restated, and that a broken script actually
fails the gate rather than being skipped.
"""
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "check_script_syntax", ROOT / "scripts" / "check-script-syntax.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, body=""):
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def test_extensions_decide_without_reading_the_file(self):
        self.assertEqual(gate.classify(self.write("a.sh")), "shell")
        self.assertEqual(gate.classify(self.write("a.py")), "python")

    def test_extensionless_files_are_classified_by_shebang(self):
        self.assertEqual(
            gate.classify(self.write("hook", "#!/usr/bin/bash\ntrue\n")), "shell"
        )
        self.assertEqual(
            gate.classify(self.write("tool", "#!/usr/bin/env python3\n")), "python"
        )

    def test_non_scripts_are_skipped(self):
        self.assertIsNone(gate.classify(self.write("notes.md", "#!/usr/bin/bash\n")))
        self.assertIsNone(gate.classify(self.write("data", "key=value\n")))
        # A shebang inside a unit file is data the gate must not try to parse.
        self.assertIsNone(
            gate.classify(self.write("x.service", "#!/usr/bin/bash\n[Unit]\n"))
        )


class CheckerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_broken_shell_is_reported(self):
        path = self.dir / "bad.sh"
        path.write_text("if true; then\n")
        self.assertIsNotNone(gate.check_shell(path))

    def test_valid_shell_passes(self):
        path = self.dir / "ok.sh"
        path.write_text("set -e\nif true; then echo hi; fi\n")
        self.assertIsNone(gate.check_shell(path))

    def test_broken_python_is_reported(self):
        path = self.dir / "bad.py"
        path.write_text("def f(:\n")
        self.assertIsNotNone(gate.check_python(path))

    def test_python_check_leaves_no_pycache(self):
        path = self.dir / "ok.py"
        path.write_text("x = 1\n")
        self.assertIsNone(gate.check_python(path))
        self.assertFalse((self.dir / "__pycache__").exists())


class InventoryTests(unittest.TestCase):
    def test_gate_covers_scripts_the_old_hand_list_missed(self):
        listed = subprocess.run(
            ["python3", str(ROOT / "scripts" / "check-script-syntax.py"),
             "--root", str(ROOT), "--list"],
            check=True, capture_output=True, text=True,
        ).stdout
        covered = {line.split("\t", 1)[1] for line in listed.splitlines() if line}
        for missed in (
            "scripts/resolve-e2e-inputs.py",
            "scripts/update-e2e-readme.py",
            "iso/scripts/live-kernel.py",
            "iso/scripts/luks-unlock.py",
            "iso/scripts/boot-installed.sh",
            "scripts/build-gnome-extensions.sh",
            "system_files/shared/usr/libexec/bluefin-refresh-stats",
        ):
            self.assertIn(missed, covered)

    def test_repository_passes_its_own_gate(self):
        result = subprocess.run(
            ["python3", str(ROOT / "scripts" / "check-script-syntax.py"),
             "--root", str(ROOT)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_newly_added_broken_script_fails_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git("init", "-q", cwd=repo)
            (repo / "new.sh").write_text("for x in; do\n")
            git("add", "new.sh", cwd=repo)
            result = subprocess.run(
                ["python3", str(ROOT / "scripts" / "check-script-syntax.py"),
                 "--root", str(repo)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("new.sh", result.stderr)

    def test_untracked_files_are_not_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git("init", "-q", cwd=repo)
            (repo / "kept.sh").write_text("true\n")
            git("add", "kept.sh", cwd=repo)
            (repo / "scratch.sh").write_text("if true; then\n")
            result = subprocess.run(
                ["python3", str(ROOT / "scripts" / "check-script-syntax.py"),
                 "--root", str(repo)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
