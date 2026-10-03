"""Tests for scripts/verify-rpm-contract.py.

The verifier is the gate that proves a built image actually contains the RPM
contract Utah promises: Bluefin's Fedora set, the GNOME desktop packages, the
parity packages, the desktop services, and -- on NVIDIA flavors -- the NVIDIA
userspace. Until now the only test that named it read the file as text and
grepped for two substrings, which passes whether or not the code runs.

These tests execute it. They cover both sources the expected set can come from
(the resolved contract install-packages.py writes, and the off-image manifest
fallback), the filters applied to each, the duplicate-name assertion behind
`--check`, and the missing-package report.
"""

from __future__ import annotations

import configparser
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify-rpm-contract.py"

# The path the script consults before falling back to the manifest. Tests that
# exercise that branch redirect it into a temporary directory.
RESOLVED_CONTRACT = "/usr/share/utah/contract.txt"


def load_module():
    """Import the script by path; its filename is not a valid module name."""
    spec = importlib.util.spec_from_file_location("verify_rpm_contract", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def toml_section(name: str, packages: list[str]) -> str:
    listing = ", ".join(f'"{pkg}"' for pkg in packages)
    return f"[{name}]\npackages = [{listing}]\n"


def write_manifest(directory: Path, fedora: list[str]) -> Path:
    path = directory / "bluefin.toml"
    path.write_text(toml_section("fedora", fedora))
    return path


def write_repo_file(directory: Path, repo_id: str, **fields) -> Path:
    """Write a single .repo file with one section, like Fedora's repos.

    `directory` is created if it does not exist. `enabled` defaults to "1".
    """
    directory.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser(interpolation=None)
    parser[repo_id] = {"enabled": "1", **fields}
    path = directory / f"{repo_id}.repo"
    with path.open("w") as handle:
        parser.write(handle)
    return path


def write_overlay(
    directory: Path,
    *,
    gnome: list[str] | None = None,
    parity: list[str] | None = None,
    hardware: list[str] | None = None,
    services: list[str] | None = None,
    unavailable: list[str] | None = None,
    gnome_versions: dict[str, str] | None = None,
    repositories: list[str] | None = None,
    baseurls: dict[str, str] | None = None,
    factory: list[str] | None = None,
) -> Path:
    """Write a utah.toml overlay that already carries the supply-chain sections.

    The contract verifier requires [gnome.versions] and [repositories.allowed];
    every overlay the tests build therefore gets those plus a matching
    [repositories.baseurls]. gnome_versions defaults to major "51" for each
    desktop package, with gtk4/libadwaita pinned to their own majors, so every
    GNOME package in the overlay is version-checked unless a test overrides it.
    """
    sections = [
        toml_section("gnome", gnome or []),
        toml_section("parity", parity or []),
        toml_section("hardware", hardware or []),
        toml_section("services", services or []),
        toml_section("unavailable", unavailable or []),
    ]
    own_majors = {"gtk4": "4", "libadwaita": "1"}
    versions = gnome_versions or {
        pkg: own_majors.get(pkg, "51") for pkg in (gnome or [])
    }
    sections.append("[gnome.versions]\n")
    for name, major in versions.items():
        sections.append(f'{name} = "{major}"\n')
    allowed = repositories or ["public-hummingbird-x86_64-rpms"]
    sections.append("[repositories]\nallowed = [" + ", ".join(f'"{r}"' for r in allowed) + "]\n")
    url_map = baseurls or {
        "public-hummingbird-x86_64-rpms":
            "https://packages.redhat.com/api/pulp-content/public-hummingbird/x86_64/"
    }
    sections.append("[repositories.baseurls]\n")
    for repo_id, url in url_map.items():
        sections.append(f'{repo_id} = ["{url}"]\n')
    for repo_id in allowed:
        sections.append(f'[repositories.security."{repo_id}"]\n')
        sections.append('gpgcheck = "0"\nrepo_gpgcheck = "0"\nsslverify = "1"\nproxy = ""\n')
    if factory:
        sections.append(toml_section("factory", factory))
    path = directory / "utah.toml"
    path.write_text("".join(sections))
    return path


def run_main(module, manifest: Path, overlay: Path, installed: set[str],
             *, flavor: str = "main",
             releases: dict[str, str] | None = None,
             multilib: set[str] | None = None,
             extra_argv: list[str] | None = None,
             report_dir: Path | None = None,
             runtime_repos_dir: Path | None = None,
             stderr_buffer: io.StringIO | None = None) -> tuple[int, str, str]:
    """Invoke scripts/verify-rpm-contract.py's main() with stubbed packages.

    Returns (exit_code, stdout, stderr). Shared by VerifyModeTests and
    OnImageRepoAllowlistTests so neither class reimplements the patch graph;
    `VerifyModeTests` discards the third tuple element and lets the test's own
    `sys.stderr` patch (when present) win by leaving stderr unwrapped here.
    """
    argv = ["verify-rpm-contract.py", *(extra_argv or []), str(manifest), str(overlay)]
    stdout = io.StringIO()
    overlay_data = tomllib.loads(overlay.read_text())
    gnome_versions = overlay_data.get("gnome", {}).get("versions", {})
    factory = set(overlay_data.get("factory", {}).get("packages", []))
    releases = releases or {}
    multilib = multilib or set()

    def fake_query(packages):
        result = {}
        for pkg in packages:
            if pkg not in installed:
                continue
            major = gnome_versions.get(pkg, "1")
            default_release = "1.bfin.x86_64" if pkg in factory else "1.hum.x86_64"
            release = releases.get(pkg, default_release)
            version = f"{major}.0"
            info = {
                "name": pkg, "epoch": "0", "version": version,
                "release": release, "arch": "x86_64",
                "nevra": f"{pkg}-{version}-{release}",
                "origin": "factory" if ".bfin" in release else (
                    "hummingbird" if ".hum" in release else "fedora"),
            }
            if pkg in multilib:
                i686_release = release.replace("x86_64", "i686")
                other = {**info, "arch": "i686", "release": i686_release,
                         "nevra": f"{pkg}-{version}-{i686_release}"}
                info = {**info, "installs": [dict(info), other]}
            result[pkg] = info
        return result, []

    report_dir = report_dir or Path(tempfile.mkdtemp())
    runtime_repos = runtime_repos_dir if runtime_repos_dir is not None else Path(
        tempfile.mkdtemp()
    )
    runtime_root = Path(tempfile.mkdtemp())
    runtime_path = runtime_root / "etc/yum.repos.d"
    runtime_path.parent.mkdir(parents=True)
    runtime_repos.mkdir(parents=True, exist_ok=True)
    runtime_path.symlink_to(runtime_repos, target_is_directory=True)
    stderr_text = ""
    base_patches = [
        patch.object(module, "is_installed",
                     side_effect=lambda p: p in installed),
        patch.object(module, "query_packages", side_effect=fake_query),
        patch.object(module, "RUNTIME_REPOS_DIR", runtime_repos),
        patch.object(module, "RUNTIME_ROOT", runtime_root),
        patch.object(sys, "argv", argv),
        patch.dict(os.environ, {"IMAGE_FLAVOR": flavor, "UTAH_REPORT_DIR": str(report_dir)}),
        redirect_stdout(stdout),
    ]
    if stderr_buffer is not None:
        base_patches.append(patch.object(sys, "stderr", stderr_buffer))
    with ExitStack() as stack:
        for patcher in base_patches:
            stack.enter_context(patcher)
        code = module.main()
    if stderr_buffer is not None:
        stderr_text = stderr_buffer.getvalue()
    return code, stdout.getvalue(), stderr_text


class SectionTests(unittest.TestCase):
    """section() is the single reader for every package list in the contract."""

    def setUp(self) -> None:
        self.module = load_module()

    def test_returns_the_declared_packages_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "packages.toml"
            path.write_text(toml_section("gnome", ["gnome-shell", "mutter"]))
            self.assertEqual(
                self.module.section(path, "gnome"), ["gnome-shell", "mutter"]
            )

    def test_absent_section_is_empty_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "packages.toml"
            path.write_text(toml_section("gnome", ["gnome-shell"]))
            self.assertEqual(self.module.section(path, "parity"), [])

    def test_section_without_a_packages_key_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "packages.toml"
            path.write_text("[parity]\nnote = \"nothing here yet\"\n")
            self.assertEqual(self.module.section(path, "parity"), [])


class IsInstalledTests(unittest.TestCase):
    """is_installed() must report on rpm's exit status, not on its output."""

    def setUp(self) -> None:
        self.module = load_module()

    def test_zero_exit_means_installed(self) -> None:
        with patch.object(
            self.module.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as run:
            self.assertTrue(self.module.is_installed("gnome-shell"))
        self.assertEqual(run.call_args.args[0], ["rpm", "-q", "gnome-shell"])

    def test_non_zero_exit_means_missing(self) -> None:
        with patch.object(
            self.module.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 1),
        ):
            self.assertFalse(self.module.is_installed("not-a-package"))


class CheckModeTests(unittest.TestCase):
    """`--check` runs off-image: it composes the expected set and validates it."""

    def run_check(self, manifest: Path, overlay: Path, flavor: str = "main"):
        env = {**os.environ, "IMAGE_FLAVOR": flavor}
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--check", str(manifest), str(overlay)],
            capture_output=True, text=True, env=env, cwd=str(ROOT),
        )

    def test_shipped_contract_passes(self) -> None:
        """The contract this repository actually ships must satisfy its own gate."""
        result = self.run_check(
            ROOT / "packages" / "bluefin.toml", ROOT / "packages" / "utah.toml"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_overlay_defaults_to_utah_toml_beside_the_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            write_overlay(directory, gnome=["gnome-shell"])
            env = {**os.environ, "IMAGE_FLAVOR": "main"}
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--check", str(manifest)],
                capture_output=True, text=True, env=env, cwd=str(ROOT),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 GNOME desktop packages", result.stdout)

    def test_unavailable_packages_are_dropped_from_the_fedora_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash", "ptyxis", "coreutils"])
            overlay = write_overlay(directory, unavailable=["ptyxis"])
            result = self.run_check(manifest, overlay)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Verifying 2 Bluefin packages", result.stdout)

    def test_every_overlay_section_is_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(
                directory,
                gnome=["gnome-shell", "mutter"],
                parity=["fastfetch", "gh", "just"],
                hardware=["linux-firmware", "iwlwifi-dvm-firmware"],
                services=["tailscale"],
            )
            result = self.run_check(manifest, overlay)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Verifying 1 Bluefin packages", result.stdout)
        self.assertIn("2 GNOME desktop packages", result.stdout)
        self.assertIn("3 parity packages", result.stdout)
        self.assertIn("2 firmware packages", result.stdout)
        self.assertIn("1 desktop service packages", result.stdout)

    def test_nvidia_packages_are_added_only_on_an_nvidia_flavor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(directory)
            plain = self.run_check(manifest, overlay, flavor="main")
            nvidia = self.run_check(manifest, overlay, flavor="nvidia")
            gaming = self.run_check(manifest, overlay, flavor="nvidia-gaming")
        self.assertIn("0 NVIDIA packages", plain.stdout)
        self.assertIn("1 NVIDIA packages", nvidia.stdout)
        self.assertIn("1 NVIDIA packages", gaming.stdout)

    def test_factory_package_outside_gnome_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(directory, hardware=["linux-firmware"], factory=["linux-firmware"])
            result = self.run_check(manifest, overlay)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Factory package 'linux-firmware'", result.stderr)
        self.assertIn("[gnome]", result.stderr)

    def test_a_duplicate_across_sections_is_rejected(self) -> None:
        """A package listed twice would be verified twice and counted twice."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash", "fastfetch"])
            overlay = write_overlay(directory, parity=["fastfetch"])
            result = self.run_check(manifest, overlay)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate package names", result.stderr)

    def test_check_mode_never_consults_rpm(self) -> None:
        """--check asserts nothing about installation, so it must not query rpm."""
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["definitely-not-installed"])
            overlay = write_overlay(directory)
            argv = ["verify-rpm-contract.py", "--check", str(manifest), str(overlay)]
            with patch.object(module, "is_installed") as is_installed, \
                    patch.object(sys, "argv", argv), \
                    patch.dict(os.environ, {"IMAGE_FLAVOR": "main"}), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(), 0)
        is_installed.assert_not_called()


class VerifyModeTests(unittest.TestCase):
    """Without --check the verifier asserts the packages are really installed."""

    def setUp(self) -> None:
        self.module = load_module()

    def run_main(self, manifest: Path, overlay: Path, installed: set[str],
                 flavor: str = "main", releases: dict[str, str] | None = None,
                 multilib: set[str] | None = None,
                 extra_argv: list[str] | None = None,
                 report_dir: Path | None = None,
                 runtime_repos_dir: Path | None = None) -> tuple[int, str]:
        code, out, _ = run_main(
            self.module, manifest, overlay, installed,
            flavor=flavor, releases=releases, multilib=multilib,
            extra_argv=extra_argv, report_dir=report_dir,
            runtime_repos_dir=runtime_repos_dir,
        )
        return code, out

    def test_a_fully_installed_contract_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(directory, gnome=["gnome-shell"])
            code, out = self.run_main(manifest, overlay, {"bash", "gnome-shell"})
        self.assertEqual(code, 0)
        self.assertIn("All 2 contract packages are present.", out)

    def test_missing_packages_fail_and_are_each_named_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash", "coreutils"])
            overlay = write_overlay(directory, parity=["fastfetch"])
            stderr = io.StringIO()
            with patch.object(sys, "stderr", stderr):
                code, _ = self.run_main(manifest, overlay, {"bash"})
        self.assertEqual(code, 1)
        report = stderr.getvalue()
        self.assertIn("2 of 3 contract packages are not installed", report)
        self.assertEqual(report.count("  - coreutils\n"), 1)
        self.assertEqual(report.count("  - fastfetch\n"), 1)
        self.assertNotIn("  - bash\n", report)

    def test_an_unavailable_package_is_not_required_to_be_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash", "ptyxis"])
            overlay = write_overlay(directory, unavailable=["ptyxis"])
            code, out = self.run_main(manifest, overlay, {"bash"})
        self.assertEqual(code, 0)
        self.assertIn("All 1 contract packages are present.", out)

    def test_nvidia_flavor_requires_the_nvidia_container_toolkit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(directory)
            stderr = io.StringIO()
            with patch.object(sys, "stderr", stderr):
                code, _ = self.run_main(manifest, overlay, {"bash"}, flavor="nvidia")
        self.assertEqual(code, 1)
        self.assertIn("  - nvidia-container-toolkit\n", stderr.getvalue())

    def test_an_attestation_violation_fails_the_run(self) -> None:
        """A bare Fedora GNOME package exits non-zero through main(), not just the helper."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(directory, gnome=["gnome-shell"])
            report_dir = Path(tmp) / "report"
            stderr = io.StringIO()
            with patch.object(sys, "stderr", stderr):
                code, _ = self.run_main(
                    manifest, overlay, {"bash", "gnome-shell"},
                    releases={"gnome-shell": "1.fc44.x86_64"},
                    report_dir=report_dir,
                )
            self.assertFalse((report_dir / "package-origins.json").exists())
        self.assertEqual(code, 1)
        report = stderr.getvalue()
        self.assertIn("supply-chain / repository contract violation(s)", report)
        self.assertIn("unapproved Fedora release", report)

    def test_no_report_verifies_without_retaining_the_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(directory, gnome=["gnome-shell"])
            report_dir = Path(tmp) / "report"
            code, out = self.run_main(
                manifest, overlay, {"bash", "gnome-shell"},
                extra_argv=["--no-report"], report_dir=report_dir,
            )
            self.assertFalse(report_dir.exists())
        self.assertEqual(code, 0)
        self.assertIn("Skipped provenance report retention (--no-report).", out)

    def test_the_retained_report_records_every_multilib_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["mesa-dri-drivers"])
            overlay = write_overlay(directory)
            report_dir = Path(tmp) / "report"
            code, out = self.run_main(
                manifest, overlay, {"mesa-dri-drivers"},
                multilib={"mesa-dri-drivers"}, report_dir=report_dir,
            )
            data = json.loads((report_dir / "package-origins.json").read_text())
            text = (report_dir / "package-origins.txt").read_text()
        self.assertEqual(code, 0)
        self.assertIn("Retained provenance report for 1 packages", out)
        self.assertEqual(
            [i["arch"] for i in data["packages"]["mesa-dri-drivers"]["installs"]],
            ["x86_64", "i686"],
        )
        self.assertEqual(text.count("mesa-dri-drivers-1.0-1.hum.x86_64"), 1)
        self.assertEqual(text.count("mesa-dri-drivers-1.0-1.hum.i686"), 1)


