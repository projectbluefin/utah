"""Tests for scripts/generate_skill_index.py.

`python3 scripts/generate_skill_index.py --check` is a required gate: it runs
inside `just check`, which `.github/workflows/build.yml` runs on every push and
pull request. It is also the only supported producer of `docs/skills/index.json`
and `docs/skills/index.md`. Before this suite no test named the script at all --
coverage reported 0% of its 106 statements -- so every refusal it is supposed to
make (missing front matter, missing required keys, an `entry_point` that does
not match the file's own path, a catalog the schema rejects, a stale committed
index) was unproven.

These tests execute the script. The module reads its inputs through module-level
path constants, so each test points those constants at a temporary skills tree
and asserts on what the code actually produces. The schema is the real committed
`docs/skills/index.schema.json`, not a copy, so a change to the shipped schema
is felt here.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "generate_skill_index.py"
REAL_SCHEMA = ROOT / "docs" / "skills" / "index.schema.json"


def load_module():
    """Import the script by path; its filename is not a valid module name."""
    spec = importlib.util.spec_from_file_location("generate_skill_index", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def front_matter(**overrides) -> str:
    """Front matter for one valid skill, with per-test overrides."""
    fields = {
        "id": "example-skill",
        "name": "example-skill",
        "one_line_purpose": "Do the example thing.",
        "entry_point": "docs/skills/example-skill.md",
        "category": "testing",
        "status": "active",
        "tags": ["example"],
        "description": "An example skill used by the unit tests.",
        "version": "1.0",
        "last_updated": "2026-01-02",
    }
    fields.update(overrides)
    lines = ["---"]
    for key, value in fields.items():
        if value is None:
            continue
        lines.append(f"{key}: {json.dumps(value)}")
    lines.append("---")
    lines.append("")
    lines.append("# Example")
    lines.append("")
    return "\n".join(lines)


class SkillsTreeTestCase(unittest.TestCase):
    """Base case: a temporary repo root whose schema is the committed one."""

    def setUp(self) -> None:
        self.module = load_module()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)
        self.skills_dir = self.repo_root / "docs" / "skills"
        self.skills_dir.mkdir(parents=True)

        self.patch_paths(self.repo_root)

    def patch_paths(self, repo_root: Path) -> None:
        skills_dir = repo_root / "docs" / "skills"
        for name, value in (
            ("REPO_ROOT", repo_root),
            ("SKILLS_DIR", skills_dir),
            ("SCHEMA_PATH", REAL_SCHEMA),
            ("INDEX_PATH", skills_dir / "index.json"),
        ):
            patcher = patch.object(self.module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_skill(self, relpath: str, **overrides) -> Path:
        """Write a skill doc at `relpath` (relative to the skills dir)."""
        path = self.skills_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        overrides.setdefault("entry_point", f"docs/skills/{relpath}")
        path.write_text(front_matter(**overrides))
        return path


class FindSkillFilesTests(SkillsTreeTestCase):
    def test_collects_flat_pages_and_per_directory_skills(self):
        self.write_skill("beta.md", id="beta")
        self.write_skill("alpha.md", id="alpha")
        self.write_skill("gamma/SKILL.md", id="gamma")

        found = [p.relative_to(self.skills_dir).as_posix()
                 for p in self.module.find_skill_files()]

        # Flat pages sort first (both groups sorted), directories appended.
        self.assertEqual(found, ["alpha.md", "beta.md", "gamma/SKILL.md"])

    def test_excludes_the_generated_index_page(self):
        self.write_skill("alpha.md", id="alpha")
        (self.skills_dir / "index.md").write_text("# generated mirror\n")

        found = [p.name for p in self.module.find_skill_files()]

        self.assertEqual(found, ["alpha.md"])

    def test_ignores_non_markdown_and_nested_non_skill_files(self):
        self.write_skill("alpha.md", id="alpha")
        (self.skills_dir / "index.schema.json").write_text("{}")
        (self.skills_dir / "gamma").mkdir()
        (self.skills_dir / "gamma" / "notes.md").write_text("not a SKILL.md\n")

        found = [p.name for p in self.module.find_skill_files()]

        self.assertEqual(found, ["alpha.md"])


class ParseFrontMatterTests(SkillsTreeTestCase):
    def test_parses_leading_yaml_block(self):
        path = self.write_skill("alpha.md", id="alpha")

        data = self.module.parse_front_matter(path)

        self.assertEqual(data["id"], "alpha")
        self.assertEqual(data["tags"], ["example"])

    def test_rejects_a_file_without_front_matter(self):
        path = self.skills_dir / "alpha.md"
        path.write_text("# No front matter here\n")

        with self.assertRaises(ValueError) as caught:
            self.module.parse_front_matter(path)

        self.assertIn("no YAML front matter", str(caught.exception))

    def test_rejects_front_matter_that_is_not_a_mapping(self):
        path = self.skills_dir / "alpha.md"
        path.write_text("---\n- one\n- two\n---\n\n# Body\n")

        with self.assertRaises(ValueError) as caught:
            self.module.parse_front_matter(path)

        self.assertIn("did not parse to a mapping", str(caught.exception))

    def test_front_matter_must_lead_the_file(self):
        """A `---` block that is not at position 0 is not front matter."""
        path = self.skills_dir / "alpha.md"
        path.write_text("# Title\n\n---\nid: alpha\n---\n")

        with self.assertRaises(ValueError):
            self.module.parse_front_matter(path)


class BuildSkillEntryTests(SkillsTreeTestCase):
    def test_builds_the_catalog_entry_from_front_matter(self):
        path = self.write_skill("alpha.md", id="alpha", name="alpha")

        entry = self.module.build_skill_entry(path)

        self.assertEqual(entry["id"], "alpha")
        self.assertEqual(entry["entry_point"], "docs/skills/alpha.md")
        self.assertEqual(entry["category"], "testing")
        self.assertNotIn("doc_type", entry)

    def test_collapses_wrapped_description_whitespace(self):
        path = self.write_skill(
            "alpha.md", id="alpha",
            description="one   two\nthree\n\n  four ",
        )

        entry = self.module.build_skill_entry(path)

        self.assertEqual(entry["description"], "one two three four")

    def test_coerces_version_and_last_updated_to_strings(self):
        path = self.skills_dir / "alpha.md"
        # Unquoted YAML scalars parse as a float and a datetime.date.
        path.write_text(
            "---\n"
            "id: alpha\n"
            "name: alpha\n"
            "one_line_purpose: Do the thing.\n"
            "entry_point: docs/skills/alpha.md\n"
            "category: testing\n"
            "status: active\n"
            "tags: [example]\n"
            "description: An example.\n"
            "version: 1.0\n"
            "last_updated: 2026-01-02\n"
            "---\n"
        )

        entry = self.module.build_skill_entry(path)

        self.assertEqual(entry["version"], "1.0")
        self.assertEqual(entry["last_updated"], "2026-01-02")
        self.assertIsInstance(entry["version"], str)
        self.assertIsInstance(entry["last_updated"], str)

    def test_carries_metadata_type_through_as_doc_type(self):
        path = self.skills_dir / "alpha.md"
        path.write_text(
            front_matter(id="alpha", entry_point="docs/skills/alpha.md")
            .replace("---\n\n# Example",
                     'metadata: {"type": "procedure"}\n---\n\n# Example')
        )

        entry = self.module.build_skill_entry(path)

        self.assertEqual(entry["doc_type"], "procedure")

    def test_tolerates_a_null_metadata_block(self):
        path = self.skills_dir / "alpha.md"
        path.write_text(
            front_matter(id="alpha", entry_point="docs/skills/alpha.md")
            .replace("---\n\n# Example", "metadata:\n---\n\n# Example")
        )

        entry = self.module.build_skill_entry(path)

        self.assertNotIn("doc_type", entry)

    def test_names_every_missing_required_key(self):
        path = self.skills_dir / "alpha.md"
        path.write_text("---\nid: alpha\nname: alpha\n---\n")

        with self.assertRaises(ValueError) as caught:
            self.module.build_skill_entry(path)

        message = str(caught.exception)
        self.assertIn("missing required front-matter key(s)", message)
        self.assertIn("entry_point", message)
        self.assertIn("last_updated", message)

    def test_rejects_an_entry_point_that_does_not_match_the_file(self):
        path = self.write_skill("alpha.md", id="alpha",
                                entry_point="docs/skills/renamed.md")

        with self.assertRaises(ValueError) as caught:
            self.module.build_skill_entry(path)

        message = str(caught.exception)
        self.assertIn("entry_point", message)
        self.assertIn("docs/skills/renamed.md", message)
        self.assertIn("docs/skills/alpha.md", message)


class BuildCatalogTests(SkillsTreeTestCase):
    def test_sorts_skills_by_id_without_timestamp(self):
        self.write_skill("zeta.md", id="zeta")
        self.write_skill("alpha.md", id="alpha")

        catalog = self.module.build_catalog()

        self.assertEqual([s["id"] for s in catalog["skills"]], ["alpha", "zeta"])
        self.assertEqual(catalog["schema_version"], self.module.SCHEMA_VERSION)
        self.assertNotIn("generated_at", catalog)

    def test_an_empty_skills_tree_produces_an_empty_catalog(self):
        catalog = self.module.build_catalog()

        self.assertEqual(catalog["skills"], [])


class ValidateCatalogTests(SkillsTreeTestCase):
    def test_accepts_a_catalog_the_committed_schema_allows(self):
        self.write_skill("alpha.md", id="alpha")

        self.module.validate_catalog(self.module.build_catalog())

    def test_rejects_a_catalog_the_schema_refuses_and_reports_the_location(self):
        self.write_skill("alpha.md", id="Alpha_Skill")  # not kebab-case
        catalog = self.module.build_catalog()

        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            self.module.validate_catalog(catalog)

        self.assertEqual(caught.exception.code, 1)
        self.assertIn("schema error at", stderr.getvalue())
        self.assertIn("id", stderr.getvalue())

    def test_reports_the_root_when_a_top_level_key_is_wrong(self):
        catalog = {"skills": []}  # missing schema_version

        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            self.module.validate_catalog(catalog)

        self.assertIn("schema error at <root>", stderr.getvalue())


class RenderMarkdownTests(SkillsTreeTestCase):
    def test_renders_a_table_row_per_skill_with_relative_links(self):
        self.write_skill("alpha.md", id="alpha")
        self.write_skill("gamma/SKILL.md", id="gamma")
        catalog = self.module.build_catalog()

        markdown = self.module.render_markdown(catalog)

        self.assertIn("| [alpha](alpha.md) | testing | active |", markdown)
        self.assertIn("| [gamma](gamma/SKILL.md) | testing | active |", markdown)
        self.assertNotIn("docs/skills/", markdown.split("|---|")[1])

    def test_header_states_the_date_schema_and_skill_count(self):
        self.write_skill("alpha.md", id="alpha")
        catalog = self.module.build_catalog()

        markdown = self.module.render_markdown(catalog)

        self.assertIn(
            f"schema {self.module.SCHEMA_VERSION} · 1 skills",
            markdown,
        )

    def test_rendered_markdown_ends_with_a_trailing_newline(self):
        self.write_skill("alpha.md", id="alpha")

        markdown = self.module.render_markdown(self.module.build_catalog())

        self.assertTrue(markdown.endswith("\n"))


class MainTests(SkillsTreeTestCase):
    def run_main(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["generate_skill_index.py", *argv]):
            with redirect_stdout(out), redirect_stderr(err):
                code = self.module.main()
        return code, out.getvalue(), err.getvalue()

    @property
    def md_path(self) -> Path:
        return self.module.SKILLS_DIR / "index.md"

    def test_write_produces_both_generated_files(self):
        self.write_skill("alpha.md", id="alpha")

        code, stdout, _ = self.run_main("--write")

        self.assertEqual(code, 0)
        self.assertIn("1 skills", stdout)
        catalog = json.loads(self.module.INDEX_PATH.read_text())
        self.assertEqual([s["id"] for s in catalog["skills"]], ["alpha"])
        self.assertTrue(self.module.INDEX_PATH.read_text().endswith("\n"))
        self.assertIn("| [alpha](alpha.md) |", self.md_path.read_text())

    def test_check_passes_immediately_after_write(self):
        self.write_skill("alpha.md", id="alpha")
        self.assertEqual(self.run_main("--write")[0], 0)

        code, stdout, _ = self.run_main("--check")

        self.assertEqual(code, 0)
        self.assertIn("up to date", stdout)

    def test_check_fails_when_a_new_skill_is_not_in_the_index(self):
        self.write_skill("alpha.md", id="alpha")
        self.run_main("--write")
        self.write_skill("beta.md", id="beta")

        code, _, stderr = self.run_main("--check")

        self.assertEqual(code, 1)
        self.assertIn("docs/skills/index.json is stale", stderr)

    def test_check_fails_when_only_the_markdown_mirror_is_stale(self):
        self.write_skill("alpha.md", id="alpha")
        self.run_main("--write")
        self.md_path.write_text("# hand-edited\n")

        code, _, stderr = self.run_main("--check")

        self.assertEqual(code, 1)
        self.assertNotIn("index.json is stale", stderr)
        self.assertIn("docs/skills/index.md is stale", stderr)

    def test_check_fails_when_neither_generated_file_exists(self):
        self.write_skill("alpha.md", id="alpha")

        code, _, stderr = self.run_main("--check")

        self.assertEqual(code, 1)
        self.assertIn("index.json is stale", stderr)
        self.assertIn("index.md is stale", stderr)

    def test_check_does_not_write_anything(self):
        self.write_skill("alpha.md", id="alpha")

        self.run_main("--check")

        self.assertFalse(self.module.INDEX_PATH.exists())
        self.assertFalse(self.md_path.exists())

    def test_catalog_is_deterministic_without_timestamp(self):
        """The catalog contains no timestamp, ensuring deterministic --check."""
        self.write_skill("alpha.md", id="alpha")
        self.run_main("--write")
        committed = json.loads(self.module.INDEX_PATH.read_text())
        self.assertNotIn("generated_at", committed)

        code, _, stderr = self.run_main("--check")

        self.assertEqual(code, 0, stderr)

    def test_a_malformed_skill_doc_fails_the_gate_without_writing(self):
        (self.skills_dir / "alpha.md").write_text("# no front matter\n")

        code, _, stderr = self.run_main("--check")

        self.assertEqual(code, 1)
        self.assertIn("error:", stderr)
        self.assertIn("no YAML front matter", stderr)
        self.assertFalse(self.module.INDEX_PATH.exists())

    def test_write_refuses_to_emit_a_catalog_the_schema_rejects(self):
        self.write_skill("alpha.md", id="Alpha_Skill")

        with self.assertRaises(SystemExit) as caught:
            self.run_main("--write")

        self.assertEqual(caught.exception.code, 1)
        self.assertFalse(self.module.INDEX_PATH.exists())

    def test_a_mode_is_required(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_main()

        self.assertEqual(caught.exception.code, 2)

    def test_write_and_check_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_main("--write", "--check")

        self.assertEqual(caught.exception.code, 2)


class CommittedCatalogTests(unittest.TestCase):
    """The repository's own catalog must satisfy the shipped schema."""

    def test_committed_index_matches_the_committed_schema(self):
        module = load_module()
        catalog = json.loads((ROOT / "docs" / "skills" / "index.json").read_text())

        module.validate_catalog(catalog)


if __name__ == "__main__":
    unittest.main()
