"""utah-image-repo routes utah* to projectbluefin/utah.

The single source of truth for image-name -> upstream repo routing lives in
common's `/usr/libexec/ublue-image-repo`. Utah's `ujust report` falls through
that grammar's `*` arm because it does not list `utah*`, and the
hard-coded `--default projectbluefin/common` then wins (projectbluefin/utah#446).

`scripts/image-repo.sh` is the Utah-local shim that fixes the reverse direction:
short-circuit any `utah*` name to `projectbluefin/utah`, then forward every
other call (and any args) to common's authoritative resolver so its grammar
remains the single source of truth for non-Utah image names.

These tests cover the shim's two paths: the utah* short-circuit, and the
fall-through that forwards --default and the positional args unchanged.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "scripts/image-repo.sh"

# The shim is only meaningful once Utah has branched off common; if a future
# Utah layout moves the file, the test should follow it, not pass trivially.
assert SHIM.exists(), f"{SHIM} not found"


class UtahImageRepoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # The shim execs /usr/libexec/ublue-image-repo unconditionally, so the
        # test stages a fake upstream that records its args and echoes a
        # caller-controlled repo. The fake is deliberately minimal: this test
        # is about what the shim forwards, not about common's grammar.
        self.upstream = self.root / "upstream-resolver"
        self.upstream.write_text(
            "#!/usr/bin/bash\n"
            "echo \"upstream $@\" >> \"$CALLS\"\n"
            "echo \"argc $#\" >> \"$CALLS\"\n"
            "# Echo the --default value back so the test can verify it was\n"
            "# preserved; if --default was missing, fall back to a fixed string.\n"
            "default=\"projectbluefin/upstream-default\"\n"
            "while (($# > 0)); do\n"
            "    case \"$1\" in\n"
            "        --default) default=\"$2\"; shift 2 ;;\n"
            "        --default=*) default=\"${1#--default=}\"; shift ;;\n"
            "        --) shift ; break ;;\n"
            "        *) shift ;;\n"
            "    esac\n"
            "done\n"
            "printf '%s\\n' \"$default\"\n"
        )
        self.upstream.chmod(0o755)
        self.calls = self.root / "upstream-calls"
        self.env = dict(os.environ, CALLS=str(self.calls))

    def shim_text(self) -> str:
        return SHIM.read_text().replace(
            "/usr/libexec/ublue-image-repo", str(self.upstream)
        )

    def upstream_calls_text(self) -> str:
        return self.calls.read_text() if self.calls.exists() else ""

    def run_shim(self, *args):
        # Spawn bash with the in-memory shim so the swap back to the real
        # path does not leak into the on-disk script.
        return subprocess.run(
            ["bash", "-c", self.shim_text(), "_", *args],
            capture_output=True, text=True, env=self.env,
        )

    def test_routes_utah_to_projectbluefin_utah(self):
        result = self.run_shim("--default", "projectbluefin/common", "utah", "testing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "projectbluefin/utah")
        # The utah* short-circuit must not call the upstream at all: the
        # upstream's resolver grammar does not know about utah, so forwarding
        # would still hit its hard-coded fallback.
        self.assertEqual(self.upstream_calls_text(), "")

    def test_routes_utah_flavored_names_to_projectbluefin_utah(self):
        for image in ["utah-gaming", "utah-nvidia", "utah-gaming-testing"]:
            with self.subTest(image=image):
                if self.calls.exists():
                    self.calls.unlink()
                result = self.run_shim("--default", "projectbluefin/common",
                                       image, "testing")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "projectbluefin/utah")
                self.assertEqual(self.upstream_calls_text(), "",
                                 f"upstream was called for {image}")

    def test_forwards_non_utah_names_to_upstream_resolver(self):
        # bluefin is matched by upstream's grammar in production; here the
        # shim's job is only to forward the call unchanged, not to make a
        # routing decision. The fake upstream echoes --default.
        result = self.run_shim("--default", "projectbluefin/common",
                               "bluefin", "testing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "projectbluefin/common")
        self.assertIn("upstream --default projectbluefin/common bluefin testing",
                      self.calls.read_text())

    def test_forwards_dakota_to_upstream_resolver(self):
        result = self.run_shim("--default", "projectbluefin/common",
                               "dakota", "testing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "projectbluefin/common")
        self.assertIn("upstream --default projectbluefin/common dakota testing",
                      self.calls.read_text())

    def test_preserves_caller_supplied_default_on_fall_through(self):
        # The caller is busy with bonedigger-report, which hard-codes
        # --default projectbluefin/common. The shim must keep that default
        # intact when forwarding, not silently substitute its own.
        result = self.run_shim("--default", "projectbluefin/knuckle",
                               "unknown-image", "lts")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "projectbluefin/knuckle")

    def test_forwards_unknown_name_without_default_to_upstream_default(self):
        # Without --default, the upstream's own grammar supplies its
        # hard-coded default. The shim must not invent one of its own.
        result = self.run_shim("unknown-image", "testing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(),
                         "projectbluefin/upstream-default")

    def test_omits_absent_positionals_instead_of_forwarding_empty_strings(self):
        # common's resolver reads `${1-${IMAGE_NAME-}}`, so forwarding an
        # empty "$1" would suppress its IMAGE_NAME/IMAGE_TAG env fallback.
        result = self.run_shim()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("argc 0", self.upstream_calls_text())

        self.calls.unlink()
        result = self.run_shim("--default", "projectbluefin/common")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("upstream --default projectbluefin/common\n",
                      self.upstream_calls_text())
        self.assertIn("argc 2", self.upstream_calls_text())

        self.calls.unlink()
        result = self.run_shim("bluefin")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("upstream bluefin\n", self.upstream_calls_text())
        self.assertIn("argc 1", self.upstream_calls_text())

    def test_empty_image_name_keeps_its_positional_slot(self):
        # bonedigger-report always passes two positionals ("$IMAGE_NAME"
        # "$IMAGE_TAG"), and IMAGE_NAME can be empty on a broken
        # /usr/lib/os-release. The shim must not slide the tag into the name
        # slot: common's grammar still routes an empty name with an lts* tag
        # to bluefin-lts, and that decision belongs to common.
        result = self.run_shim("--default", "projectbluefin/common",
                               "", "lts-20260101")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("argc 4", self.upstream_calls_text())
        self.assertIn("upstream --default projectbluefin/common  lts-20260101\n",
                      self.upstream_calls_text())

    def test_double_dash_positionals_are_forwarded(self):
        # common ends option parsing on `--` and reads the rest as
        # IMAGE_NAME/IMAGE_TAG. The shim claims the same grammar, so the
        # trailing args must reach the upstream rather than being dropped.
        result = self.run_shim("--default", "projectbluefin/common",
                               "--", "dakota", "testing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("upstream --default projectbluefin/common dakota testing",
                      self.upstream_calls_text())

    def test_double_dash_utah_name_still_short_circuits(self):
        result = self.run_shim("--", "utah", "testing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "projectbluefin/utah")
        self.assertEqual(self.upstream_calls_text(), "")

    def test_options_after_the_first_positional_are_not_parsed(self):
        # common breaks out of its option loop on the first non-option, so a
        # late --default is just another positional. The shim must forward it
        # verbatim instead of consuming it as its own option.
        result = self.run_shim("bluefin", "--default", "projectbluefin/knuckle")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("upstream bluefin --default projectbluefin/knuckle\n",
                      self.upstream_calls_text())

    def test_missing_default_argument_value_is_rejected(self):
        # `--default` without a value is a usage error in both the shim and
        # upstream. The shim surfaces its own message; the upstream case is
        # not exercised here.
        result = self.run_shim("--default")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()
