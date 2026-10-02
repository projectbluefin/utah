"""Exercise Utah's custom recipes without network, uploads or MOK writes."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RECIPES = ROOT / "system_files/shared/usr/share/ublue-os/just/60-custom.just"
BASELINE = ROOT / "baselines/utah/rpms.tsv"


# casey/just 1.56 (2026-07-09) stopped deduplicating an AST across nested
# imports of the same file. On earlier releases, Common's deeper `60-custom.just`
# import shadows Utah's shallower one and every override silently reverts to
# Common's recipe (#449). Pin the test to the same floor the override relies on.
MINIMUM_JUST_VERSION = (1, 56, 0)


def _just_version() -> tuple[int, ...] | None:
    path = shutil.which("just")
    if not path:
        return None
    result = subprocess.run([path, "--version"], text=True, capture_output=True)
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split()
    if len(parts) < 2 or parts[0] != "just":
        return None
    try:
        components = tuple(int(piece) for piece in parts[1].split("."))
    except ValueError:
        return None
    # Pad to three components so the floor comparison is consistent with the
    # baseline check (which parses NEVR into three components); a two-digit
    # `just X.Y` string would otherwise compare (1, 56) < (1, 56, 0) as True
    # at the exact floor and spuriously skip the whole class.
    return components + (0,) * (3 - len(components))


_just = shutil.which("just")
_jq = shutil.which("jq")
_version = _just_version() if _just else None
if _version is not None and _version < MINIMUM_JUST_VERSION:
    _VERSION_SKIP_REASON = (
        f"ujust override precedence requires just >= {'.'.join(str(p) for p in MINIMUM_JUST_VERSION)};"
        f" host has {'.'.join(str(p) for p in _version)};"
        " see projectbluefin/utah#449"
    )
elif not _just:
    _VERSION_SKIP_REASON = "requires `just` on PATH"
elif not _jq:
    _VERSION_SKIP_REASON = "requires `jq` on PATH"
elif _version is None:
    _VERSION_SKIP_REASON = "could not parse `just --version`"
else:
    _VERSION_SKIP_REASON = ""

_VERSION_SKIP = unittest.skipUnless(not _VERSION_SKIP_REASON, _VERSION_SKIP_REASON)


def _baseline_just_evr() -> str | None:
    for line in BASELINE.read_text().splitlines():
        name, _, evr = line.partition("\t")
        if name == "just":
            return evr.strip()
    return None


class JustFloorBaselineTests(unittest.TestCase):
    """The shipped image, not just the developer's host, must clear the floor."""

    def test_baseline_just_is_at_or_above_the_floor(self):
        evr = _baseline_just_evr()
        self.assertIsNotNone(evr, f"no `just` row in {BASELINE}")
        version = evr.split("-", 1)[0]
        try:
            parsed = tuple(int(piece) for piece in version.split("."))
        except ValueError:
            self.fail(f"cannot parse `just` version from baseline EVR {evr!r}")
        parsed += (0,) * (len(MINIMUM_JUST_VERSION) - len(parsed))
        floor = ".".join(str(piece) for piece in MINIMUM_JUST_VERSION)
        self.assertGreaterEqual(
            parsed,
            MINIMUM_JUST_VERSION,
            f"image ships just {evr}, below the {floor} floor the ujust overrides"
            " rely on; see projectbluefin/utah#449",
        )


