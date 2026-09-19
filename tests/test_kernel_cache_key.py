"""The kernel cache key must cover everything the cache image is built from.

`scripts/kernel-cache-tag.sh` names the inputs it hashes as a hand-written
list. `Containerfile.kernel` decides, independently, what the cache image is
actually built from: its `ARG BASE_IMAGE=` pin and the files it `COPY`s into
the builder. Nothing tied the two lists together, so a `COPY` added to
`Containerfile.kernel` without a matching line in the hash script produces a
cache key that does not move when that input does -- and CI skips the rebuild
whenever the tag is already published, so the three flavors that consume the
cache keep unpacking a kernel and an NVIDIA module built from the old input,
silently, for as long as the tag stays the same.

This suite derives the input set from `Containerfile.kernel` instead of
restating it, and asserts behaviourally -- by mutating a copy of the tree and
re-running the script -- that every derived input moves the key, that a file
which is not a build input does not, and that the key is deterministic.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KERNEL_CONTAINERFILE = ROOT / "Containerfile.kernel"
TAG_SCRIPT = Path("scripts/kernel-cache-tag.sh")

# Copied into every sandbox so the script can run at all. Any further input is
# discovered from Containerfile.kernel, never listed here.
SANDBOX_TREES = ("scripts", "packages")

_BASE_IMAGE_LINE = re.compile(r"^ARG BASE_IMAGE=", re.MULTILINE)


def copy_sources() -> list[str]:
    """Repo-relative paths `Containerfile.kernel` copies into the build.

    `COPY --from=<stage>` is excluded: those come from an earlier stage, not
    from this repository, so there is no file here for the key to hash.
    """
    sources: list[str] = []
    for line in KERNEL_CONTAINERFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY "):
            continue
        tokens = stripped.split()[1:]
        if any(token.startswith("--from=") for token in tokens):
            continue
        tokens = [token for token in tokens if not token.startswith("--")]
        sources.extend(tokens[:-1])
    return sources


def base_image_line() -> str:
    for line in KERNEL_CONTAINERFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("ARG BASE_IMAGE="):
            return line
    raise AssertionError("Containerfile.kernel declares no ARG BASE_IMAGE=")


def tag_in(tree: Path) -> str:
    result = subprocess.run(
        ["bash", str(tree / TAG_SCRIPT)],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


class SandboxMixin(unittest.TestCase):
    """A writable copy of exactly the tree the tag script reads."""

    def sandbox(self, tmp: str) -> Path:
        tree = Path(tmp) / "utah"
        tree.mkdir()
        for name in SANDBOX_TREES:
            shutil.copytree(ROOT / name, tree / name)
        shutil.copy2(KERNEL_CONTAINERFILE, tree / KERNEL_CONTAINERFILE.name)
        for source in copy_sources():
            destination = tree / source
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / source, destination)
        return tree


class KernelCacheKeyTests(SandboxMixin):
    def test_sandbox_reproduces_the_real_key(self):
        """A mismatch means the script reads something this suite never copies.

        Without this the mutation tests below could pass vacuously against a
        tree that is not the one CI hashes.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(tag_in(self.sandbox(tmp)), tag_in(ROOT))

    def test_key_is_deterministic(self):
        self.assertEqual(tag_in(ROOT), tag_in(ROOT))
        self.assertRegex(tag_in(ROOT), r"^[0-9a-f]{16}$")

    def test_containerfile_kernel_copies_something(self):
        """Guard against the derivation silently emptying."""
        sources = copy_sources()
        self.assertTrue(sources, "no COPY sources derived from Containerfile.kernel")
        for source in sources:
            with self.subTest(source=source):
                self.assertTrue(
                    (ROOT / source).is_file(),
                    f"Containerfile.kernel copies {source}, which does not exist",
                )

    def test_every_copied_build_input_changes_the_key(self):
        """The contract: a build input that moves must move the cache tag."""
        for source in copy_sources():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                tree = self.sandbox(tmp)
                before = tag_in(tree)
                target = tree / source
                target.write_bytes(target.read_bytes() + b"\n# kernel cache key probe\n")
                self.assertNotEqual(
                    before,
                    tag_in(tree),
                    f"Containerfile.kernel copies {source} into the cache image, but "
                    f"editing it does not change the cache tag, so CI reuses a cache "
                    f"image built from the old {source}. Add it to the hashed set in "
                    f"{TAG_SCRIPT}.",
                )

    def test_base_image_pin_changes_the_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = self.sandbox(tmp)
            before = tag_in(tree)
            containerfile = tree / KERNEL_CONTAINERFILE.name
            text = containerfile.read_text(encoding="utf-8")
            containerfile.write_text(
                text.replace(base_image_line(), base_image_line() + "0", 1),
                encoding="utf-8",
            )
            self.assertNotEqual(
                before,
                tag_in(tree),
                "the cache image is built FROM ${BASE_IMAGE} of "
                "Containerfile.kernel, but repinning it does not change the cache tag",
            )

    def test_unrelated_source_does_not_change_the_key(self):
        """"...and not otherwise": the key must not churn on unrelated edits.

        A key that moved on any change would rebuild the ~45-minute cache image
        for edits that cannot affect it, which is the cost the cache exists to
        avoid.
        """
        unrelated = "scripts/flavors.py"
        self.assertNotIn(
            unrelated,
            copy_sources(),
            f"{unrelated} is now a cache build input; pick another unrelated file",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tree = self.sandbox(tmp)
            before = tag_in(tree)
            target = tree / unrelated
            target.write_bytes(target.read_bytes() + b"\n# not a cache input\n")
            self.assertEqual(before, tag_in(tree))


if __name__ == "__main__":
    unittest.main()
