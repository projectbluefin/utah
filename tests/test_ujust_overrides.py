"""Exercise Utah's custom recipes without network, uploads or MOK writes."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
JUST_DIR = ROOT / "system_files/shared/usr/share/ublue-os/just"
RECIPES = JUST_DIR / "60-custom.just"
# Dakota's recipe files Utah ships in place of Common's (#1231).
DAKOTA_RECIPES = [JUST_DIR / name for name in ("default.just", "system.just", "flutter.just")]
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
        # default.just evaluates `id -u` and `cut` at load time; Dakota's
        # recipes start with `#!/usr/bin/env bash`.
        for name in ("cat", "mktemp", "rm", "jq", "grep", "sh", "id", "cut", "bash"):
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
        # Common's entry imports its recipe files, then 60-custom.just, at the
        # same depth. Utah ships Dakota's default.just, system.just and
        # flutter.just over Common's; changelog.just stays Common's, mocked
        # here with the recipe Utah overrides.
        common_recipes = self.root / "changelog.just"
        common_recipes.write_text("changelogs:\n    exit 99\n")
        imports = "".join(f'import "{path}"\n'
                          for path in (common_recipes, *DAKOTA_RECIPES, RECIPES))
        common = self.root / "00-common.just"
        common.write_text('set allow-duplicate-recipes\nset ignore-comments\n'
                          '_default:\n    @echo common-default\n'
                          'unrelated:\n    @echo common-unrelated\n' + imports)
        self.entry = self.root / "entry.just"
        entry = (JUST_DIR / "00-entry.just").read_text()
        self.entry.write_text(entry.replace("/usr/share/ublue-os/just/00-common.just", str(common))
                              .replace("/usr/share/ublue-os/just/60-custom.just", str(RECIPES)))

    def mock(self, name, body):
        path = self.bin / name
        path.unlink(missing_ok=True)
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

    def test_secure_boot_recipes_point_at_1225_without_privileged_calls(self):
        for recipe in ("enroll-secure-boot-key", "secure-boot-status",
                       "check-anticheat", "reseal-tpm-unlock"):
            with self.subTest(recipe=recipe):
                result = self.run_recipe(recipe)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("projectbluefin/utah", result.stderr)
                self.assertIn("1225", result.stderr)
                self.assertEqual(self.calls(), "")

    def test_network_monitoring_explains_missing_nethogs_mount(self):
        result = self.run_recipe("toggle-network-monitoring")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not ship nethogs", result.stderr)
        self.assertEqual(self.calls(), "")

    def test_toggle_user_motd_drives_uwelcome(self):
        # Common's uwelcome.sh keeps the banner state in
        # ~/.config/uwelcome/disabled; Dakota's recipe toggled a flag nothing
        # reads, so it could hide the banner but never bring it back.
        self.mock("uwelcome", f'echo "uwelcome $*" >>"{self.log}"')
        result = self.run_recipe("toggle-user-motd")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), "uwelcome toggle\n")

    def test_check_local_overrides_ships(self):
        # The testsuite runs `ujust check-local-overrides` on Utah.
        result = subprocess.run([self.just, "--justfile", str(self.entry), "--show",
                                 "check-local-overrides"],
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/usr/etc /etc", result.stdout)

    def test_dakota_recipes_send_issues_to_utah(self):
        self.mock("gum", 'echo "gum $*" >> "$CALLS"; echo "Bug report"')
        self.mock("xdg-open", 'echo "xdg-open $*" >> "$CALLS"')
        result = self.run_recipe("file-issue")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("xdg-open https://github.com/projectbluefin/utah/issues/new", self.calls())
        text = "".join(path.read_text() for path in DAKOTA_RECIPES)
        self.assertNotIn("projectbluefin/dakota/issues", text)
        self.assertEqual(text.count('REPO="projectbluefin/utah"'), 2,
                         "confirm and verify must report to projectbluefin/utah")

    def test_rebase_helper_offers_utah_images(self):
        text = (JUST_DIR / "system.just").read_text()
        body = text[text.index("\nrebase-helper:"):text.index("\nenroll-secure-boot-key:")]
        self.assertIn('REGISTRY="ghcr.io/projectbluefin"', body)
        self.assertIn("utah(-nvidia)?(-gaming)?", body)
        self.assertNotIn("dakota", body.lower())

    def setup_debug_tools(self, uid=0, bootc_exit=0):
        self.mock("id", f'echo {uid}')
        self.mock("sudo", 'echo "sudo" >> "$CALLS"')
        self.mock("bootc", f'echo "bootc $*" >> "$CALLS"; echo overlay-error >&2; '
                  f'exit {bootc_exit}')
        self.mock("dnf5", 'echo "dnf5 $*" >> "$CALLS"')

    def test_install_debug_tools_unlocks_usr_then_installs(self):
        self.setup_debug_tools()
        result = self.run_recipe("install-debug-tools")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), "bootc usr-overlay\n"
                         "dnf5 install -y gdb-headless elfutils sysprof-cli\n")
        self.assertIn('pgrep -u "$USER" -x gnome-shell', result.stdout)
        self.assertNotIn("pidof", result.stdout)

    def test_install_debug_tools_reexecs_under_sudo_when_not_root(self):
        self.setup_debug_tools(uid=1000)
        result = self.run_recipe("install-debug-tools")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), "sudo\n")

    @unittest.skipIf(os.access("/usr", os.W_OK), "/usr is writable on this host")
    def test_install_debug_tools_stops_when_usr_stays_read_only(self):
        self.setup_debug_tools(bootc_exit=1)
        result = self.run_recipe("install-debug-tools")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), "bootc usr-overlay\n")
        self.assertIn("overlay-error", result.stderr)
        self.assertIn("could not unlock /usr", result.stderr)

    def test_install_debug_tools_has_a_user_facing_list_description(self):
        result = subprocess.run([self.just, "--justfile", str(self.entry), "--list"],
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        line = next((l for l in result.stdout.splitlines()
                     if l.strip().startswith("install-debug-tools ")), None)
        self.assertIsNotNone(line, f"install-debug-tools missing from --list; got {result.stdout!r}")
        self.assertIn("# Install gdb, elfutils and sysprof-cli until reboot", line)

    def test_virtualization_recipes_run_dakota_devmode_when_libvirt_is_installed(self):
        unit = self.root / "virtqemud.socket"
        unit.write_text("")
        self.env["LIBVIRT_SOCKET_UNIT"] = str(unit)
        self.mock("devmode", 'echo "devmode $*" >> "$CALLS"')
        self.env["DAKOTA_DEVMODE"] = str(self.bin / "devmode")
        for recipe, action in (("setup-virtualization", "setup"),
                               ("manage-virtualization", "virtualization")):
            with self.subTest(recipe=recipe):
                self.log.unlink(missing_ok=True)
                result = self.run_recipe(recipe)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.calls(), f"devmode {action}\n")

    def test_virtualization_recipes_explain_missing_libvirt(self):
        self.env["LIBVIRT_SOCKET_UNIT"] = str(self.root / "absent.socket")
        self.mock("devmode", 'echo "devmode $*" >> "$CALLS"')
        self.env["DAKOTA_DEVMODE"] = str(self.bin / "devmode")
        result = self.run_recipe("setup-virtualization")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("libvirt is not in this image yet", result.stderr)
        self.assertEqual(self.calls(), "")

if __name__ == "__main__":
    unittest.main()
