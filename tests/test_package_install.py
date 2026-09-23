"""Executed coverage for scripts/install-packages.py's install path.

`tests/test_package_resolution.py` covers `--check`, `--resolve`, and the
module-level helpers. The default install path in `main()` -- lines 227-271 --
runs during image composition to actually invoke DNF and build the image, but
was previously unexercised by any test suite.

These tests replace process boundaries (dnf, rpm, filesystem writes to
`/usr/share/utah`) and assert:
- The resolved contract is recorded to `/usr/share/utah/contract.txt`, one
  package per line, excluding `[build]` tooling and `[unavailable]` packages.
- An unwritable contract record warns and fails open (still installs).
- DNF disables all repositories, enables only marked `# utah-install: true`
  repositories in priority order, and excludes `PackageKit*`.
- `mark user` covers the contract and build deps, running before excluded removal.
- Only excluded packages that rpm reports as installed are removed, with
  `--no-autoremove` placed after `remove`.
- A non-zero exit from install or mark stops the build before subsequent steps.
- `[unavailable]` packages are announced in the build log.
"""

from __future__ import annotations

import importlib.util
import io
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
RESOLVED_CONTRACT = "/usr/share/utah/contract.txt"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "install_packages", ROOT / "scripts" / "install-packages.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load_module()


def toml_section(name: str, packages: list[str]) -> str:
    listing = ", ".join(f'"{pkg}"' for pkg in packages)
    return f"[{name}]\npackages = [{listing}]\n"


def write_manifest(
    directory: Path,
    *,
    fedora: list[str] | None = None,
    fedora_release: list[str] | None = None,
    release_version: str = "44",
    excluded: list[str] | None = None,
) -> Path:
    path = directory / "bluefin.toml"
    content = toml_section("fedora", fedora or [])
    if fedora_release is not None:
        content += toml_section(f"fedora_v{release_version}", fedora_release)
    if excluded is not None:
        content += toml_section("excluded", excluded)
    path.write_text(content)
    return path


def write_overlay(
    directory: Path,
    *,
    gnome: list[str] | None = None,
    parity: list[str] | None = None,
    services: list[str] | None = None,
    build: list[str] | None = None,
    unavailable: list[str] | None = None,
) -> Path:
    path = directory / "utah.toml"
    path.write_text(
        toml_section("gnome", gnome or [])
        + toml_section("parity", parity or [])
        + toml_section("services", services or [])
        + toml_section("build", build or [])
        + toml_section("unavailable", unavailable or [])
    )
    return path


def write_repos(directory: Path) -> Path:
    repos_dir = directory / "repos"
    repos_dir.mkdir(parents=True, exist_ok=True)
    # Order: priority 5 before priority 50. Unmarked repo should not be enabled.
    (repos_dir / "low-prio.repo").write_text(
        "[low-prio-repo]\n# utah-install: true\npriority=50\n"
    )
    (repos_dir / "high-prio.repo").write_text(
        "[high-prio-repo]\n# utah-install: true\npriority=5\n"
    )
    (repos_dir / "unmarked.repo").write_text(
        "[unmarked-repo]\npriority=1\n"
    )
    return repos_dir


class InstalledHelperTests(unittest.TestCase):
    """Unit coverage for the installed() helper function."""

    def test_installed_empty_packages_returns_empty_without_rpm(self):
        with patch.object(installer.subprocess, "run") as mock_run:
            self.assertEqual(installer.installed([]), [])
            mock_run.assert_not_called()

    def test_installed_queries_rpm_and_sorts_deduplicates(self):
        fake_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="pkg-b\npkg-a\npkg-b\n"
        )
        with patch.object(installer.subprocess, "run", return_value=fake_proc) as mock_run:
            result = installer.installed(["pkg-a", "pkg-b"])
            self.assertEqual(result, ["pkg-a", "pkg-b"])
            mock_run.assert_called_once_with(
                ["rpm", "-qa", "--queryformat=%{NAME}\n", "pkg-a", "pkg-b"],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_installed_returns_empty_when_no_rpm_matches(self):
        fake_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="")
        with patch.object(installer.subprocess, "run", return_value=fake_proc):
            self.assertEqual(installer.installed(["not-installed"]), [])


