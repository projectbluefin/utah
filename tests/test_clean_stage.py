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

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "clean-stage.sh"
SOURCE_DATE_EPOCH = 1704067200  # 2024-01-01T00:00:00Z, the epoch clean-stage pins to


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
    # /var/home must not survive clean-stage even though the shipped image
    # carries it: the Containerfile creates it after clean-stage runs (#602).
    (root / "var/home").mkdir()

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
    # The reproducible-build surface: /usr and /etc with files whose mtimes are
    # deliberately far in the future, plus the dnf5 transaction history the
    # script must drop. The mtime test asserts clean-stage pins the former to
    # SOURCE_DATE_EPOCH and removes the latter (utah#313).
    for directory in ("usr/bin", "usr/share/doc", "etc/systemd/system",
                      "usr/share/fonts/utah", "boot"):
        (root / directory).mkdir(parents=True)
    (root / "usr/bin/tool").write_text("binary\n")
    (root / "usr/share/doc/readme").write_text("doc\n")
    (root / "etc/systemd/system/foo.service").write_text("[Unit]\n")
    (root / "usr/share/fonts/utah/Utah.ttf").write_bytes(b"ttf\n")
    (root / "boot/vmlinuz-7.2.6-ogc1").write_bytes(b"kernel\n")
    (root / "boot/vmlinuz").symlink_to("vmlinuz-7.2.6-ogc1")
    # Year 2036 -- well after the epoch the script pins to -- so a failure to
    # normalise is unmistakable rather than a coincidence with the target.
    future = 2085840000
    for path in (root / "usr/bin/tool", root / "usr/share/doc/readme",
                 root / "etc/systemd/system/foo.service",
                 root / "usr/share/fonts/utah/Utah.ttf",
                 root / "usr/share/fonts/utah"):
        os.utime(path, (future, future))
    # dnf5's per-transaction SQLite database under the sysroot.
    txn = root / "usr/lib/sysimage/libdnf5"
    txn.mkdir(parents=True)
    (txn / "transaction_history.sqlite").write_bytes(b"sqlite\n")
    (txn / "transaction_history.sqlite-shm").write_bytes(b"shm\n")
    (txn / "transaction_history.sqlite-wal").write_bytes(b"wal\n")
    # Symlinks whose target is not in the image. Real builds are full of them:
    # /usr/lib/bootc/storage, /usr/share/licenses/malcontent/COPYING and the
    # 32-bit libstdc++.a stubs all dangle in the committed tree.
    (root / "usr/share/licenses/malcontent").mkdir(parents=True)
    (root / "usr/share/licenses/malcontent/COPYING").symlink_to(
        "../../doc/malcontent/COPYING")
    (root / "usr/lib/bootc").mkdir(parents=True)
    (root / "usr/lib/bootc/storage").symlink_to("missing-storage")


def clean(root: Path, stub_bin: Path | None = None,
          inherit_path: bool = True) -> subprocess.CompletedProcess:
    """Run the real script with CLEAN_ROOT pointed at `root`.

    `stub_bin`, when given, is prepended to PATH so the run can supply a fake
    fc-cache. `inherit_path=False` drops the host's own directories entirely,
    which is the only way to exercise the branch taken when fontconfig is not
    installed: a CI runner that has it would otherwise hand the script a real
    fc-cache, and the real binary must never be pointed at a scratch tree.
    """
    path = "/usr/bin:/bin" if inherit_path else ""
    if stub_bin is not None:
        path = f"{stub_bin}:{path}" if path else str(stub_bin)
    env = {"PATH": path, "CLEAN_ROOT": str(root)}
    if stub_bin is not None and (stub_bin / "fc-cache").is_file():
        env["FC_CACHE"] = str(stub_bin / "fc-cache")
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )


# The external commands clean-stage.sh calls. Everything else it uses is a bash
# builtin, so a PATH holding just these runs the script with no fc-cache in
# sight and nothing else of the host's either.
SCRIPT_COMMANDS = ("bash", "find", "rm", "touch")


