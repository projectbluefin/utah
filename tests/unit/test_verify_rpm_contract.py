#!/usr/bin/env python3
"""Unit tests for scripts/verify-rpm-contract.py supply-chain assertions."""

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "verify-rpm-contract.py"

spec = importlib.util.spec_from_file_location("verify_rpm_contract", SCRIPT_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class TestVerifyRpmContract(unittest.TestCase):
    def test_verify_gnome_packages_success(self):
        gnome_pkgs = ["gnome-shell", "gtk4"]
        pkg_map = {
            "gnome-shell": {
                "name": "gnome-shell",
                "epoch": "0",
                "version": "51~beta",
                "release": "1.hum1.bfin",
                "arch": "x86_64",
                "sourcerpm": "gnome-shell-51~beta-1.hum1.bfin.src.rpm",
                "vendor": "projectbluefin",
                "nevra": "gnome-shell-0:51~beta-1.hum1.bfin.x86_64",
            },
            "gtk4": {
                "name": "gtk4",
                "epoch": "0",
                "version": "4.23.3",
                "release": "1.hum1.bfin",
                "arch": "x86_64",
                "sourcerpm": "gtk4-4.23.3-1.hum1.bfin.src.rpm",
                "vendor": "projectbluefin",
                "nevra": "gtk4-0:4.23.3-1.hum1.bfin.x86_64",
            },
        }
        majors = {"gnome-shell": "51", "gtk4": "4"}
        errors = mod.verify_gnome_packages(gnome_pkgs, pkg_map, majors)
        self.assertEqual(errors, [])

    def test_verify_gnome_packages_version_mismatch(self):
        gnome_pkgs = ["gnome-shell"]
        pkg_map = {
            "gnome-shell": {
                "name": "gnome-shell",
                "epoch": "0",
                "version": "50.1",
                "release": "1.hum1.bfin",
                "arch": "x86_64",
                "sourcerpm": "gnome-shell-50.1-1.hum1.bfin.src.rpm",
                "vendor": "projectbluefin",
                "nevra": "gnome-shell-0:50.1-1.hum1.bfin.x86_64",
            }
        }
        majors = {"gnome-shell": "51"}
        errors = mod.verify_gnome_packages(gnome_pkgs, pkg_map, majors)
        self.assertEqual(len(errors), 1)
        self.assertIn("does not match required major version '51'", errors[0])

    def test_verify_gnome_packages_unapproved_release(self):
        gnome_pkgs = ["gnome-shell"]
        pkg_map = {
            "gnome-shell": {
                "name": "gnome-shell",
                "epoch": "0",
                "version": "51~beta",
                "release": "1.fc44",
                "arch": "x86_64",
                "sourcerpm": "gnome-shell-51~beta-1.fc44.src.rpm",
                "vendor": "Fedora Project",
                "nevra": "gnome-shell-0:51~beta-1.fc44.x86_64",
            }
        }
        majors = {"gnome-shell": "51"}
        errors = mod.verify_gnome_packages(gnome_pkgs, pkg_map, majors)
        self.assertTrue(any("Fedora release identity" in err for err in errors))

    def test_verify_factory_packages_success(self):
        factory_pkgs = ["fastfetch", "just"]
        pkg_map = {
            "fastfetch": {
                "name": "fastfetch",
                "epoch": "0",
                "version": "2.21.3",
                "release": "1.hum1.bfin",
                "arch": "x86_64",
                "sourcerpm": "fastfetch-2.21.3-1.hum1.bfin.src.rpm",
                "vendor": "projectbluefin",
                "nevra": "fastfetch-0:2.21.3-1.hum1.bfin.x86_64",
            },
            "just": {
                "name": "just",
                "epoch": "0",
                "version": "1.35.0",
                "release": "1.hum1.bfin",
                "arch": "x86_64",
                "sourcerpm": "just-1.35.0-1.hum1.bfin.src.rpm",
                "vendor": "projectbluefin",
                "nevra": "just-0:1.35.0-1.hum1.bfin.x86_64",
            },
        }
        errors = mod.verify_factory_packages(factory_pkgs, pkg_map)
        self.assertEqual(errors, [])

    def test_verify_factory_packages_silent_fallback(self):
        factory_pkgs = ["fastfetch"]
        # Package resolved from Hummingbird base without .bfin rebuild
        pkg_map = {
            "fastfetch": {
                "name": "fastfetch",
                "epoch": "0",
                "version": "2.20.0",
                "release": "1.hum1",
                "arch": "x86_64",
                "sourcerpm": "fastfetch-2.20.0-1.hum1.src.rpm",
                "vendor": "Red Hat",
                "nevra": "fastfetch-0:2.20.0-1.hum1.x86_64",
            }
        }
        errors = mod.verify_factory_packages(factory_pkgs, pkg_map)
        self.assertEqual(len(errors), 1)
        self.assertIn("silent repository fallback", errors[0])

    def test_verify_repository_allowlist_allowed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repos_dir = Path(tmpdir)
            (repos_dir / "hummingbird.repo").write_text(
                "[public-hummingbird-x86_64-rpms]\nname=hummingbird\nenabled=1\n"
            )
            (repos_dir / "utah-packages.repo").write_text(
                "[utah-packages]\nname=factory\nenabled=1\n"
            )
            (repos_dir / "fedora.repo").write_text(
                "[fedora]\nname=fedora\nenabled=0\n"
            )
            allowlist = {"utah-packages", "public-hummingbird-x86_64-rpms", "nvidia-container-toolkit"}
            errors, enabled = mod.verify_repository_allowlist(allowlist, repos_dir=repos_dir)
            self.assertEqual(errors, [])
            self.assertEqual(sorted(enabled), ["public-hummingbird-x86_64-rpms", "utah-packages"])

    def test_verify_repository_allowlist_fedora_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repos_dir = Path(tmpdir)
            (repos_dir / "fedora.repo").write_text(
                "[fedora]\nname=fedora\nenabled=1\n"
            )
            allowlist = {"utah-packages", "public-hummingbird-x86_64-rpms"}
            errors, enabled = mod.verify_repository_allowlist(allowlist, repos_dir=repos_dir)
            self.assertEqual(len(errors), 1)
            self.assertIn("Fedora repository 'fedora' is enabled", errors[0])

    def test_verify_repository_allowlist_unapproved_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repos_dir = Path(tmpdir)
            (repos_dir / "random.repo").write_text(
                "[unapproved-custom-repo]\nname=random\nenabled=1\n"
            )
            allowlist = {"utah-packages", "public-hummingbird-x86_64-rpms"}
            errors, enabled = mod.verify_repository_allowlist(allowlist, repos_dir=repos_dir)
            self.assertEqual(len(errors), 1)
            self.assertIn("unapproved repository 'unapproved-custom-repo' is enabled", errors[0])

    def test_retain_provenance_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = Path(tmpdir) / "usr" / "share" / "utah" / "package-provenance.json"
            pkg_map = {
                "gnome-shell": {
                    "name": "gnome-shell",
                    "epoch": "0",
                    "version": "51~beta",
                    "release": "1.hum1.bfin",
                    "arch": "x86_64",
                    "sourcerpm": "gnome-shell-51~beta-1.hum1.bfin.src.rpm",
                    "vendor": "projectbluefin",
                    "nevra": "gnome-shell-0:51~beta-1.hum1.bfin.x86_64",
                },
                "bootc": {
                    "name": "bootc",
                    "epoch": "0",
                    "version": "1.1.0",
                    "release": "1.hum1",
                    "arch": "x86_64",
                    "sourcerpm": "bootc-1.1.0-1.hum1.src.rpm",
                    "vendor": "Red Hat",
                    "nevra": "bootc-0:1.1.0-1.hum1.x86_64",
                },
            }
            mod.retain_provenance_report(
                pkg_map=pkg_map,
                expected_categories={"gnome-shell": "gnome", "bootc": "bluefin"},
                factory_pkgs={"gnome-shell"},
                gnome_majors={"gnome-shell": "51"},
                enabled_repos=["utah-packages", "public-hummingbird-x86_64-rpms"],
                allowlist={"utah-packages", "public-hummingbird-x86_64-rpms"},
                flavor="main",
                output_path=out_file,
            )
            self.assertTrue(out_file.exists())
            data = json.loads(out_file.read_text())
            self.assertEqual(data["report_version"], "1.0")
            self.assertEqual(data["image_flavor"], "main")
            self.assertEqual(data["summary"]["total_contract_packages"], 2)
            self.assertEqual(data["summary"]["factory_packages"], 1)
            self.assertEqual(data["summary"]["hummingbird_packages"], 1)
            self.assertEqual(len(data["packages"]), 2)
            origins = {p["name"]: p["origin"] for p in data["packages"]}
            self.assertEqual(origins["gnome-shell"], "factory")
            self.assertEqual(origins["bootc"], "hummingbird")

    def test_cli_check_mode(self):
        res = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--check", str(REPO_ROOT / "packages" / "bluefin.toml")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, f"CLI check failed: {res.stderr}")
        self.assertIn("RPM contract syntax and supply-chain policy is valid", res.stdout)


class TestDeclaredReleasePatterns(unittest.TestCase):
    """The patterns as `main()` actually reads them, not as the defaults spell them.

    The gap this closes: every other factory/Hummingbird test calls the verify
    helpers positionally, which takes the Python default -- while production
    takes the value out of `packages/utah.toml`. A suite green on a regex the
    program does not use cannot see a TOML edit at all, which makes the whole
    "a dist-tag convention change is a data edit" claim unverified.

    So these load the committed TOML and assert on what it declares.
    """

    @staticmethod
    def _declared() -> dict:
        """Return the [supply_chain] block exactly as main() reads it."""

        import tomllib

        with (REPO_ROOT / "packages" / "utah.toml").open("rb") as handle:
            return tomllib.load(handle).get("supply_chain", {})

    def test_declared_patterns_are_present_and_not_catch_alls(self):
        """A typo that matches everything disables the gate while looking set.

        `re.search` on a malformed-but-valid pattern fails OPEN: it matches, so
        every package passes and no error is ever raised. Asserting the
        patterns REJECT something is what separates a live gate from a
        decorative one.
        """

        declared = self._declared()
        for key in ("factory_release_pattern", "hummingbird_release_pattern"):
            pattern = declared.get(key)
            self.assertIsNotNone(pattern, f"{key} is not declared in utah.toml")
            self.assertIsNone(
                re.search(pattern, "1.fc44"),
                f"{key} matches a plain Fedora release, so it gates nothing",
            )

    def test_declared_factory_pattern_accepts_current_artifacts(self):
        """Today's factory releases, and the bare `.bfin` form, both pass."""

        pattern = self._declared()["factory_release_pattern"]
        for release in ("1.hum1.bfin", "1.hum1.bfin.fc44", "1.bfin", "1.bfin.fc44"):
            self.assertIsNotNone(
                re.search(pattern, release),
                f"factory pattern rejects {release!r}; `.bfin` is the factory "
                "marker, and requiring `.hum<N>` beside it reports a genuine "
                "factory build as a silent repository fallback",
            )

    def test_declared_factory_pattern_rejects_base_repo_releases(self):
        """A package that resolved from a base repo is what the gate is for."""

        pattern = self._declared()["factory_release_pattern"]
        for release in ("1.hum1", "1.fc44", "2.el9"):
            self.assertIsNone(
                re.search(pattern, release),
                f"factory pattern accepts {release!r}, which carries no .bfin",
            )

    def test_declared_patterns_agree_with_the_python_defaults(self):
        """The wired value and the fallback must classify releases identically.

        Not string equality -- two spellings of the same rule are fine. What is
        not fine is the two disagreeing about a release, because then the
        answer depends on whether the TOML was readable, and a test calling the
        helper positionally proves nothing about production.
        """

        declared = self._declared()
        releases = [
            "1.hum1.bfin",
            "1.hum1.bfin.fc44",
            "1.bfin",
            "1.bfin.fc44",
            "1.fc44.bfin",
            "1.hum1",
            "1.hum2",
            "1.fc44",
            "2.el9",
        ]
        pairs = (
            ("factory_release_pattern", mod.DEFAULT_FACTORY_RELEASE_PATTERN),
            ("hummingbird_release_pattern", mod.DEFAULT_HUMMINGBIRD_RELEASE_PATTERN),
        )
        for key, default in pairs:
            for release in releases:
                self.assertEqual(
                    bool(re.search(declared[key], release)),
                    bool(re.search(default, release)),
                    f"{key} and its Python default disagree about {release!r}",
                )

    def test_factory_verification_uses_the_declared_pattern(self):
        """The wired path, end to end, on a release only one pattern accepts."""

        pattern = self._declared()["factory_release_pattern"]
        pkg_map = {
            "fastfetch": {
                "name": "fastfetch",
                "epoch": "0",
                "version": "2.21.3",
                "release": "1.bfin",
                "arch": "x86_64",
                "sourcerpm": "fastfetch-2.21.3-1.bfin.src.rpm",
                "vendor": "projectbluefin",
                "nevra": "fastfetch-0:2.21.3-1.bfin.x86_64",
            }
        }
        self.assertEqual(
            mod.verify_factory_packages(["fastfetch"], pkg_map, pattern),
            [],
            "a factory RPM tagged .bfin without .hum<N> is a genuine factory "
            "build and must not be reported as a repository fallback",
        )


if __name__ == "__main__":
    unittest.main()
