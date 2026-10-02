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
import tomllib
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
        loop = re.search(r"^RUN (?:--mount=\S+ \\\n\s+)?(for pair in .*?\bdone) &&",
                         text, re.S | re.M).group(1)
        pairs = re.findall(r"([\w.-]+):(utah-[\w.-]+)", loop)
        self.assertTrue(pairs)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "sources"
            destination = Path(tmp) / "missing" / "libexec"
            source.mkdir()
            for name, _ in pairs:
                (source / name).write_bytes((ROOT / "scripts" / name).read_bytes())
            script = loop.replace("/tmp/utah-scripts", str(source)).replace(
                "/usr/libexec", str(destination))
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

    def test_factory_pin_stamp_matches_containerfile(self):
        arg = re.search(r"^ARG PACKAGE_IMAGE_SHA=(sha256:[0-9a-f]{64})$",
                        (ROOT / "Containerfile").read_text(), re.M)
        self.assertIsNotNone(arg)
        stamp = re.search(r"^# factory-pin: (sha256:[0-9a-f]{64})$",
                          (ROOT / "packages" / "utah-packages.repo").read_text(), re.M)
        self.assertIsNotNone(stamp,
                             "packages/utah-packages.repo lost its '# factory-pin:' stamp")
        self.assertEqual(
            stamp.group(1), arg.group(1),
            "A pin bump must move the Containerfile ARG and the .repo stamp together: "
            "the stamp is the transaction's layer-cache key (#371).")

    def test_factory_digest_label_names_the_pin(self):
        text = (ROOT / "Containerfile").read_text()
        self.assertIn('LABEL io.projectbluefin.utah.factory-digest="${PACKAGE_IMAGE_SHA}"', text)
        # A global ARG is only in scope for FROM lines; the main stage must
        # re-declare it bare or the label bakes empty.
        self.assertIsNotNone(
            re.search(r"^ARG PACKAGE_IMAGE_SHA\s*$", text, re.M),
            "main stage lost its bare 'ARG PACKAGE_IMAGE_SHA' re-declaration, "
            "so the factory-digest label would bake empty")

    def test_evr_map_skips_unparseable_and_normalizes_epoch(self):
        lines = ["gnome-shell x86_64 (none):51.0-1.hum1.bfin",
                 "gdm x86_64 1:51.0-1.hum1.bfin",
                 "package mutter is not installed"]
        self.assertEqual(installer.evr_map(lines), {
            ("gnome-shell", "x86_64"): "0:51.0-1.hum1.bfin",
            ("gdm", "x86_64"): "1:51.0-1.hum1.bfin",
        })

    def test_repo_evr_returns_none_when_the_repository_is_absent(self):
        with patch.object(installer.subprocess, "run", return_value=
                          subprocess.CompletedProcess([], 1, stdout="", stderr="Error")) as run:
            self.assertIsNone(installer.repo_evr("dnf5", "utah-packages", ["shell"]))
            command = run.call_args.args[0]
            self.assertIn("repoquery", command)
            self.assertIn("--enablerepo=utah-packages", command)
            self.assertIn("--latest-limit=1", command)
        with patch.object(installer.subprocess, "run", return_value=
                          subprocess.CompletedProcess(
                              [], 0, stdout="shell x86_64 0:1.0-1.fc44\n")):
            self.assertEqual(installer.repo_evr("dnf5", "utah-packages", ["shell"]),
                             {("shell", "x86_64"): "0:1.0-1.fc44"})

    def test_containerfile_pins_generic_logos(self):
        text = (ROOT / "Containerfile").read_text()
        url = re.search(r"^ARG GENERIC_LOGOS_URL=(\S+)$", text, re.M)
        self.assertIsNotNone(url)
        self.assertTrue(url.group(1).endswith(".noarch.rpm"), url.group(1))
        sha = re.search(r"^ARG GENERIC_LOGOS_SHA256=([0-9a-f]{64})$", text, re.M)
        self.assertIsNotNone(sha)
        self.assertIn("curl -fsSL \"${GENERIC_LOGOS_URL}\" -o /tmp/generic-logos.rpm", text)
        self.assertIn('echo "${GENERIC_LOGOS_SHA256}  /tmp/generic-logos.rpm" | sha256sum --check --strict', text)

    def test_swap_distro_logos_runs_erase_install_erase(self):
        with tempfile.TemporaryDirectory() as tmp:
            rpm = Path(tmp) / "generic-logos.rpm"
            rpm.touch()
            with patch.object(installer.subprocess, "run", side_effect=[
                    subprocess.CompletedProcess([], 0, stdout="fedora-logos"),
                    subprocess.CompletedProcess([], 0, stdout=""),
                    subprocess.CompletedProcess([], 0, stdout=""),
                    subprocess.CompletedProcess([], 0, stdout=""),
                    subprocess.CompletedProcess([], 0, stdout=""),
            ]) as run:
                self.assertEqual(installer.swap_distro_logos(rpm, Path(tmp)), 0)
            argv = [call.args[0] for call in run.call_args_list]
            self.assertEqual(len(argv), 5)
            self.assertEqual(argv[1][:4], ("rpm", "--erase", "--nodeps", "fedora-logos"))
            self.assertEqual(argv[2][:3], ("rpm", "--install", str(rpm)))
            self.assertEqual(argv[3], ("rpm", "--erase", "--nodeps", "--nodb", "generic-logos"))

    def test_swap_skips_when_fedora_logos_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            rpm = Path(tmp) / "generic-logos.rpm"
            rpm.touch()
            with patch.object(installer.subprocess, "run", return_value=
                              subprocess.CompletedProcess([], 0, stdout="")) as run:
                self.assertEqual(installer.swap_distro_logos(rpm, Path(tmp)), 0)
            for call in run.call_args_list:
                self.assertEqual(call.args[0][:2], ["rpm", "-qa"])

    def test_swap_fails_when_a_logo_file_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            rpm = Path(tmp) / "generic-logos.rpm"
            rpm.touch()
            (Path(tmp) / "fedora-gdm-logo.png").touch()
            with patch.object(installer.subprocess, "run", side_effect=[
                    subprocess.CompletedProcess([], 0, stdout="fedora-logos"),
                    subprocess.CompletedProcess([], 0, stdout=""),
                    subprocess.CompletedProcess([], 0, stdout=""),
                    subprocess.CompletedProcess([], 0, stdout=""),
                    subprocess.CompletedProcess([], 0, stdout=""),
            ]):
                self.assertEqual(installer.swap_distro_logos(rpm, Path(tmp)), 1)

    def test_swap_fails_when_the_rpm_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(installer.subprocess, "run") as run:
                self.assertEqual(
                    installer.swap_distro_logos(Path(tmp) / "absent.rpm", Path(tmp)), 1)
            run.assert_not_called()

    def test_find_skew_names_mismatches_only(self):
        have = {("gnome-shell", "x86_64"): "0:51~beta-1.hum1.bfin",
                ("mutter", "x86_64"): "0:51.0-1.hum1.bfin",
                ("removed", "x86_64"): "0:1.0-1.hum1.bfin"}
        offered = {("gnome-shell", "x86_64"): "0:51.0-1.hum1.bfin",
                   ("mutter", "x86_64"): "0:51.0-1.hum1.bfin",
                   ("uninstalled", "x86_64"): "0:2.0-1.hum1.bfin"}
        skew = installer.find_skew(have, offered)
        self.assertEqual(len(skew), 1)
        self.assertIn("gnome-shell.x86_64", skew[0])
        self.assertIn("51~beta", skew[0])
        self.assertIn("51.0-1", skew[0])
        self.assertEqual(installer.find_skew(
            {("mutter", "x86_64"): "0:51.0-1.hum1.bfin"}, offered), [])

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


