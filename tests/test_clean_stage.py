"""scripts/clean-stage.sh must strip build residue without taking the keepers.

The script runs in the final Containerfile layer, and everything it deletes is
deleted from the image every flavor ships. Until now nothing executed it: the
only test that named it at all was the `bash -n` syntax gate, which proves the
file parses and nothing about what it removes. A wrong `-name` filter or a
dropped `\\!` would still parse, and the symptom would be either a `bootc
container lint --fatal-warnings` failure at the end of a full build or, worse,
an image that silently lost /var/cache/rpm-ostree.

The script already carries the seam these tests need: every path it touches is
prefixed with `CLEAN_ROOT`, documented in the script as being there "so the
script can be exercised against a temporary directory rather than the live
filesystem". These tests take it up on that -- they build a scratch tree that
mimics the offenders seen on real builds, run the real script against it, and
assert on the tree that survives.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "clean-stage.sh"


def build_tree(root: Path) -> None:
    """A scratch image tree holding one of each thing the script reasons about.

    The directory names are the ones quoted in the script's own header as the
    lint offenders it exists to remove: /var/log with dnf5 logs, the
    /var/cache/libdnf5 keyring tree the NVIDIA flavors regrew, /run/cockpit and
    the unpacked /utah-cache build input.
    """
    for directory in ("var/log", "var/lib", "var/tmp"):
        (root / directory).mkdir(parents=True)
    (root / "var/log/dnf5.log").write_text("install transaction\n")
    (root / "var/lib/systemd").mkdir()

    # /var/cache survives, but only rpm-ostree survives inside it.
    (root / "var/cache/rpm-ostree").mkdir(parents=True)
    (root / "var/cache/rpm-ostree/repomd.xml").write_text("keep me\n")
    (root / "var/cache/libdnf5/nvidia-container-toolkit-abc123/pubring").mkdir(parents=True)
    (root / "var/cache/libdnf5/nvidia-container-toolkit-abc123/pubring/"
            "DDCAE044F796ECB0.pub").write_text("gpg key\n")
    (root / "var/cache/ibus").mkdir()

    for directory in ("run/cockpit", "run/dnf", "tmp/build-scratch"):
        (root / directory).mkdir(parents=True)
    (root / "run/cockpit/socket").write_text("residue\n")
    (root / "tmp/stray.tmp").write_text("residue\n")

    (root / "utah-cache/kernel-rpms").mkdir(parents=True)
    (root / "utah-cache/kernel-rpms/kernel-7.1.8-ogc1.rpm").write_bytes(b"1.5 GB, pretend\n")


def clean(root: Path) -> subprocess.CompletedProcess:
    """Run the real script with CLEAN_ROOT pointed at `root`."""
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={"PATH": "/usr/bin:/bin", "CLEAN_ROOT": str(root)},
        capture_output=True,
        text=True,
    )


class CleanStageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        build_tree(self.root)
        self.result = clean(self.root)

    def assertCleanSucceeded(self):
        self.assertEqual(
            self.result.returncode, 0,
            f"clean-stage.sh failed:\n{self.result.stderr}")

    def test_exits_zero_on_a_representative_tree(self):
        self.assertCleanSucceeded()

    def test_var_keeps_only_cache(self):
        """Every directory directly under /var goes except `cache` itself."""
        self.assertCleanSucceeded()
        survivors = sorted(p.name for p in (self.root / "var").iterdir())
        self.assertEqual(survivors, ["cache"])

    def test_var_cache_keeps_only_rpm_ostree(self):
        """rpm-ostree is the one cache bootc expects to find after the build."""
        self.assertCleanSucceeded()
        survivors = sorted(p.name for p in (self.root / "var/cache").iterdir())
        self.assertEqual(survivors, ["rpm-ostree"])
        self.assertEqual(
            (self.root / "var/cache/rpm-ostree/repomd.xml").read_text(),
            "keep me\n",
            "the surviving cache must keep its contents, not just its directory")

    def test_libdnf5_keyring_tree_is_removed(self):
        """The regression that put var-tmpfiles back on the NVIDIA flavors.

        `dnf clean all` drops the metadata and leaves the imported GPG keyring,
        so lint rejected both the untracked directories and the key inside
        them. Asserted on its own because it is the specific tree the script's
        second `find` was widened to cover.
        """
        self.assertCleanSucceeded()
        self.assertFalse((self.root / "var/cache/libdnf5").exists())

    def test_run_and_tmp_are_emptied_but_kept(self):
        """/run and /tmp are cleared in place; deleting them breaks the build.

        The container runtime bind-mounts /run/.containerenv, so `rm -rf /run`
        fails with EBUSY and takes the build with it.
        """
        self.assertCleanSucceeded()
        for directory in ("run", "tmp"):
            path = self.root / directory
            self.assertTrue(path.is_dir(), f"/{directory} must survive as a directory")
            self.assertEqual(
                list(path.iterdir()), [],
                f"/{directory} must be left empty")

    def test_unpacked_build_input_cache_is_removed(self):
        """/utah-cache is build input; shipping it costs roughly 1.5 GB."""
        self.assertCleanSucceeded()
        self.assertFalse((self.root / "utah-cache").exists())

    def test_absent_run_and_tmp_are_not_an_error(self):
        """`main` never grows some of these; a missing directory is not a failure."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_tree(root)
            for directory in ("run", "tmp", "utah-cache"):
                subprocess.run(["rm", "-rf", str(root / directory)], check=True)
            result = clean(root)
            self.assertEqual(
                result.returncode, 0,
                f"clean-stage.sh must tolerate absent /run, /tmp and /utah-cache:\n"
                f"{result.stderr}")

    def test_files_are_not_mistaken_for_directories_under_var(self):
        """The `-type d` filter is deliberate: a plain file under /var is not a tree.

        Pinned because dropping `-type d` would look like a harmless
        simplification while changing what the final layer contains.
        """
        self.assertCleanSucceeded()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_tree(root)
            (root / "var/marker").write_text("plain file\n")
            result = clean(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "var/marker").is_file())


if __name__ == "__main__":
    unittest.main()
