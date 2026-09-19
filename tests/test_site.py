"""The status page must not be able to lie about the image.

Two failure modes are worth guarding. The first is drift: the package grid is
generated from the manifests, so a manifest change without a regeneration
would publish a list that no longer matches what the image installs. The
second is a false green: the page fetches build status at runtime, and a
network failure must read as "unavailable" rather than as passing.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
GENERATOR = ROOT / "scripts" / "generate-site-data.py"


def load_generator():
    spec = importlib.util.spec_from_file_location("generate_site_data", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_site_data"] = module
    spec.loader.exec_module(module)
    return module


class GeneratedDataTests(unittest.TestCase):
    def setUp(self):
        self.generator = load_generator()
        self.data = json.loads((SITE / "data/packages.json").read_text())

    def test_committed_data_matches_the_manifests(self):
        # The same check CI runs; asserting it here means a contributor sees it
        # from `just test` rather than from a red deployment.
        result = subprocess.run(
            [sys.executable, str(GENERATOR), "--check"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_totals_match_what_the_installer_resolves(self):
        spec = importlib.util.spec_from_file_location(
            "install_packages", ROOT / "scripts/install-packages.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        contract = installer.contract(
            ROOT / "packages/bluefin.toml", ROOT / "packages/utah.toml", None)
        self.assertEqual(self.data["totals"]["installed"], len(contract))
        # Every name the page counts as shipped is one the image asks for.
        # The build toolchain is excluded: configure-services.sh removes it
        # before the image ships, so counting it would overstate the payload.
        shown = {name for group in self.data["groups"] if not group["transient"]
                 for name in group["packages"]}
        self.assertEqual(shown, set(contract))

    def test_the_build_toolchain_is_marked_transient_and_not_counted(self):
        build = next(g for g in self.data["groups"] if g["id"] == "build")
        self.assertTrue(build["transient"])
        removal = [line for line in (ROOT / "scripts/configure-services.sh").read_text().splitlines()
                   if "remove --no-autoremove" in line]
        self.assertEqual(len(removal), 1)
        # Everything in the group is either removed again or kept for a
        # documented reason (unzip is also in [parity], which the image ships).
        kept = {name for g in self.data["groups"] if not g["transient"]
                for name in g["packages"]}
        for name in build["packages"]:
            with self.subTest(package=name):
                self.assertTrue(name in removal[0] or name in kept,
                                f"{name} is neither removed nor shipped by another section")

    def test_unavailable_entries_are_the_manifest_gaps(self):
        spec = importlib.util.spec_from_file_location(
            "install_packages", ROOT / "scripts/install-packages.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        declared = set(installer.section(ROOT / "packages/utah.toml", "unavailable"))
        self.assertEqual({gap["name"] for gap in self.data["unavailable"]}, declared)

    def test_no_installed_package_is_also_a_gap(self):
        shown = {name for group in self.data["groups"] for name in group["packages"]}
        gaps = {gap["name"] for gap in self.data["unavailable"]}
        self.assertEqual(shown & gaps, set())

    def test_tracking_issues_come_only_from_the_tracked_by_clause(self):
        """ppp's entry cites #104 and #100 to draw a comparison and is tracked
        by #107. Scraping every number in the paragraph attributed all three to
        ppp, which sent readers to the wrong issues."""
        reasons = self.generator.unavailable_reasons(ROOT / "packages/utah.toml")
        self.assertEqual(reasons.get("ppp"), [107])
        # Cross-repository references (utah-packages#112) are not this repo's
        # issue numbers and must not be rendered as links into it.
        self.assertEqual(reasons.get("firefox"), [35])

    def test_stale_data_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            stale = Path(tmp) / "packages.json"
            payload = dict(self.data)
            payload["groups"] = []
            stale.write_text(json.dumps(payload))
            result = subprocess.run(
                [sys.executable, str(GENERATOR), "--check", "--output", str(stale)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("stale", result.stderr)

    def test_a_date_change_alone_is_not_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            dated = Path(tmp) / "packages.json"
            payload = dict(self.data)
            payload["generated_at"] = "1999-12-31"
            dated.write_text(json.dumps(payload))
            result = subprocess.run(
                [sys.executable, str(GENERATOR), "--check", "--output", str(dated)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


class PageTests(unittest.TestCase):
    def setUp(self):
        self.html = (SITE / "index.html").read_text()
        self.js = (SITE / "app.js").read_text()
        self.css = (SITE / "styles.css").read_text()

    def test_every_local_reference_exists(self):
        refs = set(re.findall(r'(?:src|href)="(?!https?:|#|mailto:)([^"]+)"', self.html))
        refs |= set(re.findall(r'srcset="([^"]+)"', self.html))
        paths = set()
        for ref in refs:
            for candidate in ref.split(","):
                path = candidate.strip().split(" ")[0]
                if path:
                    paths.add(path)
        for path in sorted(paths):
            with self.subTest(path=path):
                self.assertTrue((SITE / path).is_file(), f"{path} is referenced but missing")

    def test_the_hero_uses_the_utahraptor_with_a_fallback_and_alt_text(self):
        self.assertIn("utahraptor-1400.webp", self.html)
        # A <picture> whose only source is WebP shows nothing where WebP is
        # unsupported, so the <img> fallback has to be a PNG.
        self.assertRegex(self.html, r'<img class="raptor" src="assets/utahraptor-\d+\.png"')
        alt = re.search(r'<img class="raptor"[^>]*alt="([^"]*)"', self.html)
        self.assertIsNotNone(alt)
        self.assertGreater(len(alt.group(1)), 20, "the hero needs real alt text")

    def test_status_sources_are_declared_in_one_place(self):
        block = self.js.split("const WORKFLOWS = [", 1)[1].split("];", 1)[0]
        for repo in ("projectbluefin/utah", "projectbluefin/utah-packages"):
            self.assertIn(repo, block)
        # Nothing outside that list may name a workflow file.
        rest = self.js.replace(block, "")
        self.assertNotIn(".yml", rest)

    def test_a_failed_fetch_never_renders_as_passing(self):
        # "ok" is the only class that reads as green, and it must be reachable
        # only from a conclusion of success.
        self.assertIn('case "success":   return ["ok", "passing"];', self.js)
        self.assertIn('failure ? ["unknown", "unavailable"]', self.js)
        # A run still queued must not inherit the previous conclusion.
        status_check = self.js.index('run.status === "in_progress"')
        conclusion_check = self.js.index("switch (run.conclusion)")
        self.assertLess(status_check, conclusion_check)

    def test_content_is_visible_without_javascript(self):
        """.reveal starts hidden, so the hidden state must be opt-in. Gating it
        on a class an inline script adds means a page whose JS never runs shows
        everything rather than going blank."""
        self.assertIn('classList.add("js")', self.html)
        self.assertIn(".js .reveal { opacity: 0;", self.css)
        # The bare selector must not exist, or the gate is bypassed.
        self.assertNotRegex(self.css, r"(?<!\.js )\.reveal \{ opacity: 0;")

    def test_reduced_motion_leaves_content_visible(self):
        # .reveal starts at opacity 0; if the reduced-motion block only shortened
        # durations, every revealed section would stay invisible.
        block = self.css.split("prefers-reduced-motion: reduce", 1)[1]
        self.assertIn(".js .reveal { opacity: 1; transform: none; }", block)
        self.assertIn('matchMedia("(prefers-reduced-motion: reduce)")', self.js)

    def test_external_links_are_safe_and_the_page_is_reachable_by_keyboard(self):
        for match in re.finditer(r'<a\b[^>]*href="https?://[^"]+"[^>]*>', self.html):
            with self.subTest(tag=match.group(0)[:60]):
                self.assertIn('rel="noopener"', match.group(0))
        self.assertIn('class="skip"', self.html)
        self.assertIn(":focus-visible", self.css)

    def test_hummingbird_section_links_out(self):
        section = self.html.split('id="hummingbird"', 1)[1]
        for target in ("hummingbird-project.io", "gitlab.com/redhat/hummingbird",
                       "projectbluefin/utah-packages"):
            self.assertIn(target, section)

    def test_the_page_is_dependency_free(self):
        # No build step and no framework: the repository has no JS toolchain.
        scripts = re.findall(r'<script[^>]*src="([^"]+)"', self.html)
        self.assertEqual(scripts, ["app.js"])
        for host in ("cdn.", "unpkg", "jsdelivr", "tailwind"):
            self.assertNotIn(host, self.html)

    def test_brand_palette_matches_projectbluefin(self):
        for colour in ("#0c1016", "#10151f", "#272727", "#bdbdbd", "#4285f4", "#6c7ae9"):
            self.assertIn(colour, self.css)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / ".github/workflows/pages.yml").read_text()

    def test_deployment_verifies_the_data_before_publishing(self):
        self.assertIn("generate-site-data.py --check", self.workflow)
        self.assertLess(self.workflow.index("--check"),
                        self.workflow.index("upload-pages-artifact"))

    def test_actions_are_sha_pinned(self):
        for use in re.findall(r"uses: (\S+)", self.workflow):
            with self.subTest(uses=use):
                self.assertRegex(use, r"@[0-9a-f]{40}$")

    def test_deployment_is_not_cancelled_mid_upload(self):
        self.assertIn("cancel-in-progress: false", self.workflow)


class ScriptBehaviourTests(unittest.TestCase):
    """Execute the page's own functions instead of reading them.

    The first round of tests here asserted on the source text of app.js, which
    is how `ago()` shipped rendering every duration one unit too small -- five
    minutes as "5s ago", two days as "2h ago" -- with twenty-one tests green.
    A page whose whole claim is that it does not misreport build state cannot
    have its clock checked by grep.
    """

    NODE = shutil.which("node")

    def evaluate(self, function_name, script):
        """Extract one function from app.js and run `script` against it."""
        source = (SITE / "app.js").read_text()
        start = source.index(f"function {function_name}(")
        body = source[start:]
        body = body[: body.index("\n}\n") + 3]
        harness = f"{body}\n{script}"
        result = subprocess.run([self.NODE, "--input-type=module", "-e", harness],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    @unittest.skipUnless(NODE, "node is required to execute the page's functions")
    def test_relative_times_use_the_unit_they_were_converted_into(self):
        cases = {30: "30s ago", 300: "5m ago", 10800: "3h ago",
                 172800: "2d ago", 1814400: "3w ago"}
        script = """
        const now = Date.now();
        const at = (s) => new Date(now - s * 1000).toISOString();
        console.log(JSON.stringify(%s.map((s) => ago(at(s)))));
        """ % list(cases)
        self.assertEqual(json.loads(self.evaluate("ago", script)), list(cases.values()))

    @unittest.skipUnless(NODE, "node is required to execute the page's functions")
    def test_a_tracker_label_promotes_an_issue_the_title_does_not(self):
        # index.html promises "labelled or titled as a tracker"; a title-only
        # check quietly made half that sentence false.
        script = """
        const TRACKER = /^(tracking|roadmap|epic)\\b/i;
        console.log(JSON.stringify([
          isTracker({title: "Tracking: GNOME 51", labels: []}),
          isTracker({title: "Wi-Fi is broken", labels: [{name: "tracking"}]}),
          isTracker({title: "Wi-Fi is broken", labels: [{name: "bug"}]}),
          isTracker({title: "Wi-Fi is broken"}),
        ]));
        """
        self.assertEqual(json.loads(self.evaluate("isTracker", script)),
                         [True, True, False, False])


if __name__ == "__main__":
    unittest.main()
