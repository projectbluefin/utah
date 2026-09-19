"""Unit tests for package attestation, repository allowlist, and provenance reports."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verifier = load_module("verify-rpm-contract")


class PackageAttestationTests(unittest.TestCase):
    def test_determine_origin(self):
        self.assertEqual(verifier.determine_origin("pkg", "1.hum1.bfin"), "factory")
        self.assertEqual(verifier.determine_origin("pkg", "3.hum1"), "hummingbird")
        self.assertEqual(
            verifier.determine_origin("nvidia-container-toolkit", "1.17.4-1"),
            "nvidia",
        )
        self.assertEqual(
            verifier.determine_origin("pkg", "1.fc44"), "fedora"
        )
        self.assertEqual(
            verifier.determine_origin("pkg", "1.el9"), "unknown"
        )

    def test_verify_gnome_contract_passes_valid_packages(self):
        installed = {
            "gnome-shell": {
                "name": "gnome-shell",
                "epoch": "0",
                "version": "51~beta",
                "release": "1.hum1.bfin",
                "arch": "x86_64",
                "nevra": "gnome-shell-51~beta-1.hum1.bfin.x86_64",
                "origin": "factory",
            },
            "gtk4": {
                "name": "gtk4",
                "epoch": "0",
                "version": "4.23.3",
                "release": "1.hum1.bfin",
                "arch": "x86_64",
                "nevra": "gtk4-4.23.3-1.hum1.bfin.x86_64",
                "origin": "factory",
            },
            "glibc-all-langpacks": {
                "name": "glibc-all-langpacks",
                "epoch": "0",
                "version": "2.41",
                "release": "1.hum1",
                "arch": "x86_64",
                "nevra": "glibc-all-langpacks-2.41-1.hum1.x86_64",
                "origin": "hummingbird",
            },
        }
        major_versions = {
            "gnome-shell": "51",
            "gtk4": "4",
            "glibc-all-langpacks": "2",
        }
        errors = verifier.verify_gnome_contract(
            list(installed.keys()), installed, major_versions
        )
        self.assertEqual(errors, [])

    def test_verify_gnome_contract_fails_wrong_major_version(self):
        installed = {
            "gnome-shell": {
                "name": "gnome-shell",
                "epoch": "0",
                "version": "50.1",
                "release": "1.hum1.bfin",
                "arch": "x86_64",
                "nevra": "gnome-shell-50.1-1.hum1.bfin.x86_64",
                "origin": "factory",
            },
        }
        errors = verifier.verify_gnome_contract(
            ["gnome-shell"], installed, {"gnome-shell": "51"}
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("does not match required major version '51'", errors[0])

    def test_verify_gnome_contract_fails_wrong_release_identity(self):
        installed = {
            "gnome-shell": {
                "name": "gnome-shell",
                "epoch": "0",
                "version": "51~beta",
                "release": "1.hum1",  # Missing .bfin factory identity
                "arch": "x86_64",
                "nevra": "gnome-shell-51~beta-1.hum1.x86_64",
                "origin": "hummingbird",
            },
            "glibc-all-langpacks": {
                "name": "glibc-all-langpacks",
                "epoch": "0",
                "version": "2.41",
                "release": "1.fc44",  # Fedora release, missing .hum
                "arch": "x86_64",
                "nevra": "glibc-all-langpacks-2.41-1.fc44.x86_64",
                "origin": "fedora",
            },
        }
        errors = verifier.verify_gnome_contract(
            ["gnome-shell", "glibc-all-langpacks"],
            installed,
            {"gnome-shell": "51", "glibc-all-langpacks": "2"},
        )
        self.assertEqual(len(errors), 3)
        self.assertTrue(any("factory release identity (.bfin)" in e for e in errors))
        self.assertTrue(any("Hummingbird release identity (.hum)" in e for e in errors))
        self.assertTrue(any("unapproved Fedora release" in e for e in errors))

    def test_verify_factory_parity_prevents_silent_resolution(self):
        installed = {
            "fastfetch": {
                "name": "fastfetch",
                "epoch": "0",
                "version": "2.66.0",
                "release": "2.hum1",  # Silently resolved from Hummingbird rather than factory
                "arch": "x86_64",
                "nevra": "fastfetch-2.66.0-2.hum1.x86_64",
                "origin": "hummingbird",
            },
            "distrobox": {
                "name": "distrobox",
                "epoch": "0",
                "version": "1.8.2.5",
                "release": "1.fc44",  # Silently resolved from Fedora
                "arch": "noarch",
                "nevra": "distrobox-1.8.2.5-1.fc44.noarch",
                "origin": "fedora",
            },
        }
        errors = verifier.verify_factory_parity(["fastfetch", "distrobox"], installed)
        self.assertEqual(len(errors), 3)
        self.assertTrue(any("fastfetch" in e and "expected from factory rebuild" in e for e in errors))
        self.assertTrue(any("distrobox" in e and "unapproved Fedora release" in e for e in errors))

    def test_verify_hummingbird_parity(self):
        installed = {
            "bash-completion": {
                "name": "bash-completion",
                "epoch": "1",
                "version": "2.16",
                "release": "1.hum1",
                "arch": "noarch",
                "nevra": "bash-completion-1:2.16-1.hum1.noarch",
                "origin": "hummingbird",
            },
            "less": {
                "name": "less",
                "epoch": "0",
                "version": "668",
                "release": "1.fc44",
                "arch": "x86_64",
                "nevra": "less-668-1.fc44.x86_64",
                "origin": "fedora",
            },
        }
        errors = verifier.verify_hummingbird_parity(["bash-completion", "less"], installed)
        self.assertEqual(len(errors), 1)
        self.assertTrue(any("less" in e and "unapproved Fedora release" in e for e in errors))

        # A non-factory package built in the factory (.bfin or .hum1.bfin) must pass
        installed_factory_override = {
            "bash-completion": {
                "name": "bash-completion",
                "epoch": "1",
                "version": "2.16",
                "release": "1.hum1.bfin",
                "arch": "noarch",
                "nevra": "bash-completion-1:2.16-1.hum1.bfin.noarch",
                "origin": "factory",
            },
        }
        self.assertEqual(verifier.verify_hummingbird_parity(["bash-completion"], installed_factory_override), [])

    def test_verify_repository_policy_allowlist(self):
        allowed = {"utah-packages", "public-hummingbird-x86_64-rpms"}
        with tempfile.TemporaryDirectory() as tmp:
            repos_dir = Path(tmp)
            (repos_dir / "hummingbird.repo").write_text(
                "[public-hummingbird-x86_64-rpms]\nname=hum\nenabled=1\n"
            )
            (repos_dir / "utah-packages.repo").write_text(
                "[utah-packages]\nname=utah\nenabled=True\n"
            )
            errors = verifier.verify_repository_policy(repos_dir, allowed)
            self.assertEqual(errors, [])

            # Adding an unapproved repo with enabled=true/yes must fail
            (repos_dir / "custom.repo").write_text(
                "[unapproved-repo]\nname=bad\nenabled=yes\nbaseurl=https://example.com/%20/repo\n"
            )
            errors = verifier.verify_repository_policy(repos_dir, allowed)
            self.assertEqual(len(errors), 1)
            self.assertIn("Unapproved repository 'unapproved-repo'", errors[0])

            # Adding Fedora repo must fail with specific Fedora message
            (repos_dir / "fedora.repo").write_text(
                "[fedora]\nname=Fedora Linux\nbaseurl=https://dl.fedoraproject.org/pub/fedora\nenabled=1\n"
            )
            errors = verifier.verify_repository_policy(repos_dir, allowed)
            self.assertTrue(any("Fedora repository 'fedora' is enabled" in e for e in errors))

    def test_generate_provenance_report(self):
        installed = {
            "gnome-shell": {
                "name": "gnome-shell",
                "epoch": "0",
                "version": "51~beta",
                "release": "1.hum1.bfin",
                "arch": "x86_64",
                "nevra": "gnome-shell-51~beta-1.hum1.bfin.x86_64",
                "origin": "factory",
            },
            "glibc-all-langpacks": {
                "name": "glibc-all-langpacks",
                "epoch": "0",
                "version": "2.41",
                "release": "1.hum1",
                "arch": "x86_64",
                "nevra": "glibc-all-langpacks-2.41-1.hum1.x86_64",
                "origin": "hummingbird",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            with patch.dict("os.environ", {"SOURCE_DATE_EPOCH": "1726000000"}):
                report = verifier.generate_provenance_report(
                    installed,
                    flavor="main",
                    allowed_repos={"utah-packages", "public-hummingbird-x86_64-rpms"},
                    package_sections={"gnome-shell": "gnome", "glibc-all-langpacks": "gnome"},
                    output_dir=out_dir,
                )
            self.assertTrue((out_dir / "package-origins.json").exists())
            self.assertTrue((out_dir / "package-origins.txt").exists())
            self.assertEqual(report["build_provenance"]["contract_packages"], 2)
            self.assertEqual(report["build_provenance"]["factory_packages_count"], 1)
            self.assertEqual(report["build_provenance"]["hummingbird_packages_count"], 1)
            self.assertEqual(report["build_provenance"]["timestamp"], "2024-09-10T20:26:40+00:00")
            self.assertIn("gnome-shell", report["packages"])
            self.assertEqual(report["packages"]["gnome-shell"]["nevra"], "gnome-shell-51~beta-1.hum1.bfin.x86_64")

    def test_check_mode_validates_repo_policy_and_manifests(self):
        with patch("sys.argv", ["verify-rpm-contract", "--check", str(ROOT / "packages/bluefin.toml")]):
            rc = verifier.main()
            self.assertEqual(rc, 0)

    def test_main_fails_loudly_on_missing_manifest_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_overlay = Path(tmp) / "utah.toml"
            bad_overlay.write_text("[gnome]\npackages = []\n")
            with patch("sys.argv", ["verify-rpm-contract", "--check", str(ROOT / "packages/bluefin.toml"), str(bad_overlay)]):
                rc = verifier.main()
                self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
