from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".github/scripts/prepare-gnome-extension-bumps.py"
)


class ExtensionBumpPreparationTests(unittest.TestCase):
    def git(self, cwd: Path, *args: str, env: dict[str, str]) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def create_upstream(self, path: Path, name: str, env: dict[str, str]) -> None:
        path.mkdir()
        self.git(path, "init", "-b", "main", env=env)
        self.git(path, "config", "user.name", "Extension Test", env=env)
        self.git(path, "config", "user.email", "extension-test@example.invalid", env=env)
        (path / "metadata.json").write_text('{"shell-version": ["51"]}\n')
        self.git(path, "add", "metadata.json", env=env)
        self.git(path, "commit", "-m", f"initial {name}", env=env)

    def test_invalid_head_is_skipped_without_blocking_valid_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env = os.environ.copy()
            env.pop("GITHUB_STEP_SUMMARY", None)
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "protocol.file.allow"
            env["GIT_CONFIG_VALUE_0"] = "always"

            root = base / "repo"
            root.mkdir()
            self.git(root, "init", "-b", "main", env=env)
            self.git(root, "config", "user.name", "Extension Test", env=env)
            self.git(root, "config", "user.email", "extension-test@example.invalid", env=env)

            remotes = {}
            for name in ("valid", "retired"):
                remote = base / f"{name}-upstream"
                self.create_upstream(remote, name, env)
                self.git(
                    root,
                    "submodule",
                    "add",
                    str(remote),
                    f"extensions/{name}",
                    env=env,
                )
                self.git(
                    root,
                    "config",
                    "--file",
                    ".gitmodules",
                    f"submodule.extensions/{name}.branch",
                    "main",
                    env=env,
                )
                remotes[name] = remote

            self.git(root, "add", ".gitmodules", "extensions", env=env)
            self.git(root, "commit", "-m", "pin extensions", env=env)
            old = {
                name: self.git(root, "rev-parse", f"HEAD:extensions/{name}", env=env)
                for name in remotes
            }

            (remotes["valid"] / "fix.js").write_text("// compatible upstream fix\n")
            self.git(remotes["valid"], "add", "fix.js", env=env)
            self.git(remotes["valid"], "commit", "-m", "valid update", env=env)
            (remotes["retired"] / "metadata.json").unlink()
            (remotes["retired"] / "README.md").write_text("Source was removed upstream.\n")
            self.git(remotes["retired"], "add", "-A", env=env)
            self.git(remotes["retired"], "commit", "-m", "remove extension source", env=env)
            upstream = {
                name: self.git(remote, "rev-parse", "HEAD", env=env)
                for name, remote in remotes.items()
            }
            report = base / "report.md"

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--root",
                    str(root),
                    "--report",
                    str(report),
                ],
                cwd=root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                self.git(root, "-C", "extensions/valid", "rev-parse", "HEAD", env=env),
                upstream["valid"],
            )
            self.assertEqual(
                self.git(root, "-C", "extensions/retired", "rev-parse", "HEAD", env=env),
                old["retired"],
            )
            contents = report.read_text()
            self.assertIn(
                f"| `extensions/valid` | `{old['valid'][:12]}` | `{upstream['valid'][:12]}` | 51 | proposed |",
                contents,
            )
            self.assertIn(
                f"| `extensions/retired` | `{old['retired'][:12]}` | `{upstream['retired'][:12]}` | 51 | skipped (invalid upstream metadata) |",
                contents,
            )
            self.assertIn("keeping", result.stderr)


if __name__ == "__main__":
    unittest.main()
