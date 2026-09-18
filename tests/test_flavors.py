"""Behavioral coverage for scripts/flavors.py.

flavors.py is the single source for four things that must agree: the build
matrix, the promote matrix, the release matrix, and whether the kernel cache
image is built at all. `just check` only runs `flavors.py list >/dev/null`,
which proves the file parses and nothing about what it answers -- a wrong
`needs-kernel` wastes a 45-minute kernel compile, and a wrong `list-kernel`
submits flavors against a base image that was never built.

Each query is exercised against synthetic config trees so the assertions state
the mapping rather than restate today's config/flavors.json, plus a pass over
the real config so the shipped file stays inside the contract.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "flavors.py"
REAL_CONFIG = ROOT / "config" / "flavors.json"

ALL_FLAVORS = ["main", "nvidia", "gaming", "nvidia-gaming"]


def run(script, *args):
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
    )


def build_tree(config):
    """Copy flavors.py next to a synthetic config so CONFIG can be varied.

    flavors.py resolves CONFIG from its own path, so it has to be copied into
    a throwaway tree rather than pointed at a different config.
    """
    root = Path(tempfile.mkdtemp())
    (root / "scripts").mkdir()
    (root / "config").mkdir()
    shutil.copy(SCRIPT, root / "scripts" / "flavors.py")
    payload = config if isinstance(config, str) else json.dumps(config)
    (root / "config" / "flavors.json").write_text(payload)
    return root / "scripts" / "flavors.py"


class QueryTests(unittest.TestCase):
    def test_list_returns_configured_flavors_in_order(self):
        cli = build_tree({"flavors": ["main", "nvidia"], "retired": {}})
        result = run(cli, "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["main", "nvidia"])

    def test_list_is_the_default_query(self):
        cli = build_tree({"flavors": ALL_FLAVORS, "retired": {}})
        result = run(cli)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ALL_FLAVORS)

    def test_images_maps_every_flavor_to_its_image_name(self):
        cli = build_tree({"flavors": ALL_FLAVORS, "retired": {}})
        result = run(cli, "images")
        self.assertEqual(json.loads(result.stdout), [
            {"image": "utah"},
            {"image": "utah-nvidia"},
            {"image": "utah-gaming"},
            {"image": "utah-nvidia-gaming"},
        ])

    def test_releases_promotes_testing_to_stable_for_every_flavor(self):
        cli = build_tree({"flavors": ["main", "gaming"], "retired": {}})
        result = run(cli, "releases")
        self.assertEqual(json.loads(result.stdout), [
            {"image": "utah", "source_tag": "testing", "target_tag": "stable"},
            {"image": "utah-gaming", "source_tag": "testing", "target_tag": "stable"},
        ])

    def test_needs_kernel_is_false_for_main_only(self):
        cli = build_tree({"flavors": ["main"], "retired": {}})
        self.assertEqual(run(cli, "needs-kernel").stdout.strip(), "false")

    def test_needs_kernel_is_true_for_any_non_main_flavor(self):
        for flavors in (["main", "nvidia"], ["gaming"], ["nvidia-gaming"]):
            cli = build_tree({"flavors": flavors, "retired": {}})
            self.assertEqual(
                run(cli, "needs-kernel").stdout.strip(), "true", flavors
            )

    def test_list_main_and_list_kernel_partition_the_flavor_set(self):
        cli = build_tree({"flavors": ALL_FLAVORS, "retired": {}})
        main = json.loads(run(cli, "list-main").stdout)
        kernel = json.loads(run(cli, "list-kernel").stdout)
        self.assertEqual(main, ["main"])
        self.assertEqual(kernel, ["nvidia", "gaming", "nvidia-gaming"])
        self.assertEqual(main + kernel, ALL_FLAVORS)
        self.assertEqual(set(main) & set(kernel), set())

    def test_list_main_is_empty_when_main_is_retired(self):
        cli = build_tree({"flavors": ["nvidia"], "retired": {"main": "off"}})
        self.assertEqual(json.loads(run(cli, "list-main").stdout), [])
        self.assertEqual(json.loads(run(cli, "list-kernel").stdout), ["nvidia"])

    def test_unknown_flavor_in_config_fails_every_query(self):
        cli = build_tree({"flavors": ["main", "utah-nvidia"], "retired": {}})
        for query in ("list", "images", "releases", "needs-kernel", "list-kernel"):
            result = run(cli, query)
            self.assertNotEqual(result.returncode, 0, f"{query} accepted it")
            self.assertIn("unknown flavor", result.stderr)
            self.assertIn("utah-nvidia", result.stderr)

    def test_unknown_query_fails_rather_than_printing_nothing(self):
        cli = build_tree({"flavors": ALL_FLAVORS, "retired": {}})
        result = run(cli, "list-gaming")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown query: list-gaming", result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_malformed_config_fails_loudly(self):
        cli = build_tree("{not json")
        result = run(cli, "list")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")


class ShippedConfigTests(unittest.TestCase):
    def test_shipped_config_only_names_known_flavors(self):
        config = json.loads(REAL_CONFIG.read_text())
        unknown = set(config["flavors"]) - set(ALL_FLAVORS)
        self.assertEqual(unknown, set())
        self.assertEqual(len(config["flavors"]), len(set(config["flavors"])))

    def test_shipped_config_agrees_across_all_queries(self):
        """The four consumers must describe the same set, from the real config."""
        flavors = json.loads(run(SCRIPT, "list").stdout)
        images = json.loads(run(SCRIPT, "images").stdout)
        releases = json.loads(run(SCRIPT, "releases").stdout)
        main = json.loads(run(SCRIPT, "list-main").stdout)
        kernel = json.loads(run(SCRIPT, "list-kernel").stdout)

        self.assertEqual(len(images), len(flavors))
        self.assertEqual([r["image"] for r in releases], [i["image"] for i in images])
        self.assertEqual(main + kernel, flavors)
        self.assertEqual(
            run(SCRIPT, "needs-kernel").stdout.strip(),
            "true" if kernel else "false",
        )


if __name__ == "__main__":
    unittest.main()