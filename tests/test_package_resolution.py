"""The preflight must exercise the install contract and fail closed."""

import contextlib
import importlib.util
import hashlib
import io
import re
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load("install-packages")
checker = load("check-repo-availability")


class PackageResolutionTests(unittest.TestCase):
    def test_metadata_digest_is_verified(self):
        raw = b"metadata"
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        self.assertEqual(checker.verified_bytes(raw, digest), raw)
        with self.assertRaises(ValueError):
            checker.verified_bytes(b"changed", digest)

    def metadata_archive(self, name):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            entry = tarfile.TarInfo(name)
            entry.size = len(b"<repomd/>")
            archive.addfile(entry, io.BytesIO(b"<repomd/>"))
        return stream.getvalue()

    def test_metadata_layer_extracts_only_repository_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            checker.unpack_metadata(self.metadata_archive("repository/repodata/repomd.xml"), Path(tmp))
            self.assertEqual((Path(tmp) / "repodata/repomd.xml").read_bytes(), b"<repomd/>")

    def test_metadata_layer_rejects_paths_outside_repodata(self):
        for name in ("../outside", "/absolute", "repository/payload.rpm"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    checker.unpack_metadata(self.metadata_archive(name), Path(tmp))

    def test_containerfile_installs_scripts_into_absent_destination(self):
        text = (ROOT / "Containerfile").read_text()
        loop = re.search(r"RUN (for pair in .*?\bdone) &&", text, re.S).group(1)
        pairs = re.findall(r"([\w.-]+):(utah-[\w.-]+)", loop)
        self.assertTrue(pairs)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "sources"
            destination = Path(tmp) / "missing" / "libexec"
            source.mkdir()
            for name, _ in pairs:
                (source / name).write_bytes((ROOT / "scripts" / name).read_bytes())
            script = loop.replace("/tmp/utah-scripts", str(source)).replace(
                "/usr/local/libexec", str(destination))
            subprocess.run(["bash", "-eu", "-c", script], check=True)
            self.assertEqual({p.name for p in destination.iterdir()}, {p[1] for p in pairs})
            for name, installed in pairs:
                self.assertEqual((source / name).read_bytes(), (destination / installed).read_bytes())

    def resolve(self, output, code=1):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "bluefin.toml"
            overlay = Path(tmp) / "utah.toml"
            base.write_text('[fedora]\npackages=["base", "unavailable"]\n'
                            '[fedora_v44]\npackages=["release-specific"]\n')
            overlay.write_text('[gnome]\npackages=["shell"]\n'
                               '[parity]\npackages=["manpages"]\n'
                               '[services]\npackages=["resolver"]\n'
                               '[build]\npackages=["compiler"]\n'
                               '[unavailable]\npackages=["unavailable"]\n')
            with patch("sys.argv", ["install", "--resolve", str(base), str(overlay)]), \
                 patch.object(installer, "fedora_major", return_value="44"), \
                 patch.object(installer, "dnf_path", return_value="dnf5"), \
                 patch.object(installer.subprocess, "run", return_value=
                              subprocess.CompletedProcess([], code, stdout=output)) as run:
                rc = installer.main()
                return rc, run.call_args.args[0]

    def test_valid_declined_transaction_includes_every_install_section(self):
        rc, command = self.resolve("Transaction Summary:\nInstall 12 Packages\nOperation aborted.\n")
        self.assertEqual(rc, 0)
        self.assertEqual(command[command.index("install") + 1:],
                         ["base", "release-specific", "shell", "manpages", "resolver",
                          "compiler"])
        self.assertIn("--assumeno", command)
        self.assertIn("--disablerepo=*", command)
        for repo in installer.REPOS:
            self.assertIn(f"--enablerepo={repo}", command)

    def test_resolution_failures_do_not_pass(self):
        for output in ("nothing provides libmissing.so.1\n",
                       "No match for argument: compiler\nTransaction Summary\n",
                       "Error: Failed to download metadata\n",
                       "Operation aborted.\n", ""):
            with self.subTest(output=output):
                self.assertEqual(self.resolve(output)[0], 1)

    def test_unexpected_exit_code_fails_even_with_summary(self):
        self.assertEqual(self.resolve("Transaction Summary\n", code=125)[0], 1)

    def test_already_installed_contract_passes(self):
        self.assertEqual(self.resolve("Nothing to do.\n", code=0)[0], 0)

    def test_pins_come_from_containerfile(self):
        base, packages = checker.pinned_inputs(ROOT / "Containerfile")
        self.assertIn("@sha256:", base)
        self.assertIn("@sha256:", packages)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Containerfile"
            path.write_text("ARG BASE_IMAGE=example/base:latest\n"
                            "ARG PACKAGE_IMAGE=example/packages\n"
                            f"ARG PACKAGE_IMAGE_SHA=sha256:{'1' * 64}\n")
            with self.assertRaises(ValueError):
                checker.pinned_inputs(path)

    def test_install_repos_derived_from_packages(self):
        repos = installer.install_repos(ROOT / "packages")
        self.assertEqual(repos, ("utah-packages", "public-hummingbird-x86_64-rpms"))

    def test_install_repos_priority_and_filtering(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirpath = Path(tmp)
            (dirpath / "a.repo").write_text("[low-prio]\n# utah-install: true\npriority=50\n")
            (dirpath / "b.repo").write_text("[high-prio]\n# utah-install: true\npriority=5\n")
            (dirpath / "c.repo").write_text("[unmarked]\npriority=1\n")
            self.assertEqual(installer.install_repos(dirpath), ("high-prio", "low-prio"))

    def test_install_repos_priority_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirpath = Path(tmp)
            (dirpath / "a.repo").write_text("[low-prio]\n# utah-install: true\npriority = 50\n")
            (dirpath / "b.repo").write_text("[high-prio]\n# utah-install: true\npriority  =  5\n")
            self.assertEqual(installer.install_repos(dirpath), ("high-prio", "low-prio"))

    def test_install_repos_marker_above_or_below_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirpath = Path(tmp)
            (dirpath / "a.repo").write_text("# utah-install: true\n[above-header]\npriority=10\n")
            (dirpath / "b.repo").write_text("[below-header]\n# utah-install: true\npriority=20\n")
            (dirpath / "multi.repo").write_text("[unmarked]\npriority=1\n# utah-install: true\n[second-marked]\npriority=5\n")
            self.assertEqual(installer.install_repos(dirpath), ("second-marked", "above-header", "below-header"))

    def test_install_repos_empty_or_no_marked_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirpath = Path(tmp)
            (dirpath / "unmarked.repo").write_text("[unmarked]\nname=unmarked\n")
            with self.assertRaises(ValueError):
                installer.install_repos(dirpath)

    def test_check_requires_hummingbird_and_utah_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirpath = Path(tmp)
            base = dirpath / "bluefin.toml"
            overlay = dirpath / "utah.toml"
            base.write_text('[fedora]\npackages=["base"]\n')
            overlay.write_text('[gnome]\npackages=[]\n')
            
            # Missing hummingbird
            repos_dir = dirpath / "repos"
            repos_dir.mkdir()
            (repos_dir / "u.repo").write_text("[utah-packages]\n# utah-install: true\n")
            with patch("sys.argv", ["install", "--check", "--repos-dir", str(repos_dir), str(base), str(overlay)]):
                with self.assertRaises(ValueError) as ctx:
                    installer.main()
                self.assertIn("public-hummingbird-x86_64-rpms", str(ctx.exception))


class ParityContractTests(unittest.TestCase):
    MANIFEST = ROOT / "packages/bluefin.toml"
    OVERLAY = ROOT / "packages/utah.toml"

    def test_parity_section_reaches_the_install_set(self):
        contract = installer.contract(ROOT / "packages/bluefin.toml", self.OVERLAY, "44")
        for pkg in installer.section(self.OVERLAY, "parity"):
            with self.subTest(package=pkg):
                self.assertIn(pkg, contract)

    def test_parity_section_duplicates_nothing_but_the_build_tooling_it_keeps(self):
        # A name both in [parity] and in bluefin.toml or another overlay section
        # is a duplicate claim the verifier rejects. unzip is the one deliberate
        # overlap with [build]: configure-services.sh removes the build tooling
        # after the extension build and has to keep unzip for the same reason
        # it is listed here.
        parity = installer.section(self.OVERLAY, "parity")
        self.assertEqual(len(set(parity)), len(parity))
        others = set(installer.section(ROOT / "packages/bluefin.toml", "fedora"))
        for name in ("gnome", "services", "unavailable"):
            others |= set(installer.section(self.OVERLAY, name))
        self.assertEqual(sorted(set(parity) & others), [])
        self.assertEqual(sorted(set(parity) & set(installer.section(self.OVERLAY, "build"))), ["unzip"])
        removal = [line for line in (ROOT / "scripts/configure-services.sh").read_text().splitlines()
                   if "remove --no-autoremove" in line]
        self.assertEqual(len(removal), 1)
        self.assertNotIn("unzip", removal[0])

    def test_verifier_asserts_the_parity_section(self):
        """The parity packages must reach the verifier's expected set.

        This used to grep the verifier's source text for
        `parity = section(overlay, "parity")`, which passed whether or not the
        code ran. Executed coverage for the verifier lives in
        tests/test_verify_rpm_contract.py; this asserts the specific claim the
        grep was standing in for.
        """
        verifier = load("verify-rpm-contract")
        parity = verifier.section(self.OVERLAY, "parity")
        self.assertTrue(parity, "the shipped overlay declares no parity packages")
        target = parity[0]
        argv = ["verify-rpm-contract.py", str(self.MANIFEST), str(self.OVERLAY)]
        stderr = io.StringIO()
        with patch.object(verifier, "is_installed", side_effect=lambda p: p != target), \
                patch.object(verifier.sys, "argv", argv), \
                patch.dict(verifier.os.environ, {"IMAGE_FLAVOR": "main"}), \
                patch.object(verifier.sys, "stderr", stderr), \
                contextlib.redirect_stdout(io.StringIO()):
            code = verifier.main()
        self.assertEqual(code, 1)
        self.assertIn(f"  - {target}\n", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
