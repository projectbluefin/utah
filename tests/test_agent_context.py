"""The shipped skill set must agree with explicit source opt-ins both ways."""
import importlib.util
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
