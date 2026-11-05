"""The shipped skill set must agree with explicit source opt-ins both ways."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("agent_context", ROOT / "scripts/generate_agent_context.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DeviceCatalogTests(unittest.TestCase):
    def test_selection_and_missing_or_extra_editions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "skills").mkdir()
            source = root / "source.md"
            source.write_text("---\nid: desktop\none_line_purpose: Desktop guidance\n"
                              "on_device: docs/agent/skills/desktop.md\n---\n")
            with self.assertRaisesRegex(ValueError, "missing device edition"):
                module.catalog([source], root)
            device = root / "skills/desktop.md"
            device.write_text("# Device instructions\n")
            entries = module.catalog([source], root)["skills"]
            self.assertEqual(entries[0]["entry_point"], "skills/desktop.md")
            source.write_text("---\nid: desktop\n---\n")
            with self.assertRaisesRegex(ValueError, "unmarked device skill"):
                module.catalog([source], root)

    def test_image_stages_bundle_and_build_inputs(self):
        containerfile = (ROOT / "Containerfile").read_text()
        for line in (
            "COPY docs/agent /usr/share/utah/agent",
            "COPY Containerfile /usr/share/utah/agent/build-source.Containerfile",
            "COPY .gitmodules /usr/share/utah/agent/gitmodules",
            "COPY config/flavors.json /usr/share/utah/agent/flavors.json",
        ):
            self.assertIn(line, containerfile.splitlines())
        self.assertIn("cp -a /tmp/utah-inputs/usr/. /usr/", containerfile)
        self.assertIn("python3 scripts/generate_agent_context.py --check", (ROOT / "Justfile").read_text())


class CatalogRefusalTests(unittest.TestCase):
    def write_source(self, root, name, skill_id, on_device):
        source = root / name
        source.write_text(f"---\nid: {skill_id}\none_line_purpose: {skill_id} guidance\n"
                          f"on_device: {on_device}\n---\n")
        return source

    def test_on_device_path_must_name_the_skill_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "skills").mkdir()
            (root / "skills/desktop.md").write_text("# Device\n")
            source = self.write_source(root, "source.md", "desktop",
                                       "docs/agent/skills/other.md")
            with self.assertRaisesRegex(ValueError, "on_device must be docs/agent/skills/desktop.md"):
                module.catalog([source], root)

    def test_two_sources_claiming_one_id_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "skills").mkdir()
            (root / "skills/desktop.md").write_text("# Device\n")
            sources = [self.write_source(root, name, "desktop", "docs/agent/skills/desktop.md")
                       for name in ("a.md", "b.md")]
            with self.assertRaisesRegex(ValueError, "duplicate device skill id"):
                module.catalog(sources, root)


class MainTests(unittest.TestCase):
    """`just check` runs `--check` on the real tree; these drive its refusals."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bundle = self.root / "docs/agent"
        (self.bundle / "skills").mkdir(parents=True)
        (self.bundle / "skills/desktop.md").write_text("# Device\n")
        self.source = self.root / "source.md"
        self.source.write_text("---\nid: desktop\none_line_purpose: Desktop guidance\n"
                               "on_device: docs/agent/skills/desktop.md\n---\n")
        (self.root / "contracts").mkdir()
        self.write_contract(["/usr/share/utah/agent/skills/desktop.md"])
        catalog = module.catalog
        # catalog()'s bundle default is bound at import, so redirect it too.
        for name, value in (("ROOT", self.root), ("BUNDLE", self.bundle),
                            ("find_skill_files", lambda: [self.source]),
                            ("catalog", lambda sources: catalog(sources, self.bundle))):
            original = getattr(module, name)
            setattr(module, name, value)
            self.addCleanup(setattr, module, name, original)

    def write_contract(self, files):
        listed = ", ".join(f'"{path}"' for path in files)
        (self.root / "contracts/bluefin-desktop.toml").write_text(
            f"[branding]\nfiles = [{listed}]\n")

    def run_main(self, *args):
        argv, sys.argv = sys.argv, ["generate_agent_context.py", *args]
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = module.main()
                except SystemExit as exc:
                    code = exc.code
        finally:
            sys.argv = argv
        return code, out.getvalue(), err.getvalue()

    def test_write_then_check_round_trips(self):
        code, out, err = self.run_main("--write")
        self.assertEqual(code, 0, err)
        written = json.loads((self.bundle / "index.json").read_text())
        self.assertEqual(written, {"schema_version": "1.0", "skills": [{
            "id": "desktop", "entry_point": "skills/desktop.md",
            "description": "Desktop guidance"}]})
        code, out, err = self.run_main("--check")
        self.assertEqual(code, 0, err)
        self.assertIn("Device skill catalog is current", out)

    def test_check_refuses_missing_or_stale_catalog(self):
        code, _, err = self.run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("stale device catalog", err)
        self.assertEqual(self.run_main("--write")[0], 0)
        index = self.bundle / "index.json"
        index.write_text(index.read_text().replace("Desktop guidance", "Old guidance"))
        code, out, err = self.run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("stale device catalog", err)
        self.assertNotIn("current", out)

    def test_skill_missing_from_image_contract_is_refused_before_writing(self):
        self.write_contract(["/usr/share/utah/agent/skills/other.md"])
        for mode in ("--write", "--check"):
            with self.subTest(mode=mode):
                code, _, err = self.run_main(mode)
                self.assertEqual(code, 1)
                self.assertIn("device skill missing from image contract: "
                              "/usr/share/utah/agent/skills/desktop.md", err)
                self.assertFalse((self.bundle / "index.json").exists())

    def test_catalog_refusal_surfaces_as_clean_exit(self):
        (self.bundle / "skills/stray.md").write_text("# Unmarked\n")
        code, _, err = self.run_main("--check")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: unmarked device skill files"), err)