def shadow_bin(directory: Path) -> Path:
    """A bin dir with the script's tools and deliberately no fc-cache."""
    bin_dir = directory / "shadow-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in SCRIPT_COMMANDS:
        resolved = shutil.which(name)
        assert resolved is not None, f"{name} is required to run clean-stage.sh"
        target = bin_dir / name
        if not target.exists():
            target.symlink_to(resolved)
    return bin_dir


FC_CACHE_STUB = """#!/usr/bin/bash
# Stand-in for fontconfig's fc-cache. Records how clean-stage called it and the
# state of the tree at that moment, then writes a cache file the way the real
# one does: with the current wall clock, which is what clean-stage must re-pin.
set -eu
sysroot=""
for arg in "$@"; do
    case "$arg" in
        --sysroot=*) sysroot="${arg#--sysroot=}" ;;
    esac
done
{
    echo "argv=$*"
    echo "SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH-unset}"
    echo "fontdir_mtime=$(stat -c %Y "${sysroot}/usr/share/fonts/utah")"
} > "$RECORD"
cache="${sysroot}/usr/lib/fontconfig/cache"
mkdir -p "$cache"
echo "cache" > "${cache}/deadbeef-le64.cache-9"
touch -d "@2085840000" "${cache}/deadbeef-le64.cache-9" "$cache"
"""


def clean_without_fontconfig(root: Path) -> subprocess.CompletedProcess:
    """Run the script on a PATH that holds its tools and no fc-cache at all."""
    with tempfile.TemporaryDirectory() as harness:
        return clean(root, stub_bin=shadow_bin(Path(harness)), inherit_path=False)


VAR_CACHE_FC_STUB = """#!/usr/bin/bash
# A fontconfig whose cachedir list starts with /var/cache/fontconfig, which is
# the stock upstream order. clean-stage must sweep what this leaves behind and
# re-pin the directories writing it stamped.
set -eu
sysroot=""
for arg in "$@"; do
    case "$arg" in
        --sysroot=*) sysroot="${arg#--sysroot=}" ;;
    esac
done
cache="${sysroot}/var/cache/fontconfig"
mkdir -p "$cache"
echo "cache" > "${cache}/deadbeef-le64.cache-9"
touch -d "@2085840000" "${cache}/deadbeef-le64.cache-9" "$cache"
"""


def fc_cache_stub(directory: Path, record: Path) -> Path:
    """Install the stub in `directory` and return the bin dir to put on PATH."""
    bin_dir = directory / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "fc-cache"
    stub.write_text(FC_CACHE_STUB.replace("$RECORD", str(record)))
    stub.chmod(0o755)
    return bin_dir


class CleanStageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        build_tree(self.root)
        # No fc-cache on PATH, and no host directories either: the font-cache
        # branch has its own tests with a stub, and a CI runner that happens to
        # have fontconfig installed must not end up running the real binary
        # against this scratch tree.
        harness = tempfile.TemporaryDirectory()
        self.addCleanup(harness.cleanup)
        self.result = clean(
            self.root, stub_bin=shadow_bin(Path(harness.name)), inherit_path=False)

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
            result = clean_without_fontconfig(root)
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
            result = clean_without_fontconfig(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "var/marker").is_file())


    def test_mtimes_under_usr_and_etc_are_pinned(self):
        """A rebuild that changes nothing must commit identical layer digests.
        dnf, meson and the extension build leave wall-clock mtimes under /usr
        and /etc, and chunkah splits those directories across layers, so a
        changed mtime in any tar header changes that layer's digest. clean-stage
        pins them to SOURCE_DATE_EPOCH, so a layer's digest is a function of its
        content alone (utah#313). This tree has no rpmdb and the PATH has no
        rpm, which is the fallback: nothing is packaged, so everything is
        pinned. PackagedMtimeTests covers the image case, where RPM's own
        mtimes are preserved."""
        self.assertCleanSucceeded()
        for path in (
            self.root / "usr/bin/tool",
            self.root / "usr/share/doc/readme",
            self.root / "etc/systemd/system/foo.service",
        ):
            mtime = int(path.stat().st_mtime)
            self.assertEqual(
                mtime,
                SOURCE_DATE_EPOCH,
                f"{path} was not pinned to SOURCE_DATE_EPOCH",
            )

    def test_boot_kernel_directory_and_symlink_are_pinned(self):
        self.assertCleanSucceeded()
        for path in (self.root / "boot", self.root / "boot/vmlinuz",
                     self.root / "boot/vmlinuz-7.2.6-ogc1"):
            self.assertEqual(int(path.lstat().st_mtime), SOURCE_DATE_EPOCH)

    def test_rewritten_directories_are_pinned_too(self):
        """The directories clean-stage itself rewrites must be pinned as well.

        Removing an entry from /var, /var/cache, /run, /tmp or / stamps the wall
        clock on that directory. /var/cache is a parent of the surviving
        /var/cache/rpm-ostree, so a chunkah layer carries those entries and its
        digest would vary per rebuild even though nothing changed (utah#313).
        """
        self.assertCleanSucceeded()
        for path in (
            self.root,
            self.root / "var",
            self.root / "var/cache",
            self.root / "var/cache/rpm-ostree",
            self.root / "var/cache/rpm-ostree/repomd.xml",
            self.root / "run",
            self.root / "tmp",
        ):
            mtime = int(os.lstat(path).st_mtime)
            self.assertEqual(
                mtime,
                SOURCE_DATE_EPOCH,
                f"{path} was not pinned to SOURCE_DATE_EPOCH",
            )

    def test_transaction_history_is_dropped(self):
        """dnf5 records every transaction in usr/lib/sysimage/libdnf5/
        transaction_history.sqlite (with its -shm and -wal companions). It is
        build-time metadata -- nothing at runtime reads it -- and it carries a
        wall-clock mtime plus an in-memory page cache, so it both wastes space
        and churns the layer that carries it. clean-stage drops all three."""
        self.assertCleanSucceeded()
        base = self.root / "usr/lib/sysimage/libdnf5"
        for name in (
            "transaction_history.sqlite",
            "transaction_history.sqlite-shm",
            "transaction_history.sqlite-wal",
        ):
            self.assertFalse(
                (base / name).exists(),
                f"{name} should have been removed",
            )

    def test_dangling_symlinks_do_not_fail_the_build(self):
        """A committed tree contains symlinks whose target is not in the image
        -- /usr/lib/bootc/storage, the malcontent COPYING links, the 32-bit
        libstdc++.a stubs. `touch` follows symlinks by default, so it reported
        "No such file or directory" for each one and exited non-zero, which
        under `set -e` failed the final Containerfile layer for every flavor.
        clean-stage passes -h, stamping the link itself (utah#313)."""
        self.assertCleanSucceeded()
        for link in (
            self.root / "usr/share/licenses/malcontent/COPYING",
            self.root / "usr/lib/bootc/storage",
        ):
            self.assertTrue(link.is_symlink(), f"{link} should still be a symlink")
            self.assertFalse(link.exists(), f"{link} should still dangle")
            mtime = int(os.lstat(link).st_mtime)
            self.assertEqual(
                mtime,
                SOURCE_DATE_EPOCH,
                f"{link} was not pinned to SOURCE_DATE_EPOCH",
            )

    def test_absent_usr_and_etc_are_not_an_error(self):
        """A tree without /usr or /etc must still exit zero: the normalisation
        is scoped to the directories that exist, mirroring the /run and /tmp
        guard the other absence test relies on."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_tree(root)
            for directory in ("usr", "etc"):
                subprocess.run(["rm", "-rf", str(root / directory)], check=True)
            result = clean_without_fontconfig(root)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_fc_cache_is_not_an_error(self):
        """A host without fontconfig must still produce a clean layer.

        The setUp run is already such a host -- its PATH carries the script's
        own tools and nothing else -- so this asserts on what that run said and
        left behind: the skip is announced, and no cache tree is invented.
        """
        self.assertCleanSucceeded()
        self.assertIn("fc-cache not found", self.result.stderr)
        self.assertFalse((self.root / "usr/lib/fontconfig").exists())


class FontCacheTests(unittest.TestCase):
    """Pinning /usr invalidates every system font cache, so clean-stage rebuilds.

    fontconfig accepts a cache under /usr/lib/fontconfig/cache only when the
    checksum stored in it equals the font directory's current mtime exactly
    (FcDirCacheValidateHelper in fccache.c). Fedora's fontconfig rebuilds those
    caches from a %transfiletriggerin -- `fc-cache -s`, inside the dnf
    transaction that installs Utah's fonts -- so they record the wall-clock
    mtimes dnf wrote. clean-stage then re-stamps /usr/share/fonts to
    SOURCE_DATE_EPOCH, and the rebuild removes the caches' embedded wall-clock
    checksums while matching the final tar mtimes for container readers.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        build_tree(self.root)
        harness = tempfile.TemporaryDirectory()
        self.addCleanup(harness.cleanup)
        self.record = Path(harness.name) / "fc-cache.record"
        bin_dir = fc_cache_stub(Path(harness.name), self.record)
        self.result = clean(self.root, stub_bin=bin_dir)
        self.assertEqual(
            self.result.returncode, 0,
            f"clean-stage.sh failed:\n{self.result.stderr}")

    def recorded(self) -> dict:
        self.assertTrue(
            self.record.exists(),
            "clean-stage never invoked fc-cache, so every system font cache "
            "still records a pre-pin mtime and is stale at runtime",
        )
        return dict(
            line.split("=", 1)
            for line in self.record.read_text().splitlines()
        )

    def test_font_caches_are_rebuilt_forcibly_and_system_wide(self):
        """-f is required because the stale caches are still present and
        fontconfig would otherwise keep them; -s writes the system cache
        directory rather than a per-user one; --sysroot confines the rebuild to
        the tree being cleaned."""
        argv = self.recorded()["argv"]
        self.assertIn(f"--sysroot={self.root}", argv)
        self.assertIn("--force", argv)
        self.assertIn("--system-only", argv)

    def test_rebuild_runs_after_the_mtime_pin(self):
        """Order is the whole point: fontconfig stores the directory mtime it
        sees as the cache's checksum, so the rebuild has to happen once
        /usr/share/fonts already carries its final, pinned mtime. Run it before
        the pin and the cache is stale the moment the pin lands."""
        self.assertEqual(
            self.recorded()["fontdir_mtime"],
            str(SOURCE_DATE_EPOCH),
            "fc-cache ran before the pin loop, so it stored a pre-pin checksum",
        )

    def test_rebuild_is_deterministic(self):
        """fontconfig honours SOURCE_DATE_EPOCH in both the checksum and the
        nanosecond field it writes, so exporting it is what keeps the cache
        files byte-identical between two builds of the same content."""
        self.assertEqual(
            self.recorded()["SOURCE_DATE_EPOCH"],
            str(SOURCE_DATE_EPOCH),
            "fc-cache ran without SOURCE_DATE_EPOCH exported",
        )

    def test_rebuilt_caches_are_pinned(self):
        """fc-cache writes with the wall clock, so leaving its output unpinned
        would hand back the churn the pin loop exists to remove."""
        cache = self.root / "usr/lib/fontconfig/cache"
        for path in (cache, cache / "deadbeef-le64.cache-9"):
            self.assertEqual(
                int(os.lstat(path).st_mtime),
                SOURCE_DATE_EPOCH,
                f"{path} was not pinned to SOURCE_DATE_EPOCH",
            )


