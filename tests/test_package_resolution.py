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
                               '[hardware]\npackages=["firmware"]\n'
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
                         ["base", "release-specific", "shell", "manpages", "firmware",
                          "resolver", "compiler"])
        self.assertIn("--assumeno", command)
        self.assertIn("--disablerepo=*", command)
        for repo in installer.REPOS:
            self.assertIn(f"--enablerepo={repo}", command)

    def test_libxml2_split_is_excluded_by_name(self):
        # libxml2-16 is the soname-16 build Hummingbird's libxml2-devel pins
        # (Requires: libxml2-16(x86-64)); the plain libxml2 conflicts on disk
        # with it. Keep libxml2-16, exclude the plain name so the soname is
        # still satisfied and the transaction no longer aborts
        # (projectbluefin/utah#248).
        self.assertEqual(installer.EXCLUDES, ("PackageKit*", "libxml2"))
        rc, command = self.resolve(
            "Transaction Summary:\nInstall 12 Packages\nOperation aborted.\n")
        self.assertEqual(rc, 0)
        self.assertIn("-x", command)
        # Each exclusion is its own -x, so dnf never reads a pattern as an
        # install target.
        start = command.index("-x")
        self.assertEqual(command[start:start + 4], ["-x", "PackageKit*", "-x", "libxml2"])

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

    def test_parity_ref_exists_and_contains_valid_commit_sha(self):
        ref_file = ROOT / "packages/.bluefin-parity-ref"
        self.assertTrue(ref_file.is_file(), "packages/.bluefin-parity-ref must exist")
        ref = ref_file.read_text().strip()
        self.assertRegex(
            ref,
            r"^[0-9a-f]{40}$",
            "packages/.bluefin-parity-ref must contain a 40-character hex SHA",
        )

    def test_check_parity_recipe_guards_the_ref_format(self):
        justfile = (ROOT / "Justfile").read_text()
        self.assertIn("packages/.bluefin-parity-ref", justfile)
        self.assertRegex(
            justfile,
            r'\[\[\s*!\s*"\$ref"\s*=~\s*\^\[0-9a-f\]\{40\}\$\s*\]\]',
            "Justfile check-parity recipe must validate the SHA format before fetching",
        )


class ImageSizeTests(unittest.TestCase):
    MOUNT = "--mount=type=bind,from=packages,source=/repository,target=/etc/utah-packages,ro"

    def test_package_repository_is_mounted_not_copied(self):
        # #130: a COPY put the whole 4 GB repository into every image and ISO.
        source = (ROOT / "Containerfile").read_text()
        self.assertNotIn("COPY --from=packages", source)
        steps = [step for step in source.split("\nRUN ") if step.startswith(self.MOUNT)]
        self.assertEqual(len(steps), 2, "both install steps must mount the repository")
        self.assertIn("utah-install-packages", steps[0])
        self.assertIn("utah-install-ogc-kernel", steps[1])
        self.assertIn("utah-install-nvidia", steps[1])
        # Nothing installs after the flavor step, so it is the one that turns
        # the repository file off for the image's lifetime.
        self.assertIn("sed -i 's/^enabled=1$/enabled=0/' /etc/yum.repos.d/utah-packages.repo", steps[1])

    def test_package_repository_file_is_enabled_only_during_the_build(self):
        text = (ROOT / "packages/utah-packages.repo").read_text()
        self.assertIn("enabled=1", text)
        self.assertIn("baseurl=file:///etc/utah-packages", text)
        self.assertIn("utah-packages", installer.REPOS)

    def test_hummingbird_packages_are_signature_checked(self):
        text = (ROOT / "packages/hummingbird.repo").read_text()
        self.assertIn("gpgcheck=1", text)
        self.assertIn("gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-redhat-release-2", text)
        key = (ROOT / "packages/RPM-GPG-KEY-redhat-release-2").read_text()
        self.assertIn("BEGIN PGP PUBLIC KEY BLOCK", key)
        for containerfile in ("Containerfile", "Containerfile.kernel"):
            self.assertIn("COPY packages/RPM-GPG-KEY-redhat-release-2 /etc/pki/rpm-gpg/",
                          (ROOT / containerfile).read_text(), containerfile)

    def test_live_initramfs_build_fails_on_a_dracut_error(self):
        source = (ROOT / "iso/live/Containerfile").read_text()
        self.assertIn("mkdir -p /var/roothome", source)
        self.assertIn("set -euxo pipefail", source)
        self.assertIn("dracut\\[E\\]: FAILED", source)


class DesktopUnitEnablementTests(unittest.TestCase):
    """A build-time enablement with no preset line behind it does not survive.

    bootc applies systemd presets on first boot, so `systemctl enable` at
    build time is undone unless 85-utah-desktop.preset agrees. That trap is
    documented in configure-services.sh's sshd branch and was confirmed from
    the other side by projectbluefin/utah#98 and #99. Hold the two files in
    agreement so units enabled at build time cannot silently omit the preset.
    """

    PRESET = ROOT / (
        "system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset"
    )
    SERVICES = ROOT / "scripts/configure-services.sh"

    def preset_directives(self, verb):
        return {
            line.split()[1]
            for line in self.PRESET.read_text().splitlines()
            if line.startswith(f"{verb} ")
        }

    def script_units(self, function):
        # Leading whitespace matters: sshd is enabled inside a conditional
        # block, so an anchored pattern misses it and the allowlist below
        # would look stale when it is not.
        return set(
            re.findall(rf"^[ \t]*{function} (\S+)$", self.SERVICES.read_text(), re.M)
        )

    # sshd is deliberately excluded: configure-services.sh rewrites the preset
    # in place for the opt-in debug build rather than shipping it enabled,
    # which is the one case where the two files may legitimately disagree.
    #
    # The other three predate this test and are NOT asserted to be correct.
    # They come from ublue packages that may ship their own vendor presets, in
    # which case Utah's preset has nothing to add -- but that was not verified
    # here, because it needs the built image rather than the source tree. They
    # are listed so the guard below can be exact about what it does not yet
    # cover, instead of being weakened into passing for everything. If one of
    # them turns out to have no preset behind it either, it is the same bug as
    # #98 and belongs in the preset.
    WITHOUT_PRESET = {
        "sshd.service",
        "brew-setup.service",
        "flatpak-nuke-fedora.service",
        "flatpak-preinstall.service",
    }

    def test_no_new_unit_is_enabled_without_a_preset_entry(self):
        enabled = self.script_units("enable_unit") - self.WITHOUT_PRESET
        missing = sorted(enabled - self.preset_directives("enable"))
        self.assertEqual(
            missing,
            [],
            "enabled at build time with no preset entry, so bootc's first-boot "
            f"preset application will undo it: {missing}",
        )

    def test_the_exception_list_does_not_cover_absent_units(self):
        # A stale allowlist silently widens the hole above. Every name in it
        # must still be a unit configure-services.sh actually enables.
        enabled = self.script_units("enable_unit")
        stale = sorted(self.WITHOUT_PRESET - enabled)
        self.assertEqual(stale, [], f"allowlisted but no longer enabled: {stale}")

    def test_the_reported_desktop_units_are_enabled(self):
        # projectbluefin/utah#99: input-remapper package was installed and its
        # unit never started. Name them so a refactor cannot drop one.
        for unit in ("input-remapper.service",):
            with self.subTest(unit=unit):
                self.assertIn(unit, self.script_units("enable_unit"))
                self.assertIn(unit, self.preset_directives("enable"))


if __name__ == "__main__":
    unittest.main()