class ResolveOneTests(unittest.TestCase):
    """--resolve-one is the single-name probe the unavailable-entry gate loops over."""

    def probe(self, output, code=1):
        with patch("sys.argv", ["install", "--resolve-one", "candidate"]), \
             patch.object(installer, "dnf_path", return_value="dnf5"), \
             patch.object(installer, "install_repos", return_value=("utah-packages",)), \
             patch.object(installer.subprocess, "run", return_value=
                          subprocess.CompletedProcess([], code, stdout=output)) as run, \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            return installer.main(), run.call_args.args[0], stdout.getvalue()

    def test_resolving_package_exits_zero_with_a_marker(self):
        rc, command, log = self.probe(
            "Transaction Summary:\nInstall 1 Package\nOperation aborted.\n")
        self.assertEqual(rc, 0)
        self.assertEqual(command, ["dnf5", "--assumeno", "--disablerepo=*",
                                   "--enablerepo=utah-packages",
                                   "-x", "PackageKit*", "install", "candidate"])
        self.assertIn("UTAH_RESOLVE_ONE candidate 0", log.splitlines())

    def test_already_installed_counts_as_resolves(self):
        # A package the base already carries is the most stale an
        # [unavailable] entry can be: it resolves trivially.
        rc, _, log = self.probe("Nothing to do.\n", code=0)
        self.assertEqual(rc, 0)
        self.assertIn("UTAH_RESOLVE_ONE candidate 0", log.splitlines())

    def test_unresolvable_package_exits_one(self):
        for output in ("No match for argument: candidate\n",
                       "nothing provides libmissing.so.1\n",
                       "Error: Failed to download metadata\n",
                       "Operation aborted.\n"):
            with self.subTest(output=output):
                rc, _, log = self.probe(output)
                self.assertEqual(rc, 1)
                self.assertIn("UTAH_RESOLVE_ONE candidate 1", log.splitlines())

    def test_unexpected_exit_code_fails_even_with_summary(self):
        rc, _, log = self.probe("Transaction Summary\n", code=125)
        self.assertEqual(rc, 1)
        self.assertIn("UTAH_RESOLVE_ONE candidate 1", log.splitlines())

    def test_full_dnf_output_stays_in_the_log(self):
        rc, _, log = self.probe("No match for argument: candidate\n")
        self.assertEqual(rc, 1)
        self.assertIn("No match for argument: candidate", log)

    def test_cannot_combine_with_resolve_or_check(self):
        for flag in ("--resolve", "--check"):
            with self.subTest(flag=flag), \
                 patch("sys.argv", ["install", "--resolve-one", "candidate",
                                    flag, "bluefin.toml", "utah.toml"]), \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    installer.main()