class ResolvedContractTests(unittest.TestCase):
    """The set install-packages.py resolved wins over recomputing the manifest.

    Recomputing it here is what let install and verify drift once: install grew
    a `[fedora_v<major>]` section for the running release and this check did
    not, so a contract package was installed and never verified.
    """

    def setUp(self) -> None:
        self.module = load_module()

    def run_main_with_contract(self, contract: str, manifest: Path, overlay: Path,
                               installed: set[str]) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            resolved = Path(tmp) / "contract.txt"
            resolved.write_text(contract)
            real_path = self.module.Path

            def redirected(arg, *rest):
                if str(arg) == RESOLVED_CONTRACT:
                    return real_path(resolved)
                return real_path(arg, *rest)

            argv = ["verify-rpm-contract.py", str(manifest), str(overlay)]
            stdout = io.StringIO()
            overlay_data = tomllib.loads(overlay.read_text())
            gnome_versions = overlay_data.get("gnome", {}).get("versions", {})
            factory = set(overlay_data.get("factory", {}).get("packages", []))

            def fake_query(packages):
                result = {}
                for pkg in packages:
                    if pkg not in installed:
                        continue
                    major = gnome_versions.get(pkg, "1")
                    release = "1.bfin.x86_64" if pkg in factory else "1.hum.x86_64"
                    version = f"{major}.0"
                    result[pkg] = {
                        "name": pkg, "epoch": "0", "version": version,
                        "release": release, "arch": "x86_64",
                        "nevra": f"{pkg}-{version}-{release}",
                        "origin": "factory" if pkg in factory else "hummingbird",
                    }
                return result, []

            report_dir = Path(tempfile.mkdtemp())
            # /etc/yum.repos.d is real on Fedora hosts. The resolved contract
            # tests don't care about it, so redirect it to a guaranteed-empty
            # temp directory (#454 on-image scan).
            runtime_repos = Path(tempfile.mkdtemp())
            with patch.object(self.module, "Path", redirected), \
                    patch.object(self.module, "RUNTIME_REPOS_DIR", runtime_repos), \
                    patch.object(self.module, "is_installed",
                                 side_effect=lambda p: p in installed), \
                    patch.object(self.module, "query_packages", side_effect=fake_query), \
                    patch.object(sys, "argv", argv), \
                    patch.dict(os.environ, {"IMAGE_FLAVOR": "main", "UTAH_REPORT_DIR": str(report_dir)}), \
                    redirect_stdout(stdout):
                code = self.module.main()
        return code, stdout.getvalue()

    def test_resolved_contract_supersedes_the_manifest(self) -> None:
        """A package install resolved but the manifest never listed is verified."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(directory)
            with patch.object(sys, "stderr", io.StringIO()):
                code, out = self.run_main_with_contract(
                    "bash\nresolved-only\n", manifest, overlay, {"bash"}
                )
        self.assertEqual(code, 1, out)

    def test_resolved_entries_are_bucketed_by_the_overlay_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["ignored-by-the-resolved-path"])
            overlay = write_overlay(
                directory,
                gnome=["gnome-shell"],
                parity=["fastfetch", "gh"],
                hardware=["linux-firmware"],
                services=["tailscale"],
            )
            installed = {"bash", "coreutils", "gnome-shell", "fastfetch", "gh",
                         "linux-firmware", "tailscale"}
            code, out = self.run_main_with_contract(
                "bash coreutils gnome-shell fastfetch gh linux-firmware tailscale",
                manifest, overlay, installed,
            )
        self.assertEqual(code, 0, out)
        self.assertIn("Verifying 2 Bluefin packages", out)
        self.assertIn("1 GNOME desktop packages", out)
        self.assertIn("2 parity packages", out)
        self.assertIn("1 firmware packages", out)
        self.assertIn("1 desktop service packages", out)
        self.assertIn("All 7 contract packages are present.", out)

    def test_whitespace_separated_contract_is_tolerated(self) -> None:
        """install-packages.py writes the set as a whitespace-joined blob."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, [])
            overlay = write_overlay(directory)
            code, out = self.run_main_with_contract(
                "  bash\n\n  coreutils  \n", manifest, overlay, {"bash", "coreutils"}
            )
        self.assertEqual(code, 0, out)
        self.assertIn("All 2 contract packages are present.", out)


