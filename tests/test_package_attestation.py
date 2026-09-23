"""Unit tests for package attestation, repository allowlist, and provenance reports."""

from __future__ import annotations

import configparser
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
        # The origin of a package is decided by the NVIDIA contract, not by the
        # text of its release: a rebuild tagged e.g. "1.nvidia_fix" is not an
        # NVIDIA-sourced package.
        self.assertEqual(
            verifier.determine_origin("pkg", "1.nvidia_fix.el9"), "unknown"
        )
        self.assertEqual(
            verifier.determine_origin("pkg", "1.nvidia_fix.fc44"), "fedora"
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
            list(installed.keys()), installed, major_versions, {"gnome-shell", "gtk4"}
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
            ["gnome-shell"], installed, {"gnome-shell": "51"}, {"gnome-shell"}
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
            {"gnome-shell"},
        )
        self.assertEqual(len(errors), 3)
        self.assertTrue(any("factory release identity (.bfin)" in e for e in errors))
        self.assertTrue(any("Hummingbird release identity (.hum)" in e for e in errors))
        self.assertTrue(any("unapproved Fedora release" in e for e in errors))

    def test_verify_gnome_contract_takes_the_split_from_the_manifest(self):
        """Which GNOME packages must be factory rebuilds is [factory], not code.

        The exception used to be the literal name glibc-all-langpacks. A package
        moving in or out of [factory] then needed a code edit to stay truthful,
        and until it got one the verifier asserted the wrong origin.
        """
        installed = {
            "gtk4": {
                "name": "gtk4",
                "epoch": "0",
                "version": "4.23.3",
                "release": "1.hum1",  # Hummingbird, not a factory rebuild
                "arch": "x86_64",
                "nevra": "gtk4-4.23.3-1.hum1.x86_64",
                "origin": "hummingbird",
            },
        }
        versions = {"gtk4": "4"}

        # Not named in [factory]: Hummingbird identity is what is required.
        self.assertEqual(
            verifier.verify_gnome_contract(["gtk4"], installed, versions, set()), []
        )

        # Named in [factory]: the same package must carry .bfin.
        errors = verifier.verify_gnome_contract(["gtk4"], installed, versions, {"gtk4"})
        self.assertEqual(len(errors), 1)
        self.assertIn("factory release identity (.bfin)", errors[0])

        # And a package the manifest leaves out of [factory] must not silently
        # arrive from the factory-less Fedora side either.
        installed["gtk4"]["release"] = "1.fc44"
        errors = verifier.verify_gnome_contract(["gtk4"], installed, versions, set())
        self.assertTrue(any("Hummingbird release identity (.hum)" in e for e in errors))
        self.assertTrue(any("unapproved Fedora release" in e for e in errors))

    def test_shipped_gnome_split_matches_the_shipped_manifest(self):
        """glibc-all-langpacks is the Hummingbird exception because [factory] says so."""
        overlay = ROOT / "packages" / "utah.toml"
        gnome = set(verifier.section(overlay, "gnome"))
        factory = set(verifier.section(overlay, "factory"))
        self.assertIn("glibc-all-langpacks", gnome)
        self.assertNotIn("glibc-all-langpacks", factory)
        self.assertTrue(gnome - {"glibc-all-langpacks"} <= factory)

    def test_shipped_factory_list_excludes_hummingbird_owned_sources(self):
        """A .bfin gate only holds for packages the factory actually builds.

        projectbluefin/utah-packages applies .bfin per build job, declares eight
        sources Hummingbird-owned in config/hummingbird-provided-sources.json,
        keeps no recipe for them and prunes them from its published repository.
        Listing one here would demand a release identity that cannot exist and
        fail every image build.
        """
        hummingbird_owned = {
            "bootc",
            "dracut",
            "firewalld",
            "gcc",
            "libxcrypt",
            "make",
            "openssh",
            "rust-bootupd",
        }
        factory = set(verifier.section(ROOT / "packages" / "utah.toml", "factory"))
        self.assertEqual(factory & hummingbird_owned, set())

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

    def test_runtime_policy_covers_dnf_conf_sections(self):
        """A repository declared in dnf.conf itself is still a runtime repository."""
        allowed = {"utah-packages"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "etc/dnf").mkdir(parents=True)
            (root / "etc/yum.repos.d").mkdir(parents=True)
            (root / "etc/yum.repos.d/utah-packages.repo").write_text(
                "[utah-packages]\nname=utah\nenabled=1\n"
            )
            (root / "etc/dnf/dnf.conf").write_text(
                "[main]\ngpgcheck=1\n\n[sneaky]\nname=sneaky\nenabled=1\n"
            )
            errors = verifier.verify_runtime_repository_policy(allowed, root=root)
            self.assertEqual(len(errors), 1)
            self.assertIn("Unapproved repository 'sneaky'", errors[0])
            self.assertIn("dnf.conf", errors[0])

            # [main] is DNF's own configuration, never a repository
            (root / "etc/dnf/dnf.conf").write_text("[main]\ngpgcheck=1\n")
            self.assertEqual(verifier.verify_runtime_repository_policy(allowed, root=root), [])

    def test_runtime_policy_follows_reposdir(self):
        """An alternate reposdir must be scanned; /etc/yum.repos.d alone is not the system."""
        allowed = {"utah-packages"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "etc/dnf").mkdir(parents=True)
            (root / "etc/yum.repos.d").mkdir(parents=True)
            (root / "opt/repos").mkdir(parents=True)
            (root / "opt/repos/extra.repo").write_text(
                "[unapproved-elsewhere]\nname=bad\nenabled=1\n"
            )
            (root / "etc/dnf/dnf.conf").write_text(
                "[main]\nreposdir=/etc/yum.repos.d,/opt/repos\n"
            )
            errors = verifier.verify_runtime_repository_policy(allowed, root=root)
            self.assertEqual(len(errors), 1)
            self.assertIn("Unapproved repository 'unapproved-elsewhere'", errors[0])

    def test_runtime_policy_defaults_without_dnf_conf(self):
        allowed = {"utah-packages"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "etc/yum.repos.d").mkdir(parents=True)
            (root / "etc/yum.repos.d/fedora.repo").write_text(
                "[fedora]\nname=Fedora\nenabled=1\n"
            )
            errors = verifier.verify_runtime_repository_policy(allowed, root=root)
            self.assertTrue(any("Fedora repository 'fedora' is enabled" in e for e in errors))

    def test_resolve_reposdirs_defaults_on_empty_value(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string("[main]\nreposdir=\n")
        defaults = [Path("/etc/yum.repos.d"), Path("/etc/distro.repos.d")]
        self.assertEqual(verifier.resolve_reposdirs(parser, defaults), defaults)
        self.assertEqual(verifier.resolve_reposdirs(None, defaults), defaults)

    def test_runtime_policy_covers_every_default_reposdir(self):
        """DNF's default reposdir is a list; attesting one directory is bypassable.

        A .repo file dropped in /etc/distro.repos.d is read by DNF exactly like
        one in /etc/yum.repos.d, so an unapproved repository placed there must
        fail the contract rather than pass unseen.
        """
        allowed = {"utah-packages"}
        for directory in verifier.DEFAULT_REPOSDIRS:
            with self.subTest(reposdir=directory), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / directory).mkdir(parents=True)
                (root / directory / "sneaky.repo").write_text(
                    "[unapproved-elsewhere]\nname=bad\nenabled=1\n"
                )
                errors = verifier.verify_runtime_repository_policy(allowed, root=root)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("Unapproved repository 'unapproved-elsewhere'", errors[0])

    def test_default_reposdirs_match_dnfs_own_defaults(self):
        self.assertEqual(
            verifier.DEFAULT_REPOSDIRS,
            ("etc/yum.repos.d", "etc/yum/repos.d", "etc/distro.repos.d"),
        )

    def test_an_explicit_reposdir_replaces_the_defaults(self):
        """DNF's semantics: an explicit reposdir= is the whole list, not an addition."""
        allowed = {"utah-packages"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "etc/dnf").mkdir(parents=True)
            (root / "etc/distro.repos.d").mkdir(parents=True)
            (root / "etc/distro.repos.d/ignored.repo").write_text(
                "[not-read-by-dnf]\nname=bad\nenabled=1\n"
            )
            (root / "etc/dnf/dnf.conf").write_text("[main]\nreposdir=/etc/yum.repos.d\n")
            self.assertEqual(
                verifier.verify_runtime_repository_policy(allowed, root=root), []
            )

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
