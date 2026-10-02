"""A unit that tolerates an absent thing must say so per command, not unit-wide.

`docs/skills/desktop-contract.md` records the convention: an expected non-zero
exit is expressed with the `ExecStart=-` prefix, never with a unit-wide
`SuccessExitStatus=`, and never by reaching for `flatpak remote-delete --force`
to make a re-run succeed. Until these tests the convention was prose only, and
that is exactly how PR #139 came to carry the `--force` variant of the same
hunk `flatpak-nuke-fedora.service` already had: both variants were green, and
only review caught it. The rule now has teeth.

The two failure modes this guards:

- `SuccessExitStatus=1` is unit-wide, so it also swallows a genuine exit 1
  from a *later* command in the same unit -- in this unit, the `touch` that
  stamps `/var/lib/flatpak/.fedora-initialized`. A real failure there would be
  reported as success.
- `remote-delete --force` only changes the "remote has installed refs" guard,
  so it deletes the remote *and* the apps installed from it, leaving those refs
  with no origin to update from.

The scan is deliberately scoped to unit files under `system_files/` and
`iso/live/`. A shell script may legitimately use `--force` on a throwaway
remote it created itself -- `iso/live/src/install-flatpaks.sh` does exactly
that for the live image's own `installer-local` remote, and that is not the
`fedora` remote this convention is about.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT_ROOTS = ("system_files", "iso/live")
NUKE_FEDORA = (
    ROOT / "system_files/shared/usr/lib/systemd/system/flatpak-nuke-fedora.service"
)
SKILL_DOC = "docs/skills/desktop-contract.md"


def unit_files():
    """Every systemd unit file shipped in the image, as a sorted list."""
    found = []
    for top in UNIT_ROOTS:
        base = ROOT / top
        if not base.is_dir():
            continue
        found.extend(sorted(p for p in base.rglob("*") if p.suffix in (".service", ".timer")))
    return found


def directive_lines(text: str, name: str) -> list[str]:
    """Value of every `name=` directive, ignoring commented-out lines.

    systemd treats a `#` at the start of a line as a comment, so a directive
    only counts when it is the first non-blank character on the line. A
    convention that a comment mentioning `SuccessExitStatus=1` can trip is a
    convention nobody will keep.
    """
    values = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(name + "="):
            values.append(stripped[len(name) + 1:].strip())
    return values


class UnitExitToleranceTests(unittest.TestCase):
    def setUp(self):
        self.units = unit_files()
        self.assertTrue(self.units, "no unit files found to scan")

    def test_nuke_fedora_exists(self):
        """The unit this convention was written for is still in the image."""
        self.assertIn(NUKE_FEDORA, self.units)

    def test_no_unit_masks_exits_unit_wide(self):
        """`SuccessExitStatus=` is unit-wide, so no unit may carry one."""
        offenders = []
        for unit in self.units:
            for value in directive_lines(unit.read_text(), "SuccessExitStatus"):
                offenders.append(f"{unit.relative_to(ROOT)}: SuccessExitStatus={value}")
        self.assertEqual(offenders, [], "use the `ExecStart=-` prefix instead:\n" + "\n".join(offenders))

    def test_no_unit_forces_a_remote_delete(self):
        """`--force` deletes a remote together with the apps installed from it."""
        offenders = []
        for unit in self.units:
            for value in directive_lines(unit.read_text(), "ExecStart"):
                if "remote-delete" in value and "--force" in value:
                    offenders.append(f"{unit.relative_to(ROOT)}: ExecStart={value}")
        self.assertEqual(offenders, [], "drop --force and ignore the exit instead:\n" + "\n".join(offenders))

    def test_remote_deletes_tolerate_a_missing_remote(self):
        """Every remote-delete carries the `-` prefix, so a re-run is a no-op."""
        for unit in self.units:
            for value in directive_lines(unit.read_text(), "ExecStart"):
                if "remote-delete" not in value:
                    continue
                with self.subTest(unit=unit.relative_to(ROOT), command=value):
                    self.assertTrue(
                        value.startswith("-"),
                        f"{unit.relative_to(ROOT)}: {value} must be prefixed with `-` "
                        "so an already-absent remote does not fail the unit",
                    )


class NukeFedoraServiceTests(unittest.TestCase):
    """The unit the convention was written for, asserted command by command."""

    def setUp(self):
        self.text = NUKE_FEDORA.read_text()

    def test_stamp_directory_is_created_first(self):
        """`/var/lib/flatpak` is absent on a fresh image; mkdir runs before touch."""
        pres = directive_lines(self.text, "ExecStartPre")
        self.assertTrue(any("mkdir" in v and "/var/lib/flatpak" in v for v in pres), pres)

    def test_stamp_is_not_masked(self):
        """The `touch` that stamps the marker must not be prefixed with `-`."""
        stamps = [v for v in directive_lines(self.text, "ExecStart") if ".fedora-initialized" in v]
        self.assertEqual(len(stamps), 1, stamps)
        self.assertFalse(stamps[0].startswith("-"), stamps[0])

    def test_both_fedora_remotes_are_deleted(self):
        """The unit exists to remove both Fedora remotes, tolerating neither
        being present."""
        deleted = [v for v in directive_lines(self.text, "ExecStart") if "remote-delete" in v]
        for remote in ("fedora", "fedora-testing"):
            with self.subTest(remote=remote):
                self.assertTrue(
                    any(v.startswith("-") and v.endswith(remote) for v in deleted),
                    f"no `-`-prefixed remote-delete for {remote}: {deleted}",
                )

    def test_rationale_points_at_the_skill_doc(self):
        """A future editor must be able to find why the variants are wrong."""
        self.assertIn(SKILL_DOC, self.text)


if __name__ == "__main__":
    unittest.main()
