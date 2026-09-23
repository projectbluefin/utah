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

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
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


def write_overlay(
    directory: Path,
    *,
    gnome: list[str] | None = None,
    parity: list[str] | None = None,
    services: list[str] | None = None,
    unavailable: list[str] | None = None,
    gnome_versions: dict[str, str] | None = None,
    allowed_repos: list[str] | None = None,
    factory: list[str] | None = None,
) -> Path:
    path = directory / "utah.toml"
    gnome_pkgs = gnome or []
    versions = gnome_versions if gnome_versions is not None else {p: "51" for p in gnome_pkgs}
    repos = allowed_repos if allowed_repos is not None else [
        "public-hummingbird-x86_64-rpms",
        "utah-packages",
        "nvidia-container-toolkit",
    ]
    factory_pkgs = factory if factory is not None else list(parity or [])
    versions_toml = "\n".join(f'"{k}" = "{v}"' for k, v in versions.items())
    repos_toml = ", ".join(f'"{r}"' for r in repos)
    factory_toml = ", ".join(f'"{f}"' for f in factory_pkgs)
    path.write_text(
        toml_section("gnome", gnome_pkgs)
        + (f"[gnome.versions]\n{versions_toml}\n" if versions_toml else "[gnome.versions]\n")
        + toml_section("parity", parity or [])
        + toml_section("services", services or [])
        + toml_section("unavailable", unavailable or [])
        + f"[repositories]\nallowed = [{repos_toml}]\n"
        + f"[factory]\npackages = [{factory_toml}]\n"
    )
    return path


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
                services=["tailscale"],
            )
            result = self.run_check(manifest, overlay)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Verifying 1 Bluefin packages", result.stdout)
        self.assertIn("2 GNOME desktop packages", result.stdout)
        self.assertIn("3 parity packages", result.stdout)
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


def mock_query_pkgs(pkgs: list[str], installed: set[str]):
    found = {}
    missing = []
    for p in pkgs:
        if p in installed:
            found[p] = {
                "name": p,
                "epoch": "0",
                "version": "51.0" if p == "gnome-shell" else "1.0",
                "release": "1.hum1.bfin" if p in ("gnome-shell", "fastfetch", "gh", "tailscale") else "1.hum1",
                "arch": "x86_64",
                "nevra": f"{p}-1.0.x86_64",
                "origin": "factory" if p in ("gnome-shell", "fastfetch", "gh", "tailscale") else "hummingbird",
            }
        else:
            missing.append(p)
    return found, missing


class VerifyModeTests(unittest.TestCase):
    """Without --check the verifier asserts the packages are really installed."""

    def setUp(self) -> None:
        self.module = load_module()

    def run_main(self, manifest: Path, overlay: Path, installed: set[str],
                 flavor: str = "main", report_dir: Path | None = None,
                 policy_root: Path | None = None) -> tuple[int, str]:
        argv = ["verify-rpm-contract.py", str(manifest), str(overlay)]
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as scratch:
            # The verifier retains its provenance report on disk. Without this
            # redirect these tests write into the host's /usr/share/utah.
            # UTAH_POLICY_ROOT does the same for the repository allowlist: left
            # at /, the result would depend on whatever DNF configuration the
            # machine running the tests happens to carry.
            env = {"IMAGE_FLAVOR": flavor,
                   "UTAH_REPORT_DIR": str(report_dir or Path(scratch) / "report"),
                   "UTAH_POLICY_ROOT": str(policy_root or Path(scratch) / "root")}
            with patch.object(self.module, "is_installed", side_effect=lambda p: p in installed), \
                    patch.object(self.module, "query_packages", side_effect=lambda pkgs: mock_query_pkgs(pkgs, installed)), \
                    patch.object(sys, "argv", argv), \
                    patch.dict(os.environ, env), \
                    redirect_stdout(stdout):
                code = self.module.main()
        return code, stdout.getvalue()

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

    def test_the_repository_allowlist_is_applied_to_the_attested_root(self) -> None:
        """main() really runs the repository policy, and the tests choose its root.

        Left at /, the verdict would come from whatever DNF configuration the
        machine running the tests happens to carry: green on a runner with no
        /etc/yum.repos.d, red on any Fedora host. UTAH_POLICY_ROOT makes the
        filesystem being attested part of the test.
        """
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(directory)
            policy_root = directory / "root"
            (policy_root / "etc/yum.repos.d").mkdir(parents=True)
            (policy_root / "etc/yum.repos.d/fedora.repo").write_text(
                "[fedora]\nname=Fedora\nenabled=1\n"
            )
            stderr = io.StringIO()
            with patch.object(sys, "stderr", stderr):
                code, _ = self.run_main(
                    manifest, overlay, {"bash"}, policy_root=policy_root
                )
            self.assertEqual(code, 1)
            self.assertIn("Fedora repository 'fedora' is enabled", stderr.getvalue())

            # The same run against a root carrying only approved repositories passes.
            (policy_root / "etc/yum.repos.d/fedora.repo").unlink()
            (policy_root / "etc/yum.repos.d/utah-packages.repo").write_text(
                "[utah-packages]\nname=utah\nenabled=1\n"
            )
            code, out = self.run_main(
                manifest, overlay, {"bash"}, policy_root=policy_root
            )
        self.assertEqual(code, 0, out)


