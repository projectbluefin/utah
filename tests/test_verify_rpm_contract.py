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
    Only packages the overlay declares in [gnome] get a version key: `--check`
    rejects a [gnome.versions] key that names no GNOME package.
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
             runtime_repos_dirs: "Path | list[Path] | None" = None,
             stderr_buffer: io.StringIO | None = None,
             reposdir_error: Exception | None = None) -> tuple[int, str, str]:
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
    # A single dir for the common case, or a list to cover the on-image scan
    # over all of dnf5's default reposdir paths (#513).
    if runtime_repos_dirs is None:
        runtime_repos: list[Path] = [Path(tempfile.mkdtemp())]
    elif isinstance(runtime_repos_dirs, Path):
        runtime_repos = [runtime_repos_dirs]
    else:
        runtime_repos = list(runtime_repos_dirs)
    def runtime_reposdir_paths() -> list[Path]:
        if reposdir_error is not None:
            raise reposdir_error
        return list(runtime_repos)

    stderr_text = ""
    base_patches = [
        patch.object(module, "is_installed",
                     side_effect=lambda p: p in installed),
        patch.object(module, "query_packages", side_effect=fake_query),
        patch.object(module, "runtime_reposdir_paths", runtime_reposdir_paths),
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

    def test_a_gnome_version_key_naming_no_gnome_package_is_rejected(self) -> None:
        """A misspelled key would silently drop that package's version claim."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(
                directory,
                gnome=["gnome-shell"],
                gnome_versions={"gnome-shell": "51", "gnome-shel": "51"},
            )
            result = self.run_check(manifest, overlay)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gnome-shel", result.stderr)
        self.assertIn("is not declared in the [gnome] section", result.stderr)

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
                 runtime_repos_dirs: "Path | list[Path] | None" = None) -> tuple[int, str]:
        code, out, _ = run_main(
            self.module, manifest, overlay, installed,
            flavor=flavor, releases=releases, multilib=multilib,
            extra_argv=extra_argv, report_dir=report_dir,
            runtime_repos_dirs=runtime_repos_dirs,
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
            runtime_repos = [Path(tempfile.mkdtemp())]
            with patch.object(self.module, "Path", redirected), \
                    patch.object(self.module, "runtime_reposdir_paths",
                                 lambda: list(runtime_repos)), \
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

    def test_generate_provenance_report_is_identical_across_build_days(self) -> None:
        """VERSION carries the build date; the retained report must not (#346)."""
        installed = {
            "gnome-shell": {"name": "gnome-shell", "epoch": "0", "version": "51.2",
                            "release": "1.bfin.x86_64", "arch": "x86_64",
                            "nevra": "gnome-shell-51.2-1.bfin.x86_64", "origin": "factory"},
        }
        outputs = []
        for version in ("testing-20260929-abc1234", "testing-20260930-abc1234"):
            output_dir = Path(tempfile.mkdtemp())
            env = {"SOURCE_DATE_EPOCH": "1700000000", "VERSION": version,
                   "SHA_HEAD_SHORT": "abc1234"}
            with patch.dict(os.environ, env):
                report = self.module.generate_provenance_report(
                    installed, "main", set(), {}, output_dir=output_dir)
            self.assertEqual(report["build_provenance"]["commit"], "abc1234")
            outputs.append(tuple((output_dir / f).read_bytes()
                                 for f in ("package-origins.json", "package-origins.txt")))
        self.assertEqual(outputs[0], outputs[1])

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


class Dnf5ConfigTests(unittest.TestCase):
    """The on-image reposdir scan reads dnf5's [main] config rather than hardcoding.

    A `reposdir=` setting in /etc/dnf/dnf.conf or any drop-in under
    /etc/dnf/libdnf5.conf.d/ or /usr/share/dnf5/libdnf.conf.d/ replaces the documented default
    (`/etc/yum.repos.d`, `/etc/distro.repos.d`, `/usr/share/dnf5/repos.d`).
    The hardcoded list misses the configured paths (#536), so the scan reads
    every config in load order and uses the last-set value. A config that
    sets `reposdir=` to a single custom path makes that path the only reposdir
    dnf5 loads, and the gate must scan it.
    """

    def setUp(self) -> None:
        self.module = load_module()

    def _write_conf(self, directory: Path, name: str, body: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_parse_reposdir_returns_none_when_no_config_sets_it(self) -> None:
        """Without a `reposdir=` line in any config, the documented default applies."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            distro = directory / "distro"
            user = directory / "user"
            paths = [self._write_conf(distro, "10-base.conf", "[main]\n"),
                     self._write_conf(user, "99-empty.conf", "[main]\n")]
            self.assertIsNone(self.module.parse_reposdir_from_config(paths))

    def test_parse_reposdir_picks_up_a_single_set_value(self) -> None:
        """A single `reposdir=` line replaces the documented default."""
        body = "[main]\nreposdir = /opt/repos\n"
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = self._write_conf(directory, "00-base.conf", body)
            result = self.module.parse_reposdir_from_config([path])
        self.assertEqual(result, [Path("/opt/repos")])

    def test_parse_reposdir_splits_space_separated_list(self) -> None:
        """dnf5 accepts whitespace-separated entries inside `reposdir=`."""
        body = "[main]\nreposdir = /opt/repos /etc/extra-repos\n"
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = self._write_conf(directory, "00-base.conf", body)
            result = self.module.parse_reposdir_from_config([path])
        self.assertEqual(result, [Path("/opt/repos"), Path("/etc/extra-repos")])

    def test_parse_reposdir_splits_comma_separated_list(self) -> None:
        """dnf5 accepts comma-separated entries inside `reposdir=`."""
        body = "[main]\nreposdir = /opt/repos, /etc/extra-repos\n"
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = self._write_conf(directory, "00-base.conf", body)
            result = self.module.parse_reposdir_from_config([path])
        self.assertEqual(result, [Path("/opt/repos"), Path("/etc/extra-repos")])

    def test_parse_reposdir_later_config_wins(self) -> None:
        """dnf5 documents that the last option wins; later files override earlier ones."""
        body_early = "[main]\nreposdir = /opt/early-repos\n"
        body_late = "[main]\nreposdir = /opt/late-repos\n"
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            early = self._write_conf(directory, "00-early.conf", body_early)
            late = self._write_conf(directory, "99-late.conf", body_late)
            result = self.module.parse_reposdir_from_config([early, late])
        self.assertEqual(result, [Path("/opt/late-repos")])

    def test_parse_reposdir_skips_files_without_a_main_section(self) -> None:
        """A config with no [main] does not contribute to `reposdir=`."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = self._write_conf(
                directory, "00-no-main.conf",
                "[repos]\nid = value\n",
            )
            self.assertIsNone(self.module.parse_reposdir_from_config([path]))

    def test_parse_reposdir_fails_closed_on_malformed_config(self) -> None:
        """An unparseable config raises instead of silently falling back to the defaults."""
        body_late = "[main]\nreposdir = /opt/late-repos\n"
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            bad = self._write_conf(
                directory, "00-bad.conf",
                "[main\nunterminated = section\n",
            )
            late = self._write_conf(directory, "99-late.conf", body_late)
            with self.assertRaises(self.module.Dnf5ConfigError):
                self.module.parse_reposdir_from_config([bad, late])

    def test_parse_reposdir_accepts_duplicate_keys_like_libdnf5(self) -> None:
        """libdnf5 lets a duplicate key overwrite; a duplicate must not drop reposdir."""
        body = "[main]\nexclude=foo\nexclude=bar\nreposdir=/opt/evil\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_conf(Path(tmp), "dnf.conf", body)
            result = self.module.parse_reposdir_from_config([path])
        self.assertEqual(result, [Path("/opt/evil")])

    def test_parse_reposdir_duplicate_reposdir_and_sections_last_wins(self) -> None:
        """Duplicate [main] blocks merge and the last reposdir wins, as in libdnf5."""
        body = "[main]\nreposdir=/opt/first\n[main]\nreposdir=/opt/second\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_conf(Path(tmp), "dnf.conf", body)
            result = self.module.parse_reposdir_from_config([path])
        self.assertEqual(result, [Path("/opt/second")])

    def test_parse_reposdir_substitutes_basearch(self) -> None:
        """libdnf5 expands $basearch/$arch in [main] values; the gate must scan the expanded path."""
        body = "[main]\nreposdir=/etc/repos-$basearch,/etc/r-${arch}\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_conf(Path(tmp), "dnf.conf", body)
            with patch.object(self.module.os, "uname",
                              return_value=os.uname_result(("", "", "", "", "x86_64"))):
                result = self.module.parse_reposdir_from_config([path])
        self.assertEqual(result, [Path("/etc/repos-x86_64"), Path("/etc/r-x86_64")])

    def test_parse_reposdir_fails_closed_on_unresolvable_variable(self) -> None:
        """A variable the gate cannot resolve raises rather than scanning a literal path."""
        body = "[main]\nreposdir=/etc/repos-$releasever\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_conf(Path(tmp), "dnf.conf", body)
            with self.assertRaises(self.module.Dnf5ConfigError):
                self.module.parse_reposdir_from_config([path])

    def test_parse_reposdir_fails_closed_on_default_value_substitution(self) -> None:
        """libdnf5's `${var:-default}` form must not pass through as a literal path."""
        body = "[main]\nreposdir=/etc/repos-${releasever:-44}\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_conf(Path(tmp), "dnf.conf", body)
            with self.assertRaises(self.module.Dnf5ConfigError):
                self.module.parse_reposdir_from_config([path])

    def test_parse_reposdir_fails_closed_on_alternate_value_substitution(self) -> None:
        """libdnf5's `${var:+alt}` form must not pass through as a literal path."""
        body = "[main]\nreposdir=/etc/repos-${releasever:+raw}\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_conf(Path(tmp), "dnf.conf", body)
            with self.assertRaises(self.module.Dnf5ConfigError):
                self.module.parse_reposdir_from_config([path])

    def test_parse_reposdir_is_case_sensitive_on_option_keys(self) -> None:
        """libdnf5 ignores `Reposdir:`; the gate must reject the file's colon-delimited form."""
        body = "[main]\nReposdir: /opt/custom\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_conf(Path(tmp), "dnf.conf", body)
            with self.assertRaises(self.module.Dnf5ConfigError):
                self.module.parse_reposdir_from_config([path])

    def test_parse_reposdir_returns_none_for_a_missing_file(self) -> None:
        """A nonexistent path in the input list is skipped."""
        self.assertIsNone(self.module.parse_reposdir_from_config([Path("/nonexistent.conf")]))

    def test_runtime_reposdir_paths_returns_configured_when_set(self) -> None:
        """The on-image scan honours the configured reposdir when any config sets it."""
        body = "[main]\nreposdir = /opt/runtime-repos\n"
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            distro = directory / "distro"
            user = directory / "user"
            self._write_conf(distro, "10-distro.conf", body)
            self._write_conf(user, "90-user.conf", "[main]\n")
            main_conf = directory / "dnf.conf"
            main_conf.write_text("[main]\n", encoding="utf-8")
            with patch.object(self.module, "DNF_DISTRO_CONF_D", distro), \
                    patch.object(self.module, "DNF_USER_CONF_D", user), \
                    patch.object(self.module, "DNF_MAIN_CONF", main_conf):
                paths = self.module.runtime_reposdir_paths()
        self.assertEqual(paths, [Path("/opt/runtime-repos")])

    def _patched_dirs(self, distro: Path, user: Path, main_conf: Path):
        return (
            patch.object(self.module, "DNF_DISTRO_CONF_D", distro),
            patch.object(self.module, "DNF_USER_CONF_D", user),
            patch.object(self.module, "DNF_MAIN_CONF", main_conf),
        )

    def test_drop_ins_apply_in_file_name_order_across_dirs(self) -> None:
        """libdnf5 sorts the drop-in union by file name, not by directory."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            distro = directory / "distro"
            user = directory / "user"
            self._write_conf(distro, "zz.conf", "[main]\nreposdir = /opt/distro-zz\n")
            self._write_conf(user, "aa.conf", "[main]\nreposdir = /opt/user-aa\n")
            main_conf = directory / "dnf.conf"
            p1, p2, p3 = self._patched_dirs(distro, user, main_conf)
            with p1, p2, p3:
                files = self.module.dnf5_config_files()
                paths = self.module.runtime_reposdir_paths()
        self.assertEqual(files, [user / "aa.conf", distro / "zz.conf", main_conf])
        self.assertEqual(paths, [Path("/opt/distro-zz")])

    def test_user_drop_in_masks_same_named_distro_drop_in(self) -> None:
        """A same-named /etc drop-in masks the /usr/share one entirely."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            distro = directory / "distro"
            user = directory / "user"
            self._write_conf(distro, "aa.conf", "[main]\nreposdir = /opt/distro-aa\n")
            self._write_conf(user, "aa.conf", "[main]\n")
            main_conf = directory / "dnf.conf"
            p1, p2, p3 = self._patched_dirs(distro, user, main_conf)
            with p1, p2, p3:
                files = self.module.dnf5_config_files()
                paths = self.module.runtime_reposdir_paths()
        self.assertEqual(files, [user / "aa.conf", main_conf])
        self.assertEqual(paths, list(self.module.DEFAULT_REPOS_DIRS))

    def test_runtime_reposdir_paths_falls_back_to_defaults(self) -> None:
        """No `reposdir=` set anywhere means the documented default applies."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            main_conf = directory / "dnf.conf"
            main_conf.write_text("[main]\n", encoding="utf-8")
            no_user = directory / "no-user"; no_user.mkdir()
            no_distro = directory / "no-distro"; no_distro.mkdir()
            with patch.object(self.module, "DNF_DISTRO_CONF_D", no_distro), \
                    patch.object(self.module, "DNF_USER_CONF_D", no_user), \
                    patch.object(self.module, "DNF_MAIN_CONF", main_conf):
                paths = self.module.runtime_reposdir_paths()
        self.assertEqual(paths, list(self.module.DEFAULT_REPOS_DIRS))


class MainSectionSecurityTests(unittest.TestCase):
    """The resolved [main] block is inspected for proxy= and sslverify=0 (utah#352).

    `check_repo_sections` only inspects `.repo` sections, so a proxy= or
    sslverify=0 set globally in the [main] block of dnf.conf or a libdnf5
    drop-in is never inspected. `main_section_security_errors` resolves those
    options the way libdnf5 does (later file wins) and reports the effective
    values; an unreadable or unparseable config fails closed.
    """

    def setUp(self) -> None:
        self.module = load_module()

    def _write_conf(self, directory: Path, name: str, body: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_no_main_section_is_clean(self) -> None:
        """A config without a [main] block has no global security options to report."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = self._write_conf(directory, "10-base.conf", "[main]\n")
            self.assertEqual(
                self.module.main_section_security_errors([path], "dnf5 [main] config"),
                [],
            )

    def test_proxy_in_main_is_reported(self) -> None:
        """A proxy= in [main] is reported regardless of whether it is empty."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = self._write_conf(
                directory, "10-base.conf", "[main]\nproxy=http://localhost:3128\n")
            errors = self.module.main_section_security_errors([path], "dnf5 [main] config")
        self.assertEqual(len(errors), 1)
        self.assertIn("proxy=http://localhost:3128", errors[0])

    def test_sslverify_zero_in_main_is_reported(self) -> None:
        """An sslverify=0 in [main] is reported."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = self._write_conf(directory, "10-base.conf", "[main]\nsslverify=0\n")
            errors = self.module.main_section_security_errors([path], "dnf5 [main] config")
        self.assertEqual(len(errors), 1)
        self.assertIn("sslverify=0", errors[0])

    def test_sslverify_false_in_main_is_reported(self) -> None:
        """An sslverify=false in [main] is reported (a falsy value disables verification)."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = self._write_conf(directory, "10-base.conf", "[main]\nsslverify=false\n")
            errors = self.module.main_section_security_errors([path], "dnf5 [main] config")
        self.assertEqual(len(errors), 1)
        self.assertIn("sslverify=false", errors[0])

    def test_proxy_and_sslverify_in_main_are_both_reported(self) -> None:
        """A [main] that sets both proxy and sslverify reports both problems."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = self._write_conf(
                directory, "10-base.conf",
                "[main]\nproxy=http://localhost:3128\nsslverify=0\n",
            )
            errors = self.module.main_section_security_errors([path], "dnf5 [main] config")
        self.assertEqual(len(errors), 2)

    def test_later_file_wins_for_effective_sslverify(self) -> None:
        """A later drop-in overrides an earlier sslverify=0, so only the effective value counts."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            early = self._write_conf(directory, "00-base.conf", "[main]\nsslverify=0\n")
            late = self._write_conf(directory, "99-late.conf", "[main]\nsslverify=1\n")
            errors = self.module.main_section_security_errors([early, late], "dnf5 [main] config")
        self.assertEqual(errors, [])

    def test_later_empty_proxy_clears_earlier_proxy(self) -> None:
        """A later empty proxy= resets an earlier proxy, as libdnf5's last-wins does."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            early = self._write_conf(
                directory, "00-base.conf", "[main]\nproxy=http://localhost:3128\n"
            )
            late = self._write_conf(directory, "99-late.conf", "[main]\nproxy=\n")
            errors = self.module.main_section_security_errors([early, late], "dnf5 [main] config")
        self.assertEqual(errors, [])

    def test_malformed_config_fails_closed(self) -> None:
        """A config that cannot be parsed raises Dnf5ConfigError, like parse_reposdir_from_config."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = self._write_conf(directory, "10-base.conf", "proxy=http://localhost:3128\n")
            with self.assertRaises(self.module.Dnf5ConfigError):
                self.module.main_section_security_errors([path], "dnf5 [main] config")

    def test_sslverify_one_is_not_reported(self) -> None:
        """An sslverify=1 in [main] keeps TLS verification on, so it is not reported."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = self._write_conf(directory, "10-base.conf", "[main]\nsslverify=1\n")
            self.assertEqual(
                self.module.main_section_security_errors([path], "dnf5 [main] config"),
                [],
            )

    def test_uppercase_option_key_is_not_honoured(self) -> None:
        """dnf5 parses option keys case-sensitively, so Proxy= does not set the effective proxy."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = self._write_conf(directory, "10-base.conf", "[main]\nProxy=http://localhost:3128\n")
            self.assertEqual(
                self.module.main_section_security_errors([path], "dnf5 [main] config"),
                [],
            )

    def test_missing_file_is_clean(self) -> None:
        """A config file that does not exist contributes no options."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            missing = directory / "does-not-exist.conf"
            self.assertEqual(
                self.module.main_section_security_errors([missing], "dnf5 [main] config"),
                [],
            )


class OnImageRepoAllowlistTests(unittest.TestCase):
    """The on-image run scans dnf5's default reposdir paths (#454, #513).

    `--check` already enforces the repository allowlist against the source
    repo files in `packages/`. The Hummingbird base image ships its own repo
    files; without an on-image scan those pass into the runtime unattested.
    These tests cover the scan wired into main()'s verify-mode path: a clean
    runtime repo set passes, an enabled Fedora or unapproved repo fails, a
    disabled repo is skipped, and a repo the base ships in a default reposdir
    other than /etc/yum.repos.d (that is, /etc/distro.repos.d or
    /usr/share/dnf5/repos.d) is gated just the same (#513).
    """

    def setUp(self) -> None:
        self.module = load_module()

    def run_main(self, manifest: Path, overlay: Path, installed: set[str],
                 runtime_repos_dirs: "Path | list[Path]",
                 flavor: str = "main") -> tuple[int, str, str]:
        return run_main(
            self.module, manifest, overlay, installed,
            flavor=flavor, runtime_repos_dirs=runtime_repos_dirs,
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

    def test_a_fedora_repo_in_another_reposdir_fails(self) -> None:
        """A Fedora repo the base ships outside /etc/yum.repos.d is still gated (#513).

        dnf5 loads /etc/distro.repos.d as well as /etc/yum.repos.d, so a Fedora
        repo file the Hummingbird base places there is enabled at runtime yet
        passed by the old single-dir scan. The on-image run scans every default
        reposdir, so a bad repo in the second dir still fails.
        """
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(directory, gnome=["gnome-shell"])
            yum_repos = directory / "yum.repos.d"
            distro_repos = directory / "distro.repos.d"
            write_repo_file(
                yum_repos, "public-hummingbird-x86_64-rpms",
                baseurl="https://packages.redhat.com/api/pulp-content/public-hummingbird/x86_64/",
            )
            write_repo_file(
                distro_repos, "fedora-rawhide",
                baseurl="https://dl.fedoraproject.org/pub/fedora/linux/development/rawhide/$basearch/os/",
            )
            code, _, err = self.run_main(
                manifest, overlay, {"bash", "gnome-shell"},
                [yum_repos, distro_repos],
            )
        self.assertEqual(code, 1)
        self.assertIn("Fedora repository 'fedora-rawhide' is enabled", err)
        self.assertIn("fedora-rawhide.repo", err)

    def test_an_unapproved_repo_in_the_system_reposdir_fails(self) -> None:
        """An unapproved repo the base ships under /usr/share/dnf5/repos.d is gated (#513).

        /usr/share/dnf5/repos.d is one of dnf5's default reposdir entries, so a
        repo enabled there is live at runtime. The scan covers it, so an
        unapproved id fails even though /etc/yum.repos.d is clean.
        """
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(directory, gnome=["gnome-shell"])
            yum_repos = directory / "yum.repos.d"
            system_repos = directory / "share-dnf5-repos.d"
            write_repo_file(
                yum_repos, "public-hummingbird-x86_64-rpms",
                baseurl="https://packages.redhat.com/api/pulp-content/public-hummingbird/x86_64/",
            )
            write_repo_file(
                system_repos, "third-party",
                baseurl="https://third-party.example.com/$basearch",
            )
            code, _, err = self.run_main(
                manifest, overlay, {"bash", "gnome-shell"},
                [yum_repos, system_repos],
            )
        self.assertEqual(code, 1)
        self.assertIn("Unapproved repository 'third-party' is enabled", err)

    def test_a_clean_repo_set_across_all_reposdirs_passes(self) -> None:
        """Allowlisted repos spread across every default reposdir pass (#513)."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(
                directory,
                gnome=["gnome-shell"],
                repositories=["public-hummingbird-x86_64-rpms", "utah-packages"],
                baseurls={
                    "public-hummingbird-x86_64-rpms":
                        "https://packages.redhat.com/api/pulp-content/public-hummingbird/x86_64/",
                    "utah-packages": "file:///etc/utah-packages",
                },
            )
            yum_repos = directory / "yum.repos.d"
            distro_repos = directory / "distro.repos.d"
            system_repos = directory / "share-dnf5-repos.d"
            write_repo_file(
                yum_repos, "public-hummingbird-x86_64-rpms",
                baseurl="https://packages.redhat.com/api/pulp-content/public-hummingbird/x86_64/",
            )
            write_repo_file(
                distro_repos, "utah-packages",
                baseurl="file:///etc/utah-packages",
            )
            code, out, err = self.run_main(
                manifest, overlay, {"bash", "gnome-shell"},
                [yum_repos, distro_repos, system_repos],
            )
        self.assertEqual(code, 0, err)
        self.assertIn("All 2 contract packages are present.", out)

    def test_a_fedora_repo_in_a_configured_reposdir_fails(self) -> None:
        """A base image that sets `reposdir=` to a custom path is scanned there.

        `reposdir=` in any dnf5 [main] config replaces the documented default,
        so a Fedora repo the base ships in the configured path bypasses the
        allowlist unless the gate reads the configuration. Wire the mocked
        dnf5 config to a custom reposdir; the on-image run must derive its
        scan list from runtime_reposdir_paths() (unmocked) and fail on the
        Fedora repo the base ships there.
        """
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(directory, gnome=["gnome-shell"])
            custom_repos = directory / "custom-repos"
            write_repo_file(
                custom_repos, "public-hummingbird-x86_64-rpms",
                baseurl="https://packages.redhat.com/api/pulp-content/public-hummingbird/x86_64/",
            )
            write_repo_file(
                custom_repos, "fedora-rawhide",
                baseurl="https://dl.fedoraproject.org/pub/fedora/linux/development/rawhide/$basearch/os/",
            )
            no_user = directory / "no-user"; no_user.mkdir()
            no_distro = directory / "no-distro"; no_distro.mkdir()
            dnf_main = directory / "dnf.conf"
            dnf_main.write_text(
                "[main]\nreposdir = " + str(custom_repos) + "\n", encoding="utf-8"
            )
            # Wire the actual config lookup; runtime_reposdir_paths() is NOT
            # patched here, so the on-image run sees the configured list.
            argv = ["verify-rpm-contract.py", str(manifest), str(overlay)]
            stdout = io.StringIO()
            stderr = io.StringIO()
            report_dir = Path(tempfile.mkdtemp())
            with patch.object(self.module, "DNF_DISTRO_CONF_D", no_distro), \
                    patch.object(self.module, "DNF_USER_CONF_D", no_user), \
                    patch.object(self.module, "DNF_MAIN_CONF", dnf_main), \
                    patch.object(self.module, "is_installed",
                                 side_effect=lambda p: p in {"bash", "gnome-shell"}), \
                    patch.object(self.module, "query_packages",
                                 side_effect=lambda pkgs: (
                                     {p: info for p, info in {
                                         "bash": {"name": "bash", "epoch": "0",
                                                  "version": "5.2",
                                                  "release": "1.hum.x86_64",
                                                  "arch": "x86_64",
                                                  "nevra": "bash-5.2-1.hum.x86_64",
                                                  "origin": "hummingbird"},
                                         "gnome-shell": {"name": "gnome-shell",
                                                         "epoch": "0", "version": "51.2",
                                                         "release": "1.hum.x86_64",
                                                         "arch": "x86_64",
                                                         "nevra":
                                                             "gnome-shell-51.2-1.hum.x86_64",
                                                         "origin": "hummingbird"},
                                     }.items() if p in pkgs},
                                     [p for p in pkgs if p not in {"bash", "gnome-shell"}],
                                 )), \
                    patch.object(sys, "argv", argv), \
                    patch.dict(os.environ,
                                {"IMAGE_FLAVOR": "main",
                                 "UTAH_REPORT_DIR": str(report_dir)}), \
                    redirect_stdout(stdout), \
                    patch.object(sys, "stderr", stderr):
                code = self.module.main()
        self.assertEqual(code, 1)
        self.assertIn("Fedora repository 'fedora-rawhide' is enabled", stderr.getvalue())

    def test_an_unresolvable_dnf5_config_fails_the_gate(self) -> None:
        """A dnf5 config the gate cannot resolve fails closed instead of scanning the defaults."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(directory, gnome=["gnome-shell"])
            code, _, err = run_main(
                self.module, manifest, overlay, {"bash", "gnome-shell"},
                stderr_buffer=io.StringIO(),
                reposdir_error=self.module.Dnf5ConfigError(
                    "could not parse dnf5 config /etc/dnf/dnf.conf: bad"),
            )
        self.assertEqual(code, 1)
        self.assertIn("ERROR: could not parse dnf5 config /etc/dnf/dnf.conf", err)

    def test_a_global_proxy_in_main_fails_the_gate(self) -> None:
        """A proxy= in the resolved [main] fails the gate even for a clean runtime repo set."""
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
            dnf_main = directory / "dnf.conf"
            dnf_main.write_text("[main]\nproxy=http://localhost:3128\n")
            with patch.object(self.module, "dnf5_config_files",
                              return_value=[dnf_main]), \
                 patch.object(self.module, "is_installed",
                              side_effect=lambda p: p in {"bash", "gnome-shell"}):
                code, _, stderr = self.run_main(
                    manifest, overlay, {"bash", "gnome-shell"}, runtime_repos,
                )
        self.assertEqual(code, 1)
        self.assertIn("[main] in dnf5 [main] config sets proxy=http://localhost:3128", stderr)

    def test_a_global_sslverify_zero_in_main_fails_the_gate(self) -> None:
        """An sslverify=0 in the resolved [main] fails the gate even for a clean runtime repo set."""
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
            dnf_main = directory / "dnf.conf"
            dnf_main.write_text("[main]\nsslverify=0\n")
            with patch.object(self.module, "dnf5_config_files",
                              return_value=[dnf_main]), \
                 patch.object(self.module, "is_installed",
                              side_effect=lambda p: p in {"bash", "gnome-shell"}):
                code, _, stderr = self.run_main(
                    manifest, overlay, {"bash", "gnome-shell"}, runtime_repos,
                )
        self.assertEqual(code, 1)
        self.assertIn("[main] in dnf5 [main] config sets sslverify=0", stderr)


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