class UnavailableContractTests(unittest.TestCase):
    """[unavailable] is documented parity debt: every entry carries a tracking
    issue, none of it is in the install set, and the deliberate exclusions
    are policy rather than accidents."""

    OVERLAY = ROOT / "packages/utah.toml"

    def names(self):
        return checker.unavailable_names(self.OVERLAY)

    def section_text(self):
        # Line-based: an earlier section's prose mentions "[unavailable]",
        # so a substring split would cut mid-comment.
        lines = self.OVERLAY.read_text().splitlines()
        start = lines.index("[unavailable]")
        end = next((index for index in range(start + 1, len(lines))
                    if lines[index].startswith("[")), len(lines))
        return "\n".join(lines[start:end])

    def comment_block(self, name):
        """The #-comment block whose header names this entry.

        Headers start at column zero in lowercase ("# anaconda-live,
        slitherer"); body lines are indented, blank, or lone "#", so the
        block runs to the next header, blank line, or non-comment line.
        """
        lines = self.section_text().splitlines()
        starts = [index for index, line in enumerate(lines)
                  if re.search(rf"^#.*\b{re.escape(name)}\b", line)]
        self.assertTrue(starts, f"{name} has no comment block in [unavailable]")
        block = []
        for line in lines[starts[0] + 1:]:
            if not line.startswith("#") or re.match(r"^# [a-z0-9]", line):
                break
            block.append(line)
        return "\n".join([lines[starts[0]], *block])

    def test_fish_stays_excluded(self):
        self.assertIn("fish", self.names(),
                      "Bluefin classic shipping every shell was a mistake Utah "
                      "does not repeat (bare-metal audit #382)")

    def test_firefox_rpm_stays_excluded(self):
        self.assertIn("firefox", self.names(),
                      "Utah ships the browser as the org.mozilla.firefox Flatpak")

    def test_deliberate_exclusions_are_all_in_the_overlay(self):
        self.assertLessEqual(set(checker.DELIBERATELY_EXCLUDED), set(self.names()),
                             "a deliberate exclusion left [unavailable] without "
                             "repealing the policy")

    def test_every_entry_carries_a_tracking_issue(self):
        for name in self.names():
            with self.subTest(name=name):
                self.assertIsNotNone(re.search(r"#\d+", self.comment_block(name)),
                                     f"{name} lost its tracking issue")

    def test_unavailable_entries_are_absent_from_the_install_contract(self):
        # The raw Bluefin sections legitimately list these names -- that is
        # what makes them parity debt. The assertion belongs on contract(),
        # which must subtract every one of them on every Fedora major the
        # manifest defines a section for.
        base = ROOT / "packages/bluefin.toml"
        majors = [None] + [section.removeprefix("fedora_v")
                           for section in tomllib.loads(base.read_text())
                           if section.startswith("fedora_v")]
        install = set(installer.section(self.OVERLAY, "build"))
        for major in majors:
            install |= set(installer.contract(base, self.OVERLAY, major))
        overlap = sorted(set(self.names()) & install)
        self.assertEqual(overlap, [],
                         f"[unavailable] entries still installed: {overlap}")


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
                   if "-y remove" in line]
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
        """Verifies the #152 pinned-parity-ref invariant.

        These assertions belong to the pinned-ref work (#152), not the
        contract-composition change that bundled them in; they pin the
        parity revision the build must track. Kept in ParityContractTests
        because that is where the parity file is read.
        """
        ref_file = ROOT / "packages/.bluefin-parity-ref"
        self.assertTrue(ref_file.is_file(), "packages/.bluefin-parity-ref must exist")
        ref = ref_file.read_text().strip()
        self.assertRegex(
            ref,
            r"^[0-9a-f]{40}$",
            "packages/.bluefin-parity-ref must contain a 40-character hex SHA",
        )

    def test_check_parity_recipe_guards_the_ref_format(self):
        """Covers the #152 pinned-ref format guard in the Justfile.

        Part of the pinned-ref work (#152) that was bundled into an
        unrelated PR; asserts the check-parity recipe rejects a ref that is
        not a full 40-character SHA before it is fetched.
        """
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

    def test_systemd_boot_update_enabled_and_gated_on_the_loader(self):
        # projectbluefin/utah#363: on systemd-boot systems bootupd stands down
        # by design ("managed with bootctl"), so the image must carry the boot
        # manager binaries and run bootctl update itself. The gate keeps BIOS
        # systems (no efivars) off an ESP that is not systemd-boot's; Fedora
        # GRUB-EFI also sets LoaderInfo via grub2's bli module, where bootctl
        # update is a harmless no-op. Secure Boot systems must be excluded
        # outright, or the unsigned build would overwrite a signed sd-boot and
        # the firmware would reject the next boot.
        self.assertIn("systemd-boot-update.service", self.script_units("enable_unit"))
        self.assertIn("systemd-boot-update.service", self.preset_directives("enable"))

        dropin = ROOT / (
            "system_files/shared/usr/lib/systemd/system/"
            "systemd-boot-update.service.d/10-only-on-systemd-boot.conf"
        )
        self.assertTrue(dropin.is_file(), f"missing loader gate: {dropin}")
        text = dropin.read_text()
        self.assertIn("[Unit]", text)
        self.assertIn(
            "ConditionPathExists=/sys/firmware/efi/efivars/"
            "LoaderInfo-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f",
            text,
        )
        self.assertIn("ConditionSecurity=!uefi-secureboot", text)
        self.assertIn("#363", text)

        contract = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        self.assertIn(
            "systemd-boot-update.service",
            contract.get("services", {}).get("enabled", []),
        )
        overlay = tomllib.loads((ROOT / "packages/utah.toml").read_text())
        self.assertIn(
            "systemd-boot-unsigned",
            overlay.get("services", {}).get("packages", []),
        )


if __name__ == "__main__":
    unittest.main()