class RunHelperTests(unittest.TestCase):
    """Unit coverage for the run() helper function."""

    def test_run_prints_command_and_returns_returncode(self):
        fake_proc = subprocess.CompletedProcess(args=[], returncode=17)
        stdout = io.StringIO()
        with patch.object(installer.subprocess, "run", return_value=fake_proc) as mock_subproc, \
                redirect_stdout(stdout):
            rc = installer.run("dnf5", "install", "foo")
        self.assertEqual(rc, 17)
        mock_subproc.assert_called_once_with(("dnf5", "install", "foo"), check=False)
        self.assertEqual(stdout.getvalue(), "+ dnf5 install foo\n")


class PackageInstallPathTests(unittest.TestCase):
    """The install path in main() must execute the contract, mark user, and remove excluded."""

    def run_main(
        self,
        manifest: Path,
        overlay: Path | None,
        repos_dir: Path,
        *,
        contract_target: Path | None = None,
        installed_excluded: list[str] | None = None,
        run_returncodes: list[int] | None = None,
        fail_contract_write: OSError | None = None,
        major: str = "44",
        dnf_bin: str = "/usr/bin/dnf5",
    ) -> tuple[int, list[list[str]], str]:
        """Drives installer.main() on the install path with boundaries mocked."""
        real_path = installer.Path
        commands: list[list[str]] = []
        returncodes = list(run_returncodes or [])

        with ExitStack() as stack:
            if contract_target is None and fail_contract_write is None:
                # Never let an unparameterised test touch the real
                # /usr/share/utah/contract.txt: as root that write succeeds and
                # overwrites the host's system file.
                sink = Path(stack.enter_context(tempfile.TemporaryDirectory()))
                contract_target = sink / "usr/share/utah/contract.txt"

            return self._drive_main(
                manifest,
                overlay,
                repos_dir,
                real_path=real_path,
                commands=commands,
                returncodes=returncodes,
                contract_target=contract_target,
                installed_excluded=installed_excluded,
                fail_contract_write=fail_contract_write,
                major=major,
                dnf_bin=dnf_bin,
            )

    def _drive_main(
        self,
        manifest: Path,
        overlay: Path | None,
        repos_dir: Path,
        *,
        real_path,
        commands: list[list[str]],
        returncodes: list[int],
        contract_target: Path | None,
        installed_excluded: list[str] | None,
        fail_contract_write: OSError | None,
        major: str,
        dnf_bin: str,
    ) -> tuple[int, list[list[str]], str]:
        def fake_path(arg, *rest):
            if str(arg) == RESOLVED_CONTRACT:
                if fail_contract_write:
                    mock_obj = MagicMock()
                    mock_obj.parent.mkdir = MagicMock()
                    mock_obj.write_text.side_effect = fail_contract_write
                    return mock_obj
                if contract_target is not None:
                    return real_path(contract_target)
            return real_path(arg, *rest)

        def fake_run(*args: str) -> int:
            commands.append(list(args))
            if returncodes:
                return returncodes.pop(0)
            return 0

        installed_map = set(installed_excluded or [])

        def fake_installed(pkgs: list[str]) -> list[str]:
            return sorted(set(pkgs) & installed_map)

        argv = ["install-packages.py", "--repos-dir", str(repos_dir), str(manifest)]
        if overlay is not None:
            argv.append(str(overlay))

        stdout = io.StringIO()
        with patch.object(installer, "Path", fake_path), \
                patch.object(installer, "dnf_path", return_value=dnf_bin), \
                patch.object(installer, "fedora_major", return_value=major), \
                patch.object(installer, "run", side_effect=fake_run), \
                patch.object(installer, "installed", side_effect=fake_installed), \
                patch.object(installer.sys, "argv", argv), \
                redirect_stdout(stdout):
            code = installer.main()

        return code, commands, stdout.getvalue()

    def test_resolved_contract_recorded_excluding_build_and_unavailable(self):
        """The resolved contract is written to /usr/share/utah/contract.txt excluding build and unavailable."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repos_dir = write_repos(tmp_path)
            manifest = write_manifest(
                tmp_path,
                fedora=["base-pkg", "dropped-unavail"],
                fedora_release=["release-specific"],
                release_version="44",
            )
            overlay = write_overlay(
                tmp_path,
                gnome=["gnome-shell"],
                parity=["fastfetch"],
                services=["tailscale"],
                build=["compiler-tool", "meson"],
                unavailable=["dropped-unavail"],
            )
            contract_dest = tmp_path / "usr/share/utah/contract.txt"

            code, commands, out = self.run_main(
                manifest, overlay, repos_dir, contract_target=contract_dest
            )

            self.assertEqual(code, 0)
            self.assertTrue(contract_dest.is_file())
            content = contract_dest.read_text()
            expected_packages = [
                "base-pkg",
                "release-specific",
                "gnome-shell",
                "fastfetch",
                "tailscale",
            ]
            self.assertEqual(content, "".join(f"{p}\n" for p in expected_packages))
            # Verify build tooling and unavailable packages are excluded
            self.assertNotIn("compiler-tool", content)
            self.assertNotIn("meson", content)
            self.assertNotIn("dropped-unavail", content)

    def test_unwritable_record_warns_and_fails_open(self):
        """An unwritable contract record warns in the build log and still installs."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repos_dir = write_repos(tmp_path)
            manifest = write_manifest(tmp_path, fedora=["base-pkg"])
            overlay = write_overlay(tmp_path)

            code, commands, out = self.run_main(
                manifest,
                overlay,
                repos_dir,
                fail_contract_write=OSError("Read-only file system"),
            )

            self.assertEqual(code, 0)
            self.assertIn("WARNING: could not record the resolved contract: Read-only file system", out)
            # DNF install and mark user still ran despite the record failure
            self.assertEqual(len(commands), 2)
            self.assertEqual(commands[0][0], "/usr/bin/dnf5")
            self.assertEqual(commands[1][2:4], ["mark", "user"])

    def test_install_disables_all_repos_enables_marked_repos_in_priority_order_and_excludes_packagekit(self):
        """Install disables repos, enables # utah-install: true in priority order, and excludes PackageKit*."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repos_dir = write_repos(tmp_path)
            manifest = write_manifest(
                tmp_path,
                fedora=["base-pkg"],
                fedora_release=["release-pkg"],
                release_version="44",
            )
            overlay = write_overlay(
                tmp_path,
                gnome=["gnome-shell"],
                parity=["fastfetch"],
                services=["tailscale"],
                build=["compiler"],
            )

            code, commands, out = self.run_main(manifest, overlay, repos_dir)

            self.assertEqual(code, 0)
            install_cmd = commands[0]
            self.assertEqual(install_cmd[0], "/usr/bin/dnf5")
            self.assertEqual(install_cmd[1], "-y")
            self.assertIn("--disablerepo=*", install_cmd)

            # Marked repos: high-prio-repo (priority 5) must precede low-prio-repo (priority 50)
            enablerepo_flags = [arg for arg in install_cmd if arg.startswith("--enablerepo=")]
            self.assertEqual(
                enablerepo_flags,
                ["--enablerepo=high-prio-repo", "--enablerepo=low-prio-repo"],
            )
            # Unmarked repository must not be enabled
            self.assertNotIn("--enablerepo=unmarked-repo", install_cmd)

            # PackageKit* must be excluded
            pk_index = install_cmd.index("-x")
            self.assertEqual(install_cmd[pk_index + 1], "PackageKit*")
            self.assertEqual(install_cmd[pk_index + 2], "install")

            # Arguments after 'install' are packages followed by build deps
            installed_args = install_cmd[install_cmd.index("install") + 1:]
            expected_args = [
                "base-pkg",
                "release-pkg",
                "gnome-shell",
                "fastfetch",
                "tailscale",
                "compiler",
            ]
            self.assertEqual(installed_args, expected_args)

    def test_mark_user_covers_contract_and_runs_before_excluded_removal(self):
        """`mark user` covers both contract and build packages, and runs before excluded removal."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repos_dir = write_repos(tmp_path)
            manifest = write_manifest(
                tmp_path,
                fedora=["base-pkg"],
                excluded=["unwanted-pkg"],
            )
            overlay = write_overlay(
                tmp_path,
                gnome=["gnome-shell"],
                build=["compiler"],
            )

            code, commands, out = self.run_main(
                manifest,
                overlay,
                repos_dir,
                installed_excluded=["unwanted-pkg"],
            )

            self.assertEqual(code, 0)
            self.assertEqual(len(commands), 3)

            # Command 0: install
            self.assertEqual(commands[0][commands[0].index("install")], "install")

            # Command 1: mark user
            mark_cmd = commands[1]
            self.assertEqual(mark_cmd[:4], ["/usr/bin/dnf5", "-y", "mark", "user"])
            self.assertEqual(mark_cmd[4:], ["base-pkg", "gnome-shell", "compiler"])

            # Command 2: remove (runs strictly after mark user)
            remove_cmd = commands[2]
            self.assertEqual(remove_cmd[:4], ["/usr/bin/dnf5", "-y", "remove", "--no-autoremove"])

    def test_only_installed_excluded_packages_removed_with_no_autoremove_after_remove(self):
        """Only excluded packages actually installed are removed, with --no-autoremove after remove."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repos_dir = write_repos(tmp_path)
            manifest = write_manifest(
                tmp_path,
                fedora=["base-pkg"],
                excluded=["installed-exc-1", "absent-exc", "installed-exc-2"],
            )
            overlay = write_overlay(tmp_path)

            code, commands, out = self.run_main(
                manifest,
                overlay,
                repos_dir,
                installed_excluded=["installed-exc-1", "installed-exc-2"],
            )

            self.assertEqual(code, 0)
            self.assertEqual(len(commands), 3)
            remove_cmd = commands[2]

            # Verify command ordering: remove must immediately precede --no-autoremove (dnf5 requirement)
            self.assertEqual(remove_cmd[0], "/usr/bin/dnf5")
            self.assertEqual(remove_cmd[1], "-y")
            self.assertEqual(remove_cmd[2], "remove")
            self.assertEqual(remove_cmd[3], "--no-autoremove")

            # Only the 2 packages reported by rpm query are targeted for removal
            self.assertEqual(remove_cmd[4:], ["installed-exc-1", "installed-exc-2"])
            self.assertNotIn("absent-exc", remove_cmd)
            self.assertIn("Removing 2 excluded packages: installed-exc-1 installed-exc-2", out)

    def test_no_excluded_packages_installed_skips_removal(self):
        """When rpm reports no excluded packages installed, removal is skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repos_dir = write_repos(tmp_path)
            manifest = write_manifest(
                tmp_path,
                fedora=["base-pkg"],
                excluded=["absent-1", "absent-2"],
            )
            overlay = write_overlay(tmp_path)

            code, commands, out = self.run_main(
                manifest,
                overlay,
                repos_dir,
                installed_excluded=[],
            )

            self.assertEqual(code, 0)
            # Only install and mark user should run; no remove command
            self.assertEqual(len(commands), 2)
            self.assertIn("No excluded packages found to remove.", out)

    def test_empty_excluded_manifest_skips_removal(self):
        """When manifest has no [excluded] packages, removal is skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repos_dir = write_repos(tmp_path)
            manifest = write_manifest(tmp_path, fedora=["base-pkg"], excluded=[])
            overlay = write_overlay(tmp_path)

            code, commands, out = self.run_main(
                manifest,
                overlay,
                repos_dir,
                installed_excluded=[],
            )

            self.assertEqual(code, 0)
            self.assertEqual(len(commands), 2)
            self.assertIn("No excluded packages found to remove.", out)

    def test_nonzero_install_stops_before_mark_user_or_removal(self):
        """A failure in DNF install halts execution immediately."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repos_dir = write_repos(tmp_path)
            manifest = write_manifest(tmp_path, fedora=["base-pkg"], excluded=["exc-pkg"])
            overlay = write_overlay(tmp_path)

            code, commands, out = self.run_main(
                manifest,
                overlay,
                repos_dir,
                installed_excluded=["exc-pkg"],
                run_returncodes=[1],
            )

            self.assertEqual(code, 1)
            # Only install ran; mark user and remove were never called
            self.assertEqual(len(commands), 1)

    def test_nonzero_mark_user_stops_before_removal(self):
        """A failure in DNF mark user halts execution before excluded removal."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repos_dir = write_repos(tmp_path)
            manifest = write_manifest(tmp_path, fedora=["base-pkg"], excluded=["exc-pkg"])
            overlay = write_overlay(tmp_path)

            code, commands, out = self.run_main(
                manifest,
                overlay,
                repos_dir,
                installed_excluded=["exc-pkg"],
                run_returncodes=[0, 3],
            )

            self.assertEqual(code, 3)
            # Install and mark ran; remove was never called
            self.assertEqual(len(commands), 2)

    def test_nonzero_removal_returns_exit_code(self):
        """A failure in DNF removal propagates the non-zero exit code."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repos_dir = write_repos(tmp_path)
            manifest = write_manifest(tmp_path, fedora=["base-pkg"], excluded=["exc-pkg"])
            overlay = write_overlay(tmp_path)

            code, commands, out = self.run_main(
                manifest,
                overlay,
                repos_dir,
                installed_excluded=["exc-pkg"],
                run_returncodes=[0, 0, 5],
            )

            self.assertEqual(code, 5)
            self.assertEqual(len(commands), 3)

    def test_unavailable_packages_announced_in_build_log(self):
        """Packages marked [unavailable] are announced loudly rather than dropped silently."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repos_dir = write_repos(tmp_path)
            manifest = write_manifest(tmp_path, fedora=["base-pkg", "missing-pkg-1"])
            overlay = write_overlay(
                tmp_path,
                unavailable=["missing-pkg-1", "missing-pkg-2"],
            )

            code, commands, out = self.run_main(manifest, overlay, repos_dir)

            self.assertEqual(code, 0)
            self.assertIn(
                "NOTE: missing-pkg-1 has no source in Utah's repositories and is skipped (see packages/utah.toml)",
                out,
            )
            self.assertIn(
                "NOTE: missing-pkg-2 has no source in Utah's repositories and is skipped (see packages/utah.toml)",
                out,
            )

    def test_fedora_release_logged(self):
        """The detected Fedora release is printed to the build log."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repos_dir = write_repos(tmp_path)
            manifest = write_manifest(tmp_path, fedora=["base-pkg"])
            overlay = write_overlay(tmp_path)

            code, commands, out = self.run_main(
                manifest, overlay, repos_dir, major="45"
            )

            self.assertEqual(code, 0)
            self.assertIn("Fedora release is 45", out)

    def test_overlay_defaults_to_utah_toml_beside_manifest(self):
        """When overlay is omitted from arguments, it defaults to utah.toml beside the manifest."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repos_dir = write_repos(tmp_path)
            manifest = write_manifest(tmp_path, fedora=["base-pkg"])
            # Create utah.toml beside manifest
            write_overlay(tmp_path, gnome=["overlay-default-pkg"])

            code, commands, out = self.run_main(
                manifest, None, repos_dir  # overlay is None
            )

            self.assertEqual(code, 0)
            install_cmd = commands[0]
            self.assertIn("overlay-default-pkg", install_cmd)


if __name__ == "__main__":
    unittest.main()