class VarCacheFontCacheTests(unittest.TestCase):
    """fontconfig's stock cachedir order puts /var/cache/fontconfig first.

    The Fedora base this image is built from lists /usr/lib/fontconfig/cache
    instead, but the script also runs on hosts carrying the upstream order, and
    there the rebuild deposits a fresh directory under /var/cache -- after the
    sweep that leaves bootc only the rpm-ostree cache it expects, and after the
    pin loop. Both have to be applied again or the layer ships a directory
    `bootc container lint` rejects, carrying a wall-clock mtime.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "root"
        self.root.mkdir()
        build_tree(self.root)
        harness = tempfile.TemporaryDirectory()
        self.addCleanup(harness.cleanup)
        bin_dir = Path(harness.name) / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "fc-cache"
        stub.write_text(VAR_CACHE_FC_STUB)
        stub.chmod(0o755)
        self.result = clean(self.root, stub_bin=bin_dir)
        self.assertEqual(
            self.result.returncode, 0,
            f"clean-stage.sh failed:\n{self.result.stderr}")

    def test_var_cache_still_keeps_only_rpm_ostree(self):
        survivors = sorted(p.name for p in (self.root / "var/cache").iterdir())
        self.assertEqual(survivors, ["rpm-ostree"])

    def test_directories_the_rebuild_stamped_are_repinned(self):
        for path in (
            self.root,
            self.root / "var",
            self.root / "var/cache",
            self.root / "var/cache/rpm-ostree",
        ):
            self.assertEqual(
                int(os.lstat(path).st_mtime),
                SOURCE_DATE_EPOCH,
                f"{path} was not re-pinned after the font cache rebuild",
            )


class ParentDirectoryPinTests(unittest.TestCase):
    """A cache written under /usr also stamps every directory above it.

    fc-cache creating /usr/lib/fontconfig/cache updates /usr/lib/fontconfig,
    /usr/lib and /usr, all of which chunkah carries in a layer, so pinning the
    cache alone still leaves three directories churning per rebuild.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "root"
        self.root.mkdir()
        build_tree(self.root)
        harness = tempfile.TemporaryDirectory()
        self.addCleanup(harness.cleanup)
        record = Path(harness.name) / "fc-cache.record"
        bin_dir = fc_cache_stub(Path(harness.name), record)
        self.result = clean(self.root, stub_bin=bin_dir)
        self.assertEqual(
            self.result.returncode, 0,
            f"clean-stage.sh failed:\n{self.result.stderr}")

    def test_every_parent_of_the_cache_is_pinned(self):
        for path in (
            self.root,
            self.root / "usr",
            self.root / "usr/lib",
            self.root / "usr/lib/fontconfig",
            self.root / "usr/lib/fontconfig/cache",
        ):
            self.assertEqual(
                int(os.lstat(path).st_mtime),
                SOURCE_DATE_EPOCH,
                f"{path} was not pinned after the font cache rebuild",
            )