@_VERSION_SKIP
class UjustOverridesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("cat", "mktemp", "rm", "jq", "grep", "sh"):
            (self.bin / name).symlink_to(shutil.which(name))
        self.just = shutil.which("just")
        self.log = self.root / "calls"
        self.env = dict(os.environ, PATH=str(self.bin), TMPDIR=str(self.root),
                        CALLS=str(self.log))
        info = self.root / "image-info.json"
        info.write_text(json.dumps({"image-tag": "testing", "image-name": "utah"}))
        self.env["IMAGE_INFO_FILE"] = str(info)
        self.mock("repo-helper", 'echo "repo $*" >> "$CALLS"; echo projectbluefin/utah')
        self.env["UBLUE_IMAGE_REPO_BIN"] = str(self.bin / "repo-helper")
        for name in ("bootc", "flatpak", "uname", "lscpu", "lsblk", "free"):
            self.mock(name, f'echo "{name} diagnostic"')
        for name in ("sudo", "mokutil"):
            self.mock(name, f'echo "{name}" >> "$CALLS"; exit 99')
        self.mock("curl", 'echo "curl $*" >> "$CALLS"; echo \'{"body":"# Release notes"}\'')
        # Common imports defaults before custom recipes at the same depth.
        # Utah wraps that entry point to make its override import shallower.
        original = self.root / "default.just"
        original.write_text("device-info:\n    exit 99\nchangelogs:\n    exit 99\n"
                            "enroll-secure-boot-key:\n    exit 99\n"
                            "report:\n    exit 99\n")
        common = self.root / "00-common.just"
        common.write_text('set allow-duplicate-recipes\n_default:\n    @echo common-default\n'
                          'unrelated:\n    @echo common-unrelated\n'
                          f'import "{original}"\n'
                          f'import "{RECIPES}"\n')
        self.entry = self.root / "entry.just"
        entry = (RECIPES.parent / "00-entry.just").read_text()
        self.entry.write_text(entry.replace("/usr/share/ublue-os/just/00-common.just", str(common))
                              .replace("/usr/share/ublue-os/just/60-custom.just", str(RECIPES)))

    def mock(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/bash\n" + body + "\n")
        path.chmod(0o755)

    def run_recipe(self, name):
        return subprocess.run([self.just, "--justfile", str(self.entry), name],
                              env=self.env, text=True, capture_output=True)

    def calls(self):
        return self.log.read_text() if self.log.exists() else ""

    def test_common_default_and_unrelated_recipes_survive(self):
        result = subprocess.run([self.just, "--justfile", str(self.entry)],
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "common-default\n")
        result = self.run_recipe("unrelated")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "common-unrelated\n")

    def test_device_info_without_fpaste_is_local_and_cleans_report(self):
        result = self.run_recipe("device-info")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("bootc", "flatpak", "uname", "lscpu", "lsblk", "free"):
            self.assertIn(f"{name} diagnostic", result.stdout)
        self.assertIn("fpaste is unavailable", result.stderr)
        self.assertEqual(self.calls(), "")
        self.assertEqual(list(self.root.glob("utah-device-info.*")), [])

    def setup_fpaste(self, consent):
        self.mock("gum", f'echo confirm >> "$CALLS"; exit {0 if consent else 1}')
        self.mock("fpaste", 'if [[ "$*" == "--sysinfo --printonly" ]]; then\n'
                  'echo sysinfo\nelse\necho upload >> "$CALLS"; cat\nfi')

    def test_device_upload_requires_confirmation(self):
        self.setup_fpaste(False)
        result = self.run_recipe("device-info")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sysinfo", result.stdout)
        self.assertEqual(self.calls(), "confirm\n")

    def test_device_upload_after_confirmation(self):
        self.setup_fpaste(True)
        result = self.run_recipe("device-info")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), "confirm\nupload\n")
        self.assertEqual(list(self.root.glob("utah-device-info.*")), [])

    def test_device_failure_cleans_report_and_does_not_upload(self):
        self.setup_fpaste(True)
        self.mock("bootc", "exit 7")
        result = self.run_recipe("device-info")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), "")
        self.assertEqual(list(self.root.glob("utah-device-info.*")), [])

    def test_changelogs_without_glow_prints_release(self):
        result = self.run_recipe("changelogs")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "# Release notes\n")
        self.assertIn("repo --default projectbluefin/utah utah testing", self.calls())
        self.assertIn("https://api.github.com/repos/projectbluefin/utah/releases/latest", self.calls())

    def test_changelogs_with_glow(self):
        self.mock("glow", 'echo "glow $*" >> "$CALLS"; cat')
        result = self.run_recipe("changelogs")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "# Release notes\n")
        self.assertIn("glow -p", self.calls())

    def test_changelogs_preserves_specific_release_selection(self):
        Path(self.env["IMAGE_INFO_FILE"]).write_text(json.dumps(
            {"image-tag": "stable", "image-name": "utah"}))
        (self.bin / "grep").unlink()
        self.mock("grep", "echo 20261001")
        self.mock("curl", 'echo "curl $*" >> "$CALLS"; '
                  'echo \'[{"tag_name":"stable-20261001","body":"Specific release"}]\'')
        result = self.run_recipe("changelogs")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Specific release\n")
        self.assertNotIn("releases/latest", self.calls())

    def test_changelogs_propagates_http_failure(self):
        self.mock("curl", "exit 22")
        result = self.run_recipe("changelogs")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_changelogs_rejects_missing_release_body(self):
        self.mock("curl", "echo '{}'")
        result = self.run_recipe("changelogs")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_enrollment_explains_missing_support_without_privileged_calls(self):
        result = self.run_recipe("enroll-secure-boot-key")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not yet ship", result.stderr)
        self.assertIn("https://github.com/projectbluefin/utah/issues/395", result.stderr)
        self.assertEqual(self.calls(), "")

    def test_report_override_keeps_a_user_facing_list_description(self):
        # just uses only the comment line immediately preceding a recipe as
        # its description, so an implementation comment block ending right
        # above `[group('System')]` would replace Common's user-facing text
        # in `ujust --list` with a fragment like "...authoritative grammar.".
        result = subprocess.run([self.just, "--justfile", str(self.entry), "--list"],
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        line = next((l for l in result.stdout.splitlines()
                     if l.strip().startswith("report ")), None)
        self.assertIsNotNone(line, f"report missing from --list; got {result.stdout!r}")
        self.assertIn("# Collect a previewed, privacy-respecting report", line)
        for fragment in ("authoritative grammar", "ublue-image-repo", "#446",
                         "BONEDIGGER"):
            self.assertNotIn(fragment, line,
                             "ujust --list must not surface implementation comments")

    def test_report_override_runs_bonedigger_with_utah_image_repo(self):
        # projectbluefin/utah#446: ujust report on Utah was falling through
        # common's routing grammar and landing in projectbluefin/common.
        # Utah overrides `report` in 60-custom.just so bonedigger-report
        # routes through the local utah-image-repo shim. Run the recipe
        # end-to-end through the entry point with the absolute path the
        # recipe calls replaced by a tmp-dir stub; the stub records the
        # UBLUE_IMAGE_REPO_BIN env that the recipe set, which proves the
        # shim path is what bonedigger sees at runtime, not the default
        # ublue-image-repo from common. The entry point resolves `report`
        # to Utah's override because the shallower import wins duplicate
        # handling on just >= 1.56; the staged default.just defines a
        # competing `report: exit 99` so an override that lost duplicate
        # resolution fails loudly instead of passing trivially.
        bonedigger_stub = self.root / "usr-libexec-bonedigger-report"
        bonedigger_stub.write_text(
            "#!/usr/bin/bash\n"
            'echo "bonedigger $*" >> "$CALLS"\n'
            'echo "${UBLUE_IMAGE_REPO_BIN:-unset}" >> "$CALLS"\n'
        )
        bonedigger_stub.chmod(0o755)
        # Rewrite the entry's resolved recipe text so the absolute
        # `/usr/libexec/bonedigger-report` calls the stub instead. The
        # rewrite lives in a tmp copy that the entry justfile imports; the
        # original recipe source on disk is untouched.
        original_recipe = RECIPES.read_text()
        patched_recipes = self.root / "60-custom.just"
        patched_recipes.write_text(original_recipe.replace(
            "/usr/libexec/bonedigger-report", str(bonedigger_stub)
        ))
        # Replace the entry's import of the live recipe with the patched copy
        # so `just` resolves the stubbed path.
        entry_text = self.entry.read_text()
        self.entry.write_text(entry_text.replace(str(RECIPES), str(patched_recipes)))

        # setUp pre-sets UBLUE_IMAGE_REPO_BIN for the changelogs tests; drop it
        # here so the stub's `${UBLUE_IMAGE_REPO_BIN:-unset}` really would print
        # `unset` if the recipe itself failed to export the shim path.
        env = dict(self.env)
        env.pop("UBLUE_IMAGE_REPO_BIN", None)
        result = subprocess.run([self.just, "--justfile", str(self.entry), "report"],
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        # The stub is what the recipe actually executed, so its presence in
        # the calls log proves the recipe reached bonedigger-report (not the
        # default ublue-image-repo path). The stub wrote the UBLUE_IMAGE_REPO_BIN
        # env it received, so the second line proves the shim path was set
        # for bonedigger-report at runtime, not the default from common.
        self.assertTrue(calls.startswith("bonedigger \n"),
                        f"bonedigger stub did not run; calls={calls!r}")
        self.assertIn("/usr/libexec/utah-image-repo", calls,
                      "ujust report must set UBLUE_IMAGE_REPO_BIN to the Utah shim")
        self.assertNotIn("unset", calls,
                          "UBLUE_IMAGE_REPO_BIN must be set by the recipe, "
                          "not left to fall back to common's ublue-image-repo")
        # Static guard: the shipped recipe text must also reference the shim
        # path directly, so a future contributor who removes the export
        # breaks the test before the merge claim.
        match = re.search(
            r"report \*args:\s*\n"
            r"(?P<body>(?:[ \t].*\n|\s*\\\s*\n)+)",
            original_recipe,
        )
        self.assertIsNotNone(
            match,
            "ujust report recipe must exist with a multi-line body",
        )
        body = match.group("body")
        self.assertIn(
            'UBLUE_IMAGE_REPO_BIN="/usr/libexec/utah-image-repo"',
            body,
            "ujust report must set UBLUE_IMAGE_REPO_BIN to the Utah shim",
        )
        self.assertIn(
            "/usr/libexec/bonedigger-report",
            body,
            "ujust report must still call bonedigger-report",
        )

if __name__ == "__main__":
    unittest.main()