class ParitySectionReachesTheExpectedSetTests(unittest.TestCase):
    """Replaces the source-text grep in test_package_resolution.py with behavior.

    That test asserted the string `parity = section(overlay, "parity")` appeared
    in the file. This asserts the parity section is actually verified.
    """

    def setUp(self) -> None:
        self.module = load_module()

    def test_a_missing_parity_package_fails_the_verifier(self) -> None:
        parity = self.module.section(ROOT / "packages" / "utah.toml", "parity")
        self.assertTrue(parity, "the shipped overlay declares no parity packages")
        target = parity[0]
        argv = [
            "verify-rpm-contract.py",
            str(ROOT / "packages" / "bluefin.toml"),
            str(ROOT / "packages" / "utah.toml"),
        ]
        stderr = io.StringIO()
        with patch.object(self.module, "is_installed", side_effect=lambda p: p != target), \
                patch.object(sys, "argv", argv), \
                patch.dict(os.environ, {"IMAGE_FLAVOR": "main"}), \
                patch.object(sys, "stderr", stderr), \
                redirect_stdout(io.StringIO()):
            code = self.module.main()
        self.assertEqual(code, 1)
        self.assertIn(f"  - {target}\n", stderr.getvalue())


class NvidiaImageAssertionTests(unittest.TestCase):
    """On an NVIDIA flavor the verifier also asserts what a source build produced.

    These run against a fake image root: every absolute `/usr/...` path the
    script consults is redirected into a temporary tree, so the module and
    userspace assertions execute without an NVIDIA image.
    """

    def setUp(self) -> None:
        self.module = load_module()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.directory = self.root / "contract"
        self.directory.mkdir()
        self.manifest = write_manifest(self.directory, [])
        self.overlay = write_overlay(self.directory)

    def image_path(self, absolute: str) -> Path:
        """Translate an in-image absolute path into the fake root."""
        return self.root / "image" / absolute.lstrip("/")

    def add_file(self, absolute: str, text: str = "") -> Path:
        path = self.image_path(absolute)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def add_module_tree(self, release: str, *, nvidia_ko: bool = True) -> None:
        tree = self.image_path(f"/usr/lib/modules/{release}")
        tree.mkdir(parents=True, exist_ok=True)
        if nvidia_ko:
            self.add_file(f"/usr/lib/modules/{release}/extra/nvidia/nvidia.ko")

    def run_main(self, flavor: str, rpm_kernel_stdout: str) -> tuple[int, str, str]:
        real_path = self.module.Path
        root = self.root / "image"

        def redirected(arg, *rest):
            text = str(arg)
            if text.startswith("/usr/") or text.startswith("/etc/"):
                return real_path(root / text.lstrip("/"), *rest)
            return real_path(arg, *rest)

        def fake_run(cmd, *args, **kwargs):
            if list(cmd[:3]) == ["rpm", "-q", "kernel"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=rpm_kernel_stdout)
            # query_packages() reads every package's NEVRA with one rpm call.
            if list(cmd[:3]) == ["rpm", "-q", "--qf"]:
                release = "1.bfin.x86_64"
                fields = [f"{name}|0|1.0|{release}|x86_64" for name in cmd[3:]]
                return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(fields) + "\n")
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        argv = ["verify-rpm-contract.py", str(self.manifest), str(self.overlay)]
        stdout, stderr = io.StringIO(), io.StringIO()
        report_dir = Path(tempfile.mkdtemp())
        with patch.object(self.module, "Path", redirected), \
                patch.object(self.module, "is_installed", return_value=True), \
                patch.object(self.module.subprocess, "run", fake_run), \
                patch.object(sys, "argv", argv), \
                patch.dict(os.environ, {"IMAGE_FLAVOR": flavor, "UTAH_REPORT_DIR": str(report_dir)}), \
                patch.object(sys, "stderr", stderr), \
                redirect_stdout(stdout):
            code = self.module.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_a_complete_nvidia_image_passes(self) -> None:
        self.add_module_tree("6.17.4-200.fc44.x86_64")
        self.add_file("/usr/bin/nvidia-smi")
        self.add_file("/usr/lib/utah/nvidia-driver-version", "580.95.05\n")
        code, out, err = self.run_main("nvidia", "6.17.4-200.fc44.x86_64\n")
        self.assertEqual(code, 0, err)
        self.assertIn("NVIDIA 580.95.05 present for: 6.17.4-200.fc44.x86_64", out)

    def test_a_missing_module_for_the_booted_kernel_fails(self) -> None:
        self.add_module_tree("6.17.4-200.fc44.x86_64", nvidia_ko=False)
        self.add_file("/usr/bin/nvidia-smi")
        self.add_file("/usr/lib/utah/nvidia-driver-version", "580.95.05\n")
        code, _, err = self.run_main("nvidia", "6.17.4-200.fc44.x86_64\n")
        self.assertEqual(code, 1)
        self.assertIn("NVIDIA module missing for kernel 6.17.4-200.fc44.x86_64", err)

    def test_missing_userspace_is_reported_for_each_path(self) -> None:
        self.add_module_tree("6.17.4-200.fc44.x86_64")
        code, _, err = self.run_main("nvidia", "6.17.4-200.fc44.x86_64\n")
        self.assertEqual(code, 1)
        self.assertIn("/usr/bin/nvidia-smi is missing", err.replace(str(self.root / "image"), ""))
        self.assertIn(
            "/usr/lib/utah/nvidia-driver-version is missing",
            err.replace(str(self.root / "image"), ""),
        )

    def test_rpm_failure_text_is_not_mistaken_for_a_kernel_release(self) -> None:
        """`package kernel is not installed` ends in "installed", not a release.

        Taking rpm's last word unconditionally is what made the verifier look
        for a module tree named `installed`. The module-tree fallback must
        rescue that, not report a missing module for a kernel of that name.
        """
        self.add_module_tree("6.17.4-200.fc44.x86_64")
        self.add_file("/usr/bin/nvidia-smi")
        self.add_file("/usr/lib/utah/nvidia-driver-version", "580.95.05\n")
        code, out, err = self.run_main("nvidia", "package kernel is not installed\n")
        self.assertEqual(code, 0, err)
        self.assertIn("present for: 6.17.4-200.fc44.x86_64", out)
        self.assertNotIn("installed/extra", err)

    def test_empty_rpm_output_still_falls_back_to_a_real_module_tree(self) -> None:
        """When rpm -q kernel prints nothing, the fallback must still resolve the tree.

        Path('/usr/lib/modules/') is a directory, so without checking for an
        empty release string first, the fallback is skipped and an empty release
        is asserted instead.
        """
        self.add_module_tree("6.17.4-200.fc44.x86_64")
        self.add_file("/usr/bin/nvidia-smi")
        self.add_file("/usr/lib/utah/nvidia-driver-version", "580.95.05\n")
        code, out, err = self.run_main("nvidia", "")
        self.assertEqual(code, 0, err)
        self.assertIn("present for: 6.17.4-200.fc44.x86_64", out)

    def test_whitespace_rpm_output_still_falls_back_to_a_real_module_tree(self) -> None:
        self.add_module_tree("6.17.4-200.fc44.x86_64")
        self.add_file("/usr/bin/nvidia-smi")
        self.add_file("/usr/lib/utah/nvidia-driver-version", "580.95.05\n")
        code, out, err = self.run_main("nvidia", "   \n\t  \n")
        self.assertEqual(code, 0, err)
        self.assertIn("present for: 6.17.4-200.fc44.x86_64", out)

    def test_no_module_tree_at_all_is_a_hard_failure(self) -> None:
        self.image_path("/usr/lib/modules").mkdir(parents=True)
        self.add_file("/usr/bin/nvidia-smi")
        self.add_file("/usr/lib/utah/nvidia-driver-version", "580.95.05\n")
        code, _, err = self.run_main("nvidia", "package kernel is not installed\n")
        self.assertEqual(code, 1)
        self.assertIn("no kernel module tree found", err)

    def test_no_module_tree_at_all_with_empty_rpm_output_is_a_hard_failure(self) -> None:
        self.image_path("/usr/lib/modules").mkdir(parents=True)
        self.add_file("/usr/bin/nvidia-smi")
        self.add_file("/usr/lib/utah/nvidia-driver-version", "580.95.05\n")
        code, _, err = self.run_main("nvidia", "")
        self.assertEqual(code, 1)
        self.assertIn("no kernel module tree found", err)

    def test_empty_kernel_release_in_releases_fails_without_empty_release_error_message(self) -> None:
        """Empty releases must be refused before building paths, without naming an empty kernel."""
        self.add_file("/usr/lib/utah/ogc-kernel-release", "   \n")
        self.add_module_tree("6.17.4-200.fc44.x86_64")
        self.add_file("/usr/bin/nvidia-smi")
        self.add_file("/usr/lib/utah/nvidia-driver-version", "580.95.05\n")
        code, _, err = self.run_main("nvidia-gaming", "6.17.4-200.fc44.x86_64\n")
        self.assertEqual(code, 1)
        self.assertIn("empty kernel release; no kernel to check NVIDIA module against", err)
        self.assertNotIn("NVIDIA module missing for kernel ", err)

    def test_the_ogc_release_is_excluded_from_the_base_kernel_fallback(self) -> None:
        """The OGC kernel is the gaming kernel, not the base one it stands in for."""
        self.add_file("/usr/lib/utah/ogc-kernel-release", "6.17.4-200.ogc.x86_64\n")
        self.add_module_tree("6.17.4-200.ogc.x86_64")
        self.add_module_tree("6.17.4-200.fc44.x86_64")
        self.add_file("/usr/bin/nvidia-smi")
        self.add_file("/usr/lib/utah/nvidia-driver-version", "580.95.05\n")
        code, out, err = self.run_main("nvidia", "package kernel is not installed\n")
        self.assertEqual(code, 0, err)
        self.assertIn("present for: 6.17.4-200.fc44.x86_64", out)

    def test_nvidia_gaming_requires_a_module_for_the_ogc_kernel_too(self) -> None:
        self.add_file("/usr/lib/utah/ogc-kernel-release", "6.17.4-200.ogc.x86_64\n")
        self.add_module_tree("6.17.4-200.fc44.x86_64")
        self.add_module_tree("6.17.4-200.ogc.x86_64", nvidia_ko=False)
        self.add_file("/usr/bin/nvidia-smi")
        self.add_file("/usr/lib/utah/nvidia-driver-version", "580.95.05\n")
        code, _, err = self.run_main("nvidia-gaming", "6.17.4-200.fc44.x86_64\n")
        self.assertEqual(code, 1)
        self.assertIn("NVIDIA module missing for kernel 6.17.4-200.ogc.x86_64", err)

    def test_a_non_nvidia_flavor_never_reaches_the_module_assertions(self) -> None:
        """No module tree, no nvidia-smi: a plain image must still pass."""
        code, out, err = self.run_main("main", "")
        self.assertEqual(code, 0, err)
        self.assertNotIn("NVIDIA", out.split("Verifying", 1)[-1].split("\n", 1)[-1])