class ProvenanceReportTests(unittest.TestCase):
    """The retained report is a contract criterion, so it is asserted, not assumed.

    It is also the reason these tests route the writer through UTAH_REPORT_DIR:
    the verifier's own default is /usr/share/utah, which a test run must never
    touch -- unprivileged that is a permission error, as root it is host
    pollution.
    """

    def setUp(self) -> None:
        self.module = load_module()

    def run_main(self, manifest: Path, overlay: Path, installed: set[str],
                 report_dir: Path) -> tuple[int, str]:
        argv = ["verify-rpm-contract.py", str(manifest), str(overlay)]
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as scratch, \
                patch.object(self.module, "is_installed", side_effect=lambda p: p in installed), \
                patch.object(self.module, "query_packages",
                             side_effect=lambda pkgs: mock_query_pkgs(pkgs, installed)), \
                patch.object(sys, "argv", argv), \
                patch.dict(os.environ, {"IMAGE_FLAVOR": "main",
                                        "UTAH_REPORT_DIR": str(report_dir),
                                        "UTAH_POLICY_ROOT": str(Path(scratch) / "root")}), \
                redirect_stdout(stdout):
            code = self.module.main()
        return code, stdout.getvalue()

    def test_the_report_is_written_where_the_run_is_told_to_put_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(directory, gnome=["gnome-shell"])
            report_dir = directory / "report"
            code, out = self.run_main(manifest, overlay, {"bash", "gnome-shell"}, report_dir)

            self.assertEqual(code, 0, out)
            self.assertTrue((report_dir / "package-origins.txt").is_file())
            report = json.loads((report_dir / "package-origins.json").read_text())

        self.assertEqual(report["build_provenance"]["contract_packages"], 2)
        self.assertEqual(report["packages"]["gnome-shell"]["section"], "gnome")
        self.assertEqual(report["packages"]["bash"]["section"], "bluefin")
        self.assertIn(str(report_dir / "package-origins.json"), out)

    def test_a_report_that_cannot_be_written_fails_the_build(self) -> None:
        """Fail closed: a warning here would pass a build that proved nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = write_overlay(directory)
            # A file, not a directory: mkdir on it raises OSError.
            blocked = directory / "blocked"
            blocked.write_text("")
            stderr = io.StringIO()
            with patch.object(sys, "stderr", stderr):
                code, out = self.run_main(manifest, overlay, {"bash"}, blocked)

        self.assertEqual(code, 1, out)
        self.assertIn("could not retain provenance report", stderr.getvalue())


class MissingOverlayTests(unittest.TestCase):
    """A missing overlay is reported, not raised as a bare FileNotFoundError.

    The guard used to sit below the first read of the file, so it never ran.
    """

    def setUp(self) -> None:
        self.module = load_module()

    def test_a_missing_overlay_is_a_named_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest = write_manifest(directory, ["bash"])
            overlay = directory / "utah.toml"
            argv = ["verify-rpm-contract.py", str(manifest), str(overlay)]
            stderr = io.StringIO()
            with patch.object(sys, "argv", argv), \
                    patch.dict(os.environ, {"IMAGE_FLAVOR": "main"}), \
                    patch.object(sys, "stderr", stderr), \
                    redirect_stdout(io.StringIO()):
                code = self.module.main()
        self.assertEqual(code, 1)
        self.assertIn(f"Overlay manifest '{overlay}' does not exist", stderr.getvalue())


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
            env = {"IMAGE_FLAVOR": "main", "UTAH_REPORT_DIR": str(Path(tmp) / "report"),
                   "UTAH_POLICY_ROOT": str(Path(tmp) / "root")}
            with patch.object(self.module, "Path", redirected), \
                    patch.object(self.module, "is_installed",
                                 side_effect=lambda p: p in installed), \
                    patch.object(self.module, "query_packages",
                                 side_effect=lambda pkgs: mock_query_pkgs(pkgs, installed)), \
                    patch.object(sys, "argv", argv), \
                    patch.dict(os.environ, env), \
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
                services=["tailscale"],
            )
            installed = {"bash", "coreutils", "gnome-shell", "fastfetch", "gh", "tailscale"}
            code, out = self.run_main_with_contract(
                "bash coreutils gnome-shell fastfetch gh tailscale",
                manifest, overlay, installed,
            )
        self.assertEqual(code, 0, out)
        self.assertIn("Verifying 2 Bluefin packages", out)
        self.assertIn("1 GNOME desktop packages", out)
        self.assertIn("2 parity packages", out)
        self.assertIn("1 desktop service packages", out)
        self.assertIn("All 6 contract packages are present.", out)

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
            if list(cmd[:3]) == ["rpm", "-q", "--qf"]:
                lines = [f"{pkg}|0|1.17.4|1.hum1.bfin|x86_64\n" for pkg in cmd[3:]]
                return subprocess.CompletedProcess(cmd, 0, stdout="".join(lines))
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        argv = ["verify-rpm-contract.py", str(self.manifest), str(self.overlay)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(self.module, "Path", redirected), \
                patch.object(self.module, "is_installed", return_value=True), \
                patch.object(self.module.subprocess, "run", fake_run), \
                patch.object(sys, "argv", argv), \
                patch.dict(os.environ, {"IMAGE_FLAVOR": flavor,
                                        "UTAH_REPORT_DIR": str(root / "usr/share/utah"),
                                        "UTAH_POLICY_ROOT": str(root)}), \
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