class EmptyVarCacheTests(unittest.TestCase):
    """A flavor with no /var/cache/rpm-ostree empties /var/cache entirely.

    The sweep runs twice -- once up front, once after the font cache rebuild --
    and on such a tree the second pass sees a directory with nothing in it.
    A `/var/cache/*` glob matches nothing there, bash hands `find` the literal
    pattern, `find` exits 1, and `set -e` takes the whole image build with it:
    `Error: building at STEP "RUN ... utah-clean-stage ..."`.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "root"
        self.root.mkdir()
        build_tree(self.root)
        shutil.rmtree(self.root / "var/cache/rpm-ostree")
        harness = tempfile.TemporaryDirectory()
        self.addCleanup(harness.cleanup)
        record = Path(harness.name) / "fc-cache.record"
        bin_dir = fc_cache_stub(Path(harness.name), record)
        self.result = clean(self.root, stub_bin=bin_dir)

    def test_script_succeeds(self):
        self.assertEqual(
            self.result.returncode, 0,
            f"clean-stage.sh failed on an empty /var/cache:\n{self.result.stderr}")

    def test_var_cache_survives_empty(self):
        cache = self.root / "var/cache"
        self.assertTrue(cache.is_dir(), "/var/cache must not be removed")
        self.assertEqual(sorted(p.name for p in cache.iterdir()), [])


class PackagedMtimeTests(unittest.TestCase):
    """The pin must skip paths still carrying the mtime RPM gave them.

    A blanket `touch` of /usr rewrites every RPM-installed file, and two things
    depend on those mtimes. Fedora byte-compiles with
    `--invalidation-mode=timestamp`, so every .pyc records the mtime its .py had
    at build time: moving the .py without rewriting the .pyc makes each stdlib
    import recompile in memory on every container run of the image. And `rpm -V`
    compares the same mtime, so it reports T for every file in the image, which
    buries any real modification. Neither is visible on a booted bootc host --
    ostree deploys with mtime 0 -- but the ISO compose, CI and `podman run` all
    read the image as a container, where the stamp survives.

    So clean-stage asks RPM what it gave each path and pins only the mismatches:
    what RPM does not own, plus what the build rewrote after RPM wrote it.
    """

    # The tools the rpm-aware sweep needs on top of SCRIPT_COMMANDS.
    EXTRA_COMMANDS = ("sort", "comm", "cut", "tr", "xargs", "sed")

    # Packaged, never touched by the build: mtimes that match the index below.
    PACKAGED_MTIME = 1600000000
    # Packaged, then rewritten by the build -- ld.so.cache, a `sed -i` target.
    # The index still carries the original, so the on-disk stamp is a mismatch.
    REPACKAGED_ORIGINAL_MTIME = 1600000001
    BUILD_MTIME = 2085840000

    def harness(self, index_lines, root):
        """A PATH with the script's tools and an `rpm` stub serving `index_lines`."""
        harness = tempfile.TemporaryDirectory()
        self.addCleanup(harness.cleanup)
        base = Path(harness.name)
        bin_dir = shadow_bin(base)
        for name in self.EXTRA_COMMANDS:
            resolved = shutil.which(name)
            assert resolved is not None, f"{name} is required by clean-stage.sh"
            target = bin_dir / name
            if not target.exists():
                target.symlink_to(resolved)
        index = "".join(f"{path}\t{mtime}\n" for path, mtime in index_lines)
        self.argv_record = base / "rpm.argv"
        stub = bin_dir / "rpm"
        # printf and the redirect are builtins: the stub has to run on the same
        # stripped PATH as the script, which carries no `cat`.
        stub.write_text(
            "#!/usr/bin/bash\n"
            "set -eu\n"
            f'echo "$*" > "{self.argv_record}"\n'
            f"printf '%s' {shlex.quote(index)}\n"
        )
        stub.chmod(0o755)
        return clean(root, stub_bin=bin_dir, inherit_path=False)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        build_tree(self.root)
        # A packaged file and the .pyc that records its mtime, the pairing the
        # blanket touch desynced.
        site = self.root / "usr/lib/python3.13/site-packages"
        (site / "__pycache__").mkdir(parents=True)
        self.packaged = site / "utahmod.py"
        self.packaged.write_text("import os\n")
        self.packaged_pyc = site / "__pycache__/utahmod.cpython-313.pyc"
        self.packaged_pyc.write_bytes(b"pyc\n")
        for path in (self.packaged, self.packaged_pyc):
            os.utime(path, (self.PACKAGED_MTIME, self.PACKAGED_MTIME))
        # Packaged, then rewritten by the build: on disk at the wall clock while
        # the index still holds what RPM wrote.
        self.rewritten = self.root / "etc/ld.so.cache"
        self.rewritten.write_bytes(b"cache\n")
        os.utime(self.rewritten, (self.BUILD_MTIME, self.BUILD_MTIME))
        # A packaged directory nothing wrote into afterwards.
        self.packaged_dir = self.root / "usr/share/licenses/malcontent"
        os.utime(self.packaged_dir, (self.PACKAGED_MTIME, self.PACKAGED_MTIME))
        self.result = self.harness(
            [
                (f"/{self.packaged.relative_to(self.root)}", self.PACKAGED_MTIME),
                (f"/{self.packaged_pyc.relative_to(self.root)}", self.PACKAGED_MTIME),
                (f"/{self.packaged_dir.relative_to(self.root)}", self.PACKAGED_MTIME),
                (f"/{self.rewritten.relative_to(self.root)}",
                 self.REPACKAGED_ORIGINAL_MTIME),
            ],
            self.root,
        )

    def assertCleanSucceeded(self):
        self.assertEqual(
            self.result.returncode, 0,
            f"clean-stage.sh failed:\n{self.result.stderr}")

    def test_the_rpmdb_is_read_under_clean_root(self):
        """The query must be scoped to the tree being cleaned, not the host."""
        self.assertCleanSucceeded()
        argv = self.argv_record.read_text()
        self.assertIn(f"--root={self.root}", argv)
        self.assertIn("%{FILENAMES}", argv)
        self.assertIn("%{FILEMTIMES}", argv)

    def test_unmodified_packaged_files_keep_their_mtime(self):
        self.assertCleanSucceeded()
        for path in (self.packaged, self.packaged_pyc, self.packaged_dir):
            self.assertEqual(
                int(os.lstat(path).st_mtime),
                self.PACKAGED_MTIME,
                f"{path} was re-stamped even though it still had RPM's mtime",
            )

    def test_the_pyc_pairing_survives(self):
        """The .py and its .pyc must come out of the sweep agreeing."""
        self.assertCleanSucceeded()
        self.assertEqual(
            int(os.lstat(self.packaged).st_mtime),
            int(os.lstat(self.packaged_pyc).st_mtime),
            "the source and its byte-compiled form no longer agree, so every "
            "import of it recompiles in memory",
        )

    def test_packaged_files_the_build_rewrote_are_pinned(self):
        """RPM's record is stale for these, so there is nothing to preserve."""
        self.assertCleanSucceeded()
        self.assertEqual(
            int(os.lstat(self.rewritten).st_mtime),
            SOURCE_DATE_EPOCH,
            f"{self.rewritten} kept a wall-clock mtime and would churn its layer",
        )

    def test_unpackaged_paths_are_still_pinned(self):
        """Everything COPYed in or built in place still gets the epoch."""
        self.assertCleanSucceeded()
        for path in (
            self.root / "usr/bin/tool",
            self.root / "usr/share/doc/readme",
            self.root / "etc/systemd/system/foo.service",
            self.root / "usr/share/fonts/utah/Utah.ttf",
            self.root / "usr/share/fonts/utah",
            self.root / "usr/lib/bootc/storage",
        ):
            self.assertEqual(
                int(os.lstat(path).st_mtime),
                SOURCE_DATE_EPOCH,
                f"{path} was not pinned to SOURCE_DATE_EPOCH",
            )

    def test_an_empty_rpmdb_falls_back_to_pinning_everything(self):
        """rpm installed but reporting nothing must not ship an unpinned tree.

        The fallback is the old blanket touch, which is worth announcing: it is
        what reintroduces the .pyc and `rpm -V` damage.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_tree(root)
            result = self.harness([], root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("rpm reported no packaged mtimes", result.stderr)
            self.assertEqual(
                int(os.lstat(root / "usr/bin/tool").st_mtime), SOURCE_DATE_EPOCH)

    def test_no_index_is_left_behind_in_the_image(self):
        """The packaged-mtime index must not reach the image.

        It is held in a variable precisely because this runs after /tmp and
        /var/tmp are emptied: a temp file there is residue `bootc container
        lint --fatal-warnings` rejects, and with TMPDIR pointing at a directory
        the sweep removed, mktemp fails and `set -e` takes the build with it.
        """
        self.assertCleanSucceeded()
        self.assertEqual(
            list((self.root / "tmp").iterdir()), [],
            "/tmp must be empty: the packaged-mtime index has to be removed")
        self.assertEqual(
            int(os.lstat(self.root / "tmp").st_mtime),
            SOURCE_DATE_EPOCH,
            "/tmp must be pinned after the index is removed, not before")


if __name__ == "__main__":
    unittest.main()
