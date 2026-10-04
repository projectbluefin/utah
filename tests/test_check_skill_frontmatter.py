"""scripts/check-skill-frontmatter.sh must reject every malformed skill page.

`just check` runs the script on every docs/skills page, but in CI it only ever
sees the committed pages, which all pass. Its failure branches -- missing front
matter, a missing required key, a missing metadata.type, an over-long
description, an over-budget page -- never ran, so a regression that made any of
them pass silently would not be caught. These tests run the real script
against scratch docs/skills trees and assert on its exit code and output.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-skill-frontmatter.sh"

MAX_DESC = 256


def page(description: str = "description: A skill.", *, body_lines: int = 1,
         drop: str | None = None, metadata: str = "metadata:\n  type: skill") -> str:
    """A skill page whose front matter passes unless a part is overridden."""
    keys = {
        "name": "name: demo",
        "version": "version: 1.0.0",
        "last_updated": "last_updated: 2026-01-01",
        "tags": "tags: [demo]",
        "description": description,
    }
    if drop:
        del keys[drop]
    fm = "\n".join([*keys.values(), metadata]).strip("\n")
    body = "\n".join(["# Demo", *["text"] * (body_lines - 1)])
    return f"---\n{fm}\n---\n{body}\n"


class CheckSkillFrontmatterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.skills = self.root / "docs" / "skills"
        self.skills.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, text: str) -> Path:
        path = self.skills / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def run_check(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPT)], cwd=self.root, capture_output=True, text=True,
        )

    def assert_passes(self) -> str:
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("error:", result.stdout)
        return result.stdout

    def assert_fails_with(self, message: str) -> None:
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(message, result.stdout)


class PassingPagesTest(CheckSkillFrontmatterTest):
    def test_empty_skills_directory_passes(self) -> None:
        self.assert_passes()

    def test_flat_page_with_full_front_matter_passes(self) -> None:
        self.write("demo.md", page())
        self.assertEqual(self.assert_passes(), "")

    def test_per_skill_directory_page_passes(self) -> None:
        self.write("demo/SKILL.md", page())
        self.assert_passes()

    def test_generated_index_md_is_skipped(self) -> None:
        """docs/skills/index.md is the generated catalog mirror, not a skill."""
        self.write("index.md", "# Catalog\n")
        self.assert_passes()

    def test_nested_non_skill_markdown_is_not_checked(self) -> None:
        """Only <dir>/SKILL.md is a skill; references beside it are not."""
        self.write("demo/SKILL.md", page())
        self.write("demo/references/notes.md", "# Notes, no front matter\n")
        self.assert_passes()

    def test_real_skill_pages_pass(self) -> None:
        """The committed docs tree is the gate's own fixture in CI."""
        result = subprocess.run(
            ["bash", str(SCRIPT)], cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class FrontMatterTest(CheckSkillFrontmatterTest):
    def test_page_without_front_matter_fails(self) -> None:
        self.write("demo.md", "# Demo\n")
        self.assert_fails_with("error: docs/skills/demo.md has no front-matter")

    def test_empty_front_matter_fails(self) -> None:
        self.write("demo.md", "---\n---\n# Demo\n")
        self.assert_fails_with("has no front-matter")

    def test_per_skill_page_without_front_matter_fails(self) -> None:
        self.write("demo/SKILL.md", "# Demo\n")
        self.assert_fails_with("error: docs/skills/demo/SKILL.md has no front-matter")

    def test_each_required_key_is_enforced(self) -> None:
        for key in ("name", "version", "last_updated", "tags", "description"):
            with self.subTest(key=key):
                self.write("demo.md", page(drop=key))
                self.assert_fails_with(
                    f"error: docs/skills/demo.md missing required key '{key}'"
                )

    def test_key_outside_front_matter_does_not_count(self) -> None:
        """A `version:` line in the body must not satisfy the front-matter check."""
        self.write("demo.md", page(drop="version") + "version: 9\n")
        self.assert_fails_with("missing required key 'version'")

    def test_missing_metadata_block_fails(self) -> None:
        self.write("demo.md", page(metadata=""))
        self.assert_fails_with("error: docs/skills/demo.md missing metadata.type")

    def test_metadata_without_type_fails(self) -> None:
        self.write("demo.md", page(metadata="metadata:\n  owner: utah"))
        self.assert_fails_with("missing metadata.type")

    def test_every_bad_page_is_reported_not_just_the_first(self) -> None:
        self.write("a.md", "# A\n")
        self.write("b.md", page(drop="tags"))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("docs/skills/a.md has no front-matter", result.stdout)
        self.assertIn("docs/skills/b.md missing required key 'tags'", result.stdout)


class DescriptionLengthTest(CheckSkillFrontmatterTest):
    def test_inline_description_at_limit_passes(self) -> None:
        self.write("demo.md", page(f"description: {'x' * MAX_DESC}"))
        self.assert_passes()

    def test_inline_description_over_limit_fails(self) -> None:
        self.write("demo.md", page(f"description: {'x' * (MAX_DESC + 1)}"))
        self.assert_fails_with(
            f"error: docs/skills/demo.md description is {MAX_DESC + 1} chars (max {MAX_DESC})"
        )

    def test_surrounding_quotes_are_not_counted(self) -> None:
        for quote in ('"', "'"):
            with self.subTest(quote=quote):
                self.write("demo.md", page(f"description: {quote}{'x' * MAX_DESC}{quote}"))
                self.assert_passes()

    def test_folded_description_lines_are_joined_and_measured(self) -> None:
        """A folded description that is far over the limit must still fail."""
        lines = "\n".join(f"  {'x' * 100}" for _ in range(3))
        self.write("demo.md", page(f"description: >\n{lines}"))
        self.assert_fails_with("description is")

    @unittest.expectedFailure
    def test_folded_description_under_limit_passes(self) -> None:
        """A 255-char folded description is under the 256 limit.

        Known bug: the leading `> ` block indicator is counted, so this is
        reported as 257 chars. Remove this decorator once the script strips the
        indicator; the unexpected success will fail the suite until then.
        """
        self.write("demo.md", page(f"description: >\n  {'x' * (MAX_DESC - 1)}"))
        self.assert_passes()


class SizeBudgetTest(CheckSkillFrontmatterTest):
    FM_LINES = 9  # page()'s front matter, including both '---' fences

    def lines(self, total: int) -> str:
        return page(body_lines=total - self.FM_LINES)

    def test_page_at_soft_limit_is_silent(self) -> None:
        self.write("demo.md", self.lines(200))
        self.assertEqual(self.assert_passes(), "")

    def test_page_over_soft_limit_warns_but_passes(self) -> None:
        self.write("demo.md", self.lines(201))
        out = self.assert_passes()
        self.assertIn("warning: docs/skills/demo.md is 201 lines (soft max 200)", out)

    def test_page_at_hard_limit_warns_but_passes(self) -> None:
        self.write("demo.md", self.lines(500))
        out = self.assert_passes()
        self.assertIn("is 500 lines (soft max 200)", out)

    def test_page_over_hard_limit_fails(self) -> None:
        self.write("demo.md", self.lines(501))
        self.assert_fails_with(
            "error: docs/skills/demo.md is 501 lines (hard max 500)"
        )


if __name__ == "__main__":
    unittest.main()
