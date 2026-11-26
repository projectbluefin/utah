"""The artwork Utah takes from projectbluefin/artwork is pinned and verified.

Two sources, two mechanisms:

* the wallpapers come from the digest-pinned bluefin-wallpapers-gnome image,
  declared like the other image pins so Renovate keeps it current;
* every other file is fetched at one artwork commit and checked against a
  sha256 in config/artwork.lock by scripts/fetch-artwork.sh.

The build itself enforces both; these tests keep the declarations consistent
and prove the fetch script refuses what it should.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTAINERFILE = (ROOT / "Containerfile").read_text()
LOCK = ROOT / "config/artwork.lock"
FETCH = ROOT / "scripts/fetch-artwork.sh"

# Every caller of the fetch script: path in the artwork repository.
CALLERS = [
    ROOT / "Containerfile",
]


def lock_entries() -> tuple[str, dict[str, str]]:
    commit = ""
    sums: dict[str, str] = {}
    for line in LOCK.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if fields[0] == "commit":
            commit = fields[1]
        else:
            sums[fields[1]] = fields[0]
    return commit, sums


def fetched_paths() -> set[str]:
    paths: set[str] = set()
    for caller in CALLERS:
        text = caller.read_text().replace("\\\n", " ")
        paths |= set(re.findall(r"fetch-artwork\.sh\s+(assets/\S+)", text))
    return paths


class WallpaperImagePinTests(unittest.TestCase):
    def test_wallpapers_image_is_digest_pinned_like_the_other_images(self):
        self.assertRegex(
            CONTAINERFILE,
            r"(?m)^ARG WALLPAPERS_IMAGE=ghcr\.io/projectbluefin/bluefin-wallpapers-gnome$",
        )
        self.assertRegex(
            CONTAINERFILE, r"(?m)^ARG WALLPAPERS_IMAGE_SHA=sha256:[0-9a-f]{64}$"
        )
        self.assertIn(
            "FROM ${WALLPAPERS_IMAGE}@${WALLPAPERS_IMAGE_SHA} AS wallpapers",
            CONTAINERFILE,
        )

    def test_wallpapers_overlay_common_after_it_and_before_utah_local(self):
        order = [
            "cp -a /tmp/utah-inputs/tmp/utah-bluefin/. /",
            "cp -a /tmp/utah-inputs/tmp/utah-artwork/. /",
            "cp -a /tmp/utah-inputs/tmp/utah-local/. /",
        ]
        positions = [
            re.search(r"(?m)^\s+" + re.escape(item), CONTAINERFILE).start() for item in order
        ]
        self.assertEqual(positions, sorted(positions), order)
        self.assertIn("COPY --from=artwork /out/overlay /tmp/utah-artwork", CONTAINERFILE)

    def test_wallpaper_normalization_matches_commons(self):
        # Common moves the flat files into backgrounds/bluefin and rewrites the
        # per-user path to the system one. The same result must come out here.
        self.assertIn("COPY --from=wallpapers / /wallpapers", CONTAINERFILE)
        self.assertIn("sed -i 's|~/\\.local/share|/usr/share|g'", CONTAINERFILE)
        self.assertIn("usr/share/backgrounds/bluefin", CONTAINERFILE)
        self.assertIn("usr/share/gnome-background-properties", CONTAINERFILE)


class ArtworkLockTests(unittest.TestCase):
    def test_lock_is_a_full_commit_and_full_checksums(self):
        commit, sums = lock_entries()
        self.assertRegex(commit, r"^[0-9a-f]{40}$")
        self.assertTrue(sums)
        for path, digest in sums.items():
            self.assertRegex(digest, r"^[0-9a-f]{64}$", path)
            self.assertTrue(path.startswith("assets/"), path)

    def test_every_fetched_path_is_locked_and_every_lock_entry_is_used(self):
        _, sums = lock_entries()
        used = fetched_paths()
        self.assertEqual(used, set(sums), "lock and callers disagree")

    def test_the_three_old_blobs_are_gone(self):
        for blob in (
            "iso/live/src/images/utahraptor.png",
            "system_files/shared/usr/share/icons/hicolor/scalable/actions/ublue-logo-symbolic.svg",
            "system_files/shared/usr/share/pixmaps/bluefin-gdm-logo.png",
        ):
            self.assertFalse((ROOT / blob).exists(), f"{blob} must come from artwork")

class FetchScriptTests(unittest.TestCase):
    PATH = "assets/thing.svg"
    BODY = b"<svg/>\n"
    COMMIT = "a" * 40

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        tree = self.dir / "tree" / self.COMMIT / self.PATH
        tree.parent.mkdir(parents=True)
        tree.write_bytes(self.BODY)

    def lock(self, digest: str, path: str | None = None, commit: str | None = None) -> Path:
        lock = self.dir / "lock"
        lock.write_text(f"commit {commit or self.COMMIT}\n{digest}  {path or self.PATH}\n")
        return lock

    def run_fetch(self, lock: Path, path: str | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(
            os.environ,
            UTAH_ARTWORK_LOCK=str(lock),
            UTAH_ARTWORK_BASE=f"file://{self.dir / 'tree'}",
        )
        return subprocess.run(
            ["bash", str(FETCH), path or self.PATH, str(self.dir / "out/thing.svg")],
            env=env,
            capture_output=True,
            text=True,
        )

    def test_matching_checksum_installs_the_file(self):
        result = self.run_fetch(self.lock(hashlib.sha256(self.BODY).hexdigest()))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.dir / "out/thing.svg").read_bytes(), self.BODY)

    def test_checksum_mismatch_installs_nothing(self):
        result = self.run_fetch(self.lock("0" * 64))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)
        self.assertFalse((self.dir / "out/thing.svg").exists())

    def test_unlisted_path_is_refused(self):
        digest = hashlib.sha256(self.BODY).hexdigest()
        result = self.run_fetch(self.lock(digest), path="assets/other.svg")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no sha256 entry", result.stderr)

    def test_a_moved_commit_with_old_checksums_fails(self):
        # The commit moves, the bytes there differ: the stale checksum catches it.
        other = "b" * 40
        moved = self.dir / "tree" / other / self.PATH
        moved.parent.mkdir(parents=True)
        moved.write_bytes(b"<svg>changed</svg>\n")
        result = self.run_fetch(
            self.lock(hashlib.sha256(self.BODY).hexdigest(), commit=other)
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.dir / "out/thing.svg").exists())

    def test_a_short_commit_is_refused(self):
        result = self.run_fetch(
            self.lock(hashlib.sha256(self.BODY).hexdigest(), commit="main")
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("40-hex", result.stderr)


if __name__ == "__main__":
    unittest.main()