class SupplyChainTests(unittest.TestCase):
    """The supply-chain attestation added for issue #21.

    These exercise the functions the contract verifier gained on top of the
    original presence check: release-identity classification, GNOME version
    attestation, parity-origin attestation, and repository-policy enforcement.
    Every test runs offline against in-memory package dicts and fake .repo
    files -- no image build required.
    """

    def setUp(self) -> None:
        self.module = load_module()

    @staticmethod
    def _parser(fields: dict[str, str]) -> configparser.ConfigParser:
        parser = configparser.ConfigParser(interpolation=None)
        parser["repo"] = fields
        return parser

    def test_determine_origin_factory_from_bfin_release(self) -> None:
        self.assertEqual(self.module.determine_origin("gnome-shell", "51.0-1.bfin.x86_64"), "factory")

    def test_determine_origin_hummingbird_from_hum_release(self) -> None:
        self.assertEqual(self.module.determine_origin("fastfetch", "1.0-1.hum.x86_64"), "hummingbird")

    def test_determine_origin_fedora_from_fc_release(self) -> None:
        self.assertEqual(self.module.determine_origin("coreutils", "9.0-1.fc44.x86_64"), "fedora")

    def test_determine_origin_unknown_when_release_has_no_identity(self) -> None:
        self.assertEqual(self.module.determine_origin("something", "1.0-1.x86_64"), "unknown")

    def test_determine_origin_prefers_bfin_over_fedora(self) -> None:
        """A release carrying both identities is factory, not Fedora."""
        self.assertEqual(self.module.determine_origin("gnome-shell", "51.0-1.fc44.bfin.x86_64"), "factory")

    def test_gnome_contract_passes_for_promised_major(self) -> None:
        installed = {"gnome-shell": {"release": "51.2-1.bfin.x86_64", "version": "51.2"}}
        errors = self.module.verify_gnome_contract(["gnome-shell"], installed, {"gnome-shell": "51"}, set())
        self.assertEqual(errors, [])

    def test_gnome_contract_flags_wrong_major(self) -> None:
        installed = {"gnome-shell": {"release": "50.1-1.bfin.x86_64", "version": "50.1"}}
        errors = self.module.verify_gnome_contract(["gnome-shell"], installed, {"gnome-shell": "51"}, set())
        self.assertEqual(len(errors), 1)
        self.assertIn("gnome-shell", errors[0])
        self.assertIn("51", errors[0])

    def test_gnome_contract_rejects_bare_fedora(self) -> None:
        """A GNOME package resolving from a bare Fedora release is rejected."""
        installed = {"gnome-shell": {"release": "51.2-1.fc44.x86_64", "version": "51.2"}}
        errors = self.module.verify_gnome_contract(["gnome-shell"], installed, {"gnome-shell": "51"}, set())
        self.assertEqual(len(errors), 1, f"one defect must report one error: {errors}")
        self.assertIn("unapproved Fedora", errors[0])

    def test_gnome_contract_reports_unidentified_release_once(self) -> None:
        """A release with no identity at all is reported once, generally."""
        installed = {"gnome-shell": {"release": "51.2-1.x86_64", "version": "51.2"}}
        errors = self.module.verify_gnome_contract(["gnome-shell"], installed, {"gnome-shell": "51"}, set())
        self.assertEqual(len(errors), 1)
        self.assertIn("unapproved release", errors[0])

    def test_gnome_contract_passes_for_hummingbird_identity(self) -> None:
        installed = {"gnome-shell": {"release": "51.2-1.hum.x86_64", "version": "51.2"}}
        errors = self.module.verify_gnome_contract(["gnome-shell"], installed, {"gnome-shell": "51"}, set())
        self.assertEqual(errors, [])

    def test_gnome_contract_requires_factory_rebuild_for_factory_package(self) -> None:
        installed = {"gnome-shell": {"release": "51.2-1.hum.x86_64", "version": "51.2"}}
        errors = self.module.verify_gnome_contract(["gnome-shell"], installed, {"gnome-shell": "51"}, {"gnome-shell"})
        self.assertEqual(len(errors), 1)
        self.assertIn(".bfin", errors[0])

    def test_gnome_package_absent_from_versions_is_not_version_attested(self) -> None:
        """A GNOME package not listed in [gnome.versions] is simply not checked."""
        installed = {"gnome-shell": {"release": "51.2-1.bfin.x86_64", "version": "51.2"}}
        errors = self.module.verify_gnome_contract(["gnome-shell"], installed, {}, set())
        self.assertEqual(errors, [])

    def test_parity_origin_passes_for_approved_origin(self) -> None:
        installed = {"fastfetch": {"release": "1.0-1.hum.x86_64"}}
        self.assertEqual(self.module.verify_parity_origin(["fastfetch"], installed), [])

    def test_parity_origin_rejects_bare_fedora(self) -> None:
        installed = {"fastfetch": {"release": "1.0-1.fc44.x86_64"}}
        errors = self.module.verify_parity_origin(["fastfetch"], installed)
        self.assertEqual(len(errors), 1)
        self.assertIn("fastfetch", errors[0])

    def test_parity_origin_passes_when_tagged_hummingbird(self) -> None:
        installed = {"fastfetch": {"release": "1.0-1.fc44.hum.x86_64"}}
        self.assertEqual(self.module.verify_parity_origin(["fastfetch"], installed), [])

    def test_factory_parity_package_must_carry_factory_identity(self) -> None:
        """A factory-supplied parity package resolving from Hummingbird fails."""
        installed = {"fprintd": {"release": "1.94.5-1.hum1.x86_64"}}
        errors = self.module.verify_parity_origin(["fprintd"], installed, {"fprintd"})
        self.assertEqual(len(errors), 1)
        self.assertIn("fprintd", errors[0])
        self.assertIn(".bfin", errors[0])

    def test_factory_parity_package_passes_on_factory_release(self) -> None:
        installed = {"fprintd": {"release": "1.94.5-1.hum1.bfin.x86_64"}}
        self.assertEqual(
            self.module.verify_parity_origin(["fprintd"], installed, {"fprintd"}), []
        )

    def test_factory_parity_rule_does_not_leak_to_other_parity_packages(self) -> None:
        installed = {"fastfetch": {"release": "1.0-1.hum1.x86_64"}}
        self.assertEqual(
            self.module.verify_parity_origin(["fastfetch"], installed, {"fprintd"}), []
        )

    def test_shipped_factory_parity_names_are_declared_parity_packages(self) -> None:
        """Every [factory] parity name must be a package the contract installs."""
        overlay = ROOT / "packages" / "utah.toml"
        parity = set(self.module.section(overlay, "parity"))
        factory_parity = self.module.section(overlay, "factory", "parity")
        self.assertTrue(factory_parity, "the shipped overlay declares no factory parity packages")
        self.assertEqual([p for p in factory_parity if p not in parity], [])

    def test_shipped_gnome_versions_cover_the_gnome_release_packages(self) -> None:
        """nautilus and gnome-initial-setup track GNOME, so they are asserted too."""
        overlay = ROOT / "packages" / "utah.toml"
        versions = tomllib.loads(overlay.read_text())["gnome"]["versions"]
        for pkg in ("nautilus", "gnome-initial-setup"):
            self.assertEqual(versions.get(pkg), "51", f"{pkg} is not version-asserted")

    def test_normalize_baseurl_strips_trailing_slash_and_lowercases_scheme_and_host(self) -> None:
        self.assertEqual(
            self.module.normalize_baseurl("HTTPS://Packages.Redhat.com/A/"),
            "https://packages.redhat.com/A",
        )

    def test_normalize_baseurl_normalizes_basearch_token_only(self) -> None:
        """${basearch} becomes $basearch; the rest of the path is case-sensitive."""
        self.assertEqual(
            self.module.normalize_baseurl("https://x/Y/${basearch}/Repo"),
            "https://x/Y/$basearch/Repo",
        )
    def test_repo_pin_errors_flags_unpinned_baseurl(self) -> None:
        parser = self._parser({"baseurl": "https://a.example.com/$basearch"})
        errors = self.module.repo_pin_errors(
            "repo", parser, "fedora.repo", {"repo": ("https://pinned.example.com/$basearch",)})
        self.assertEqual(len(errors), 1)
        self.assertIn("unpinned", errors[0])

    def test_repo_pin_errors_flags_metalink(self) -> None:
        parser = self._parser({"metalink": "https://mirrors.example.com/metalink?f=fedora"})
        errors = self.module.repo_pin_errors(
            "repo", parser, "fedora.repo", {"repo": ("https://pinned.example.com/$basearch",)})
        self.assertIn("metalink", errors[0])

    def test_repo_pin_errors_flags_mirrorlist(self) -> None:
        parser = self._parser({"mirrorlist": "https://mirrors.example.com/list?f=fedora"})
        errors = self.module.repo_pin_errors(
            "repo", parser, "fedora.repo", {"repo": ("https://pinned.example.com/$basearch",)})
        self.assertIn("mirrorlist", errors[0])

    def test_repo_pin_errors_flags_declared_without_baseurl(self) -> None:
        """An allowlisted repo id must have a pinned baseurl in the manifest."""
        parser = self._parser({"baseurl": "https://a.example.com/$basearch"})
        errors = self.module.repo_pin_errors("repo", parser, "fedora.repo", {})
        self.assertEqual(len(errors), 1)
        self.assertIn("no pinned baseurl", errors[0])

    def test_repo_pin_errors_passes_for_matching_pinned_baseurl(self) -> None:
        parser = self._parser({"baseurl": "https://pinned.example.com/$basearch"})
        errors = self.module.repo_pin_errors(
            "repo", parser, "fedora.repo", {"repo": ("https://pinned.example.com/$basearch",)})
        self.assertEqual(errors, [])

    def test_repo_security_option_errors_flags_proxy(self) -> None:
        parser = self._parser({"baseurl": "https://a.example.com/$basearch", "proxy": "http://proxy:3128"})
        errors = self.module.repo_security_option_errors("repo", parser, "fedora.repo")
        self.assertIn("proxy", errors[0])

    def test_repo_security_option_errors_flags_sslverify_zero(self) -> None:
        parser = self._parser({"baseurl": "https://a.example.com/$basearch", "sslverify": "0"})
        errors = self.module.repo_security_option_errors("repo", parser, "fedora.repo")
        self.assertIn("sslverify", errors[0])

    def test_repo_security_option_errors_passes_when_clean(self) -> None:
        parser = self._parser({"baseurl": "https://a.example.com/$basearch"})
        self.assertEqual(self.module.repo_security_option_errors("repo", parser, "fedora.repo"), [])

    def test_check_repo_sections_flags_unapproved_repo(self) -> None:
        parser = self._parser({"baseurl": "https://a.example.com/$basearch", "enabled": "1"})
        errors = self.module.check_repo_sections(
            parser, "fedora.repo", {"public-hummingbird-x86_64-rpms"}, expected_baseurls=None)
        self.assertIn("Unapproved", errors[0])

    def test_check_repo_sections_allows_approved_repo(self) -> None:
        parser = self._parser({"baseurl": "https://a.example.com/$basearch", "enabled": "1"})
        errors = self.module.check_repo_sections(parser, "fedora.repo", {"repo"}, expected_baseurls=None)
        self.assertEqual(errors, [])

    def test_check_repo_sections_skips_disabled_repo(self) -> None:
        parser = self._parser({"baseurl": "https://a.example.com/$basearch", "enabled": "0"})
        errors = self.module.check_repo_sections(parser, "fedora.repo", set(), expected_baseurls=None)
        self.assertEqual(errors, [])

    def test_check_repo_sections_flags_fedora_repo(self) -> None:
        parser = self._parser({"baseurl": "https://src.fedoraproject.org/repos/fedora-$basearch", "enabled": "1"})
        errors = self.module.check_repo_sections(
            parser, "fedora.repo", {"public-hummingbird-x86_64-rpms"}, expected_baseurls=None)
        self.assertIn("Fedora", errors[0])
    def _write_repo(self, directory: Path, repo_id: str, **fields) -> Path:
        parser = configparser.ConfigParser(interpolation=None)
        parser[repo_id] = fields
        path = directory / f"{repo_id}.repo"
        with path.open("w") as handle:
            parser.write(handle)
        return path

    def test_verify_repository_policy_passes_for_clean_allowlist(self) -> None:
        directory = Path(tempfile.mkdtemp())
        self._write_repo(
            directory, "public-hummingbird-x86_64-rpms",
            baseurl="https://packages.redhat.com/api/pulp-content/public-hummingbird/x86_64/",
            enabled="1")
        errors = self.module.verify_repository_policy(
            directory, {"public-hummingbird-x86_64-rpms"}, check_mode=True,
            expected_baseurls={"public-hummingbird-x86_64-rpms":
                               ("https://packages.redhat.com/api/pulp-content/public-hummingbird/x86_64/",)})
        self.assertEqual(errors, [])

    def test_verify_repository_policy_flags_unpinned_allowlisted_repo(self) -> None:
        directory = Path(tempfile.mkdtemp())
        self._write_repo(
            directory, "public-hummingbird-x86_64-rpms",
            baseurl="http://unpinned.example.com/$basearch", enabled="1")
        errors = self.module.verify_repository_policy(
            directory, {"public-hummingbird-x86_64-rpms"}, check_mode=True,
            expected_baseurls={"public-hummingbird-x86_64-rpms":
                               ("https://packages.redhat.com/api/pulp-content/public-hummingbird/x86_64/",)})
        self.assertTrue(any("pinned" in e for e in errors))

    def test_verify_repository_policy_flags_unapproved_repo(self) -> None:
        directory = Path(tempfile.mkdtemp())
        self._write_repo(directory, "third-party", baseurl="https://third-party.example.com/$basearch", enabled="1")
        errors = self.module.verify_repository_policy(
            directory, {"public-hummingbird-x86_64-rpms"}, check_mode=True, expected_baseurls=None)
        self.assertTrue(any("approved" in e for e in errors))

    def test_verify_repository_policy_skips_builder_only_files(self) -> None:
        """A marker authorizes no skip unless the file is exclusively builder-copied."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "packages"
            directory.mkdir()
            (directory / "fedora-44.repo").write_text(
                "# builder-only: true\n[fedora]\nbaseurl=https://a.example.com/$basearch\n")
            containerfile = root / "Containerfile"
            containerfile.write_text("FROM base AS builder\nCOPY packages/fedora-44.repo /etc/yum.repos.d/\nFROM base\n")
            errors = self.module.verify_repository_policy(
                directory, set(), check_mode=True, expected_baseurls=None)
            self.assertEqual(errors, [])
            containerfile.write_text(containerfile.read_text() + "COPY packages/*.repo /etc/yum.repos.d/\n")
            errors = self.module.verify_repository_policy(
                directory, set(), check_mode=True, expected_baseurls=None)
            self.assertTrue(any("Fedora" in error for error in errors), errors)
            containerfile.unlink()
            errors = self.module.verify_repository_policy(
                directory, set(), check_mode=True, expected_baseurls=None)
            self.assertTrue(any("exclusive builder COPY" in error for error in errors), errors)


    def test_verify_repository_policy_skips_disabled_repos(self) -> None:
        directory = Path(tempfile.mkdtemp())
        self._write_repo(directory, "disabled-repo", baseurl="http://x/$basearch", enabled="0")
        errors = self.module.verify_repository_policy(
            directory, set(), check_mode=True, expected_baseurls=None)
        self.assertEqual(errors, [])

    def test_resolve_build_timestamp_reads_source_date_epoch(self) -> None:
        ts, source = self.module.resolve_build_timestamp({"SOURCE_DATE_EPOCH": "1700000000"})
        self.assertEqual(source, "SOURCE_DATE_EPOCH")
        self.assertEqual(ts, "2023-11-14T22:13:20+00:00")

    def test_resolve_build_timestamp_records_nothing_when_epoch_unset(self) -> None:
        ts, source = self.module.resolve_build_timestamp({})
        self.assertIsNone(ts)
        self.assertEqual(source, "unset")

    def test_resolve_build_timestamp_records_nothing_when_epoch_unusable(self) -> None:
        with patch.object(sys, "stderr", io.StringIO()):
            ts, source = self.module.resolve_build_timestamp({"SOURCE_DATE_EPOCH": "not-an-epoch"})
        self.assertIsNone(ts)
        self.assertEqual(source, "unusable")

    def test_read_factory_pin_reads_the_repo_file_stamp(self) -> None:
        directory = Path(tempfile.mkdtemp())
        repo_file = directory / "utah-packages.repo"
        digest = "sha256:" + "a" * 64
        repo_file.write_text(f"# a comment\n# factory-pin: {digest}\n[utah-packages]\n")
        self.assertEqual(self.module.read_factory_pin(repo_file), digest)

    def test_read_factory_pin_is_none_without_a_stamp_or_a_file(self) -> None:
        directory = Path(tempfile.mkdtemp())
        unstamped = directory / "utah-packages.repo"
        unstamped.write_text("[utah-packages]\nenabled=1\n")
        self.assertIsNone(self.module.read_factory_pin(unstamped))
        self.assertIsNone(self.module.read_factory_pin(directory / "absent.repo"))

    def test_generate_provenance_report_writes_json_and_txt(self) -> None:
        output_dir = Path(tempfile.mkdtemp())
        installed = {
            "gnome-shell": {"name": "gnome-shell", "epoch": "0", "version": "51.2",
                            "release": "1.bfin.x86_64", "arch": "x86_64",
                            "nevra": "gnome-shell-51.2-1.bfin.x86_64", "origin": "factory"},
        }
        sections = {"gnome-shell": "gnome"}
        with patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1700000000"}):
            report = self.module.generate_provenance_report(
                installed, "main", {"public-hummingbird-x86_64-rpms"}, sections, output_dir=output_dir)
        self.assertEqual(report["build_provenance"]["flavor"], "main")
        self.assertEqual(report["build_provenance"]["timestamp"], "2023-11-14T22:13:20+00:00")
        self.assertEqual(report["build_provenance"]["timestamp_source"], "SOURCE_DATE_EPOCH")
        self.assertEqual(report["packages"]["gnome-shell"]["origin"], "factory")
        self.assertEqual(report["packages"]["gnome-shell"]["section"], "gnome")
        self.assertTrue((output_dir / "package-origins.json").exists())
        self.assertTrue((output_dir / "package-origins.txt").exists())

    def test_generate_provenance_report_records_the_factory_pin_and_base_image(self) -> None:
        """The report says which factory and which base the NEVRAs came from."""
        output_dir = Path(tempfile.mkdtemp())
        digest = "sha256:" + "b" * 64
        base = "quay.io/hummingbird-community/bootc-os:latest@sha256:" + "c" * 64
        installed = {
            "gnome-shell": {"name": "gnome-shell", "epoch": "0", "version": "51.2",
                            "release": "1.bfin.x86_64", "arch": "x86_64",
                            "nevra": "gnome-shell-51.2-1.bfin.x86_64", "origin": "factory"},
        }
        with patch.object(self.module, "read_factory_pin", return_value=digest), \
                patch.dict(os.environ, {"BASE_IMAGE": base}):
            os.environ.pop("SOURCE_DATE_EPOCH", None)
            report = self.module.generate_provenance_report(
                installed, "main", set(), {}, output_dir=output_dir)
        provenance = report["build_provenance"]
        self.assertEqual(provenance["factory_pin"], digest)
        self.assertEqual(provenance["base_image"], base)
        self.assertEqual(provenance["base_image_digest"], "sha256:" + "c" * 64)
        self.assertIsNone(provenance["timestamp"])
        self.assertEqual(provenance["timestamp_source"], "unset")
        text = (output_dir / "package-origins.txt").read_text()
        self.assertIn(f"# Factory pin: {digest}", text)
        self.assertIn("# Generated: unstamped (unset)", text)

    def test_generate_provenance_report_records_every_multilib_install(self) -> None:
        """A multilib pair is two installs of one name; both reach the report."""
        output_dir = Path(tempfile.mkdtemp())
        x86 = {"name": "mesa-dri-drivers", "epoch": "0", "version": "25.1",
               "release": "1.hum.x86_64", "arch": "x86_64",
               "nevra": "mesa-dri-drivers-25.1-1.hum.x86_64", "origin": "hummingbird"}
        i686 = {**x86, "arch": "i686", "release": "1.hum.i686",
                "nevra": "mesa-dri-drivers-25.1-1.hum.i686"}
        installed = {"mesa-dri-drivers": {**x86, "installs": [x86, i686]}}
        report = self.module.generate_provenance_report(
            installed, "main", set(), {"mesa-dri-drivers": "parity"}, output_dir=output_dir)
        installs = report["packages"]["mesa-dri-drivers"]["installs"]
        self.assertEqual([i["arch"] for i in installs], ["x86_64", "i686"])
        text = (output_dir / "package-origins.txt").read_text()
        self.assertIn("mesa-dri-drivers-25.1-1.hum.x86_64", text)
        self.assertIn("mesa-dri-drivers-25.1-1.hum.i686", text)


class OnImageRepoAllowlistTests(unittest.TestCase):
    """The on-image run scans the composed image's /etc/yum.repos.d (#454).

    `--check` already enforces the repository allowlist against the source
    repo files in `packages/`. The Hummingbird base image ships its own repo
    files; without an on-image scan those pass into the runtime unattested.
    These tests cover the scan wired into main()'s verify-mode path: a clean
    runtime repo set passes, an enabled Fedora or unapproved repo fails, a
    disabled repo is skipped, and a Hummingbird-shipped file with the right
    section id still passes.
    """

    def setUp(self) -> None:
        self.module = load_module()

    def run_main(self, manifest: Path, overlay: Path, installed: set[str],
                 runtime_repos_dir: Path, flavor: str = "main") -> tuple[int, str, str]:
        return run_main(
            self.module, manifest, overlay, installed,
            flavor=flavor, runtime_repos_dir=runtime_repos_dir,
            stderr_buffer=io.StringIO(),
        )

    def test_a_clean_runtime_repo_set_passes(self) -> None:
        """The runtime /etc/yum.repos.d only contains the allowlisted repos."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(
                directory,
                gnome=["gnome-shell"],
                repositories=[
                    "public-hummingbird-x86_64-rpms",
                    "utah-packages",
                    "nvidia-container-toolkit",
                ],
                baseurls={
                    "public-hummingbird-x86_64-rpms":
                        "https://packages.redhat.com/api/pulp-content/public-hummingbird/x86_64/",
                    "utah-packages": "file:///etc/utah-packages",
                    "nvidia-container-toolkit":
                        "https://nvidia.github.io/libnvidia-container/stable/rpm/$basearch",
                },
            )
            runtime_repos = directory / "runtime-yum-repos"
            write_repo_file(
                runtime_repos, "public-hummingbird-x86_64-rpms",
                baseurl="https://packages.redhat.com/api/pulp-content/public-hummingbird/x86_64/",
            )
            write_repo_file(
                runtime_repos, "utah-packages",
                baseurl="file:///etc/utah-packages",
            )
            write_repo_file(
                runtime_repos, "nvidia-container-toolkit",
                baseurl="https://nvidia.github.io/libnvidia-container/stable/rpm/$basearch",
                enabled="0",
            )
            code, out, err = self.run_main(
                manifest, overlay, {"bash", "gnome-shell"}, runtime_repos
            )
        self.assertEqual(code, 0, err)
        self.assertIn("All 2 contract packages are present.", out)

    def test_an_enabled_fedora_repo_in_the_image_fails(self) -> None:
        """A Hummingbird-shipped Fedora repo file fails the on-image run."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(directory, gnome=["gnome-shell"])
            runtime_repos = directory / "runtime-yum-repos"
            write_repo_file(
                runtime_repos, "public-hummingbird-x86_64-rpms",
                baseurl="https://packages.redhat.com/api/pulp-content/public-hummingbird/x86_64/",
            )
            write_repo_file(
                runtime_repos, "fedora-rawhide",
                baseurl="https://dl.fedoraproject.org/pub/fedora/linux/development/rawhide/$basearch/os/",
            )
            code, _, err = self.run_main(
                manifest, overlay, {"bash", "gnome-shell"}, runtime_repos
            )
        self.assertEqual(code, 1)
        self.assertIn("Fedora repository 'fedora-rawhide' is enabled", err)
        self.assertIn("fedora-rawhide.repo", err)

    def test_an_unapproved_repo_in_the_image_fails(self) -> None:
        """A Hummingbird-shipped repo id not on the allowlist fails."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(directory, gnome=["gnome-shell"])
            runtime_repos = directory / "runtime-yum-repos"
            write_repo_file(
                runtime_repos, "public-hummingbird-x86_64-rpms",
                baseurl="https://packages.redhat.com/api/pulp-content/public-hummingbird/x86_64/",
            )
            write_repo_file(
                runtime_repos, "third-party",
                baseurl="https://third-party.example.com/$basearch",
            )
            code, _, err = self.run_main(
                manifest, overlay, {"bash", "gnome-shell"}, runtime_repos
            )
        self.assertEqual(code, 1)
        self.assertIn("Unapproved repository 'third-party' is enabled", err)

    def test_a_disabled_repo_is_skipped(self) -> None:
        """An allowlisted repo with enabled=0 contributes no error."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(directory, gnome=["gnome-shell"])
            runtime_repos = directory / "runtime-yum-repos"
            write_repo_file(
                runtime_repos, "public-hummingbird-x86_64-rpms",
                baseurl="https://packages.redhat.com/api/pulp-content/public-hummingbird/x86_64/",
                enabled="0",
            )
            code, _, err = self.run_main(
                manifest, overlay, {"bash", "gnome-shell"}, runtime_repos
            )
        self.assertEqual(code, 0, err)

    def test_an_empty_runtime_repo_dir_passes(self) -> None:
        """A real Fedora host's /etc/yum.repos.d might not be readable here."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(directory, gnome=["gnome-shell"])
            runtime_repos = directory / "runtime-yum-repos-empty"
            runtime_repos.mkdir()
            code, _, err = self.run_main(
                manifest, overlay, {"bash", "gnome-shell"}, runtime_repos
            )
        self.assertEqual(code, 0, err)

    def test_an_unpinned_baseurl_in_an_allowlisted_repo_fails(self) -> None:
        """A Hummingbird repo file with a different baseurl fails the pin check."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(directory, gnome=["gnome-shell"])
            runtime_repos = directory / "runtime-yum-repos"
            write_repo_file(
                runtime_repos, "public-hummingbird-x86_64-rpms",
                baseurl="https://mirror.example.com/hummingbird/$basearch/",
            )
            code, _, err = self.run_main(
                manifest, overlay, {"bash", "gnome-shell"}, runtime_repos
            )
        self.assertEqual(code, 1)
        self.assertIn("unpinned baseurl", err)


class UsageTests(unittest.TestCase):
    """The manifest argument is required; the verifier must not run without one."""

    def test_missing_manifest_argument_is_rejected(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manifest", result.stderr)


if __name__ == "__main__":
    unittest.main()
