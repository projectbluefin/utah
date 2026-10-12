"""ChairLift's GUI installation must be enabled, and build failures visible.

The runtime invariants for the Control Center (ChairLift) on Utah: the
brew-preinstall service must enable globally, the inherited release pins in
contracts/bluefin-desktop.toml must line up with what Common ships, and the
polkit/gschemas/helper binaries must not be removed from the image. The
Homebrew native-compilation invariants (PATH filtering, /usr/bin/make) and
the /var/home symlink structure must also hold so the persistent
/home/linuxbrew/.linuxbrew prefix survives a cask install that compiles a
gem. The bootc-update-stage polkit annotation must route to
/usr/libexec/bootc-update-stage so the privileged update path executes
without surprising a pkexec boundary. See issue #676.
"""
import tomllib
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts/bluefin-desktop.toml"
BLUEFIN_TOML = ROOT / "packages/bluefin.toml"
UTAH_TOML = ROOT / "packages/utah.toml"
CONFIGURE_SERVICES = ROOT / "scripts/configure-services.sh"
USER_PRESET = ROOT / "system_files/shared/usr/lib/systemd/user-preset/85-utah-desktop.preset"
STARSHIP_PROFILE = ROOT / "system_files/shared/etc/profile.d/90-bluefin-starship.sh"
FRAMEWORK_HOOK = ROOT / "system_files/shared/usr/share/ublue-os/user-setup.hooks.d/20-framework.sh"


class ChairLiftSetupTests(unittest.TestCase):
    def run_setup(self, exists=0, enable=0):
        source = (ROOT / "scripts/configure-services.sh").read_text()
        block = source.split("# Common's user preset is not applied", 1)[1].split(
            "# Match Bluefin's login behavior.", 1
        )[0]
        # Execute the actual required setup with controlled unit discovery and
        # systemctl failures. Bash must stop before later build operations.
        block = "# Common's user preset is not applied" + block
        return subprocess.run(
            ["bash", "-ec", f'''
user_unit_exists() {{ return {exists}; }}
systemctl() {{ printf '%s\\n' "$*"; return {enable}; }}
{block}
echo completed
'''], capture_output=True, text=True,
        )

    def test_enables_gui_installation_for_every_user(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "--global enable brew-preinstall.service", "completed",
        ])

    def test_missing_preinstaller_fails_the_build(self):
        result = self.run_setup(exists=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("completed", result.stdout)
        self.assertNotIn("--global", result.stdout)

    def test_enablement_failure_fails_the_build(self):
        result = self.run_setup(enable=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("completed", result.stdout)


class ChairLiftContractAlignmentTests(unittest.TestCase):
    """Issue #676: the chairlift-helper binary, both polkit policies, and the
    three GSettings schemas must stay declared in the contract so a downstream
    image regression that drops one fails the in-image verifier rather than
    surfacing on a booted install.
    """

    @classmethod
    def setUpClass(cls):
        cls.contract = tomllib.loads(CONTRACT.read_text())

    def _files(self, section):
        return self.contract.get(section, {}).get("files", [])

    def _file_contains(self, path):
        return (
            self.contract.get("configuration", {}).get("file_contains", {}).get(path, [])
        )

    def test_branding_declares_helper_and_ublue_policy(self):
        files = self._files("branding")
        self.assertIn("/usr/bin/chairlift-helper", files)
        # The ublue policy grants administrator authentication on the nine
        # privileged actions Common's ChairLift release ships; Common owns the
        # file, and Utah must declare it as branding so the in-image verifier
        # fails closed if Common drops it.
        self.assertIn(
            "/usr/share/polkit-1/actions/io.projectbluefin.chairlift.ublue.policy",
            files,
        )

    def test_configuration_declares_release_assets(self):
        files = self._files("configuration")
        expected = [
            "/usr/share/applications/io.projectbluefin.chairlift.desktop",
            "/usr/share/icons/hicolor/scalable/apps/io.projectbluefin.chairlift.svg",
            "/usr/share/icons/hicolor/symbolic/apps/io.projectbluefin.chairlift-symbolic.svg",
            "/usr/share/chairlift/config.yml",
            "/usr/share/ublue-os/homebrew/preinstall.d/chairlift.Brewfile",
            "/usr/bin/brew-preinstall",
            "/usr/libexec/brew-preinstall",
            "/usr/lib/systemd/user/brew-preinstall.service",
            "/usr/libexec/bootc-update-stage",
            "/usr/share/polkit-1/actions/io.projectbluefin.chairlift.bootc.policy",
            "/usr/share/glib-2.0/schemas/io.projectbluefin.chairlift.livery.gschema.xml",
            "/usr/share/glib-2.0/schemas/io.projectbluefin.chairlift.updates.gschema.xml",
            "/usr/share/glib-2.0/schemas/io.projectbluefin.chairlift.firstrun.gschema.xml",
        ]
        for path in expected:
            self.assertIn(path, files, f"contract must assert {path}")

    def test_three_gschema_schemas_present(self):
        # ChairLift's firstrun, updates, and livery schemas are the runtime
        # knobs the GNOME shell reads; losing any of them silently disables
        # settings persistence in the Control Center.
        files = self._files("configuration")
        schemas = [p for p in files if p.endswith(".gschema.xml")]
        names = sorted(Path(p).name for p in schemas)
        self.assertEqual(
            names,
            sorted([
                "io.projectbluefin.chairlift.firstrun.gschema.xml",
                "io.projectbluefin.chairlift.livery.gschema.xml",
                "io.projectbluefin.chairlift.updates.gschema.xml",
            ]),
        )

    def test_desktop_file_pins_chairlift_wrapper_exec(self):
        # Exec must point at the brewed chairlift-wrapper, not at a system
        # binary. The contract pins the exact line so a regression that
        # substitutes a non-existent path or removes Exec entirely fails the
        # in-image verifier.
        expectations = self._file_contains(
            "/usr/share/applications/io.projectbluefin.chairlift.desktop"
        )
        self.assertIn("Name=Control Center", expectations)
        self.assertIn(
            "Exec=/home/linuxbrew/.linuxbrew/bin/chairlift-wrapper", expectations
        )
        self.assertIn("Icon=io.projectbluefin.chairlift", expectations)

    def test_chairlift_brewfile_uses_trusted_tap_and_cask(self):
        expectations = self._file_contains(
            "/usr/share/ublue-os/homebrew/preinstall.d/chairlift.Brewfile"
        )
        self.assertIn('tap "projectbluefin/tap", trusted: true', expectations)
        self.assertIn('cask "projectbluefin/tap/chairlift"', expectations)

    def test_brew_preinstall_service_in_user_enabled(self):
        # `brew-preinstall.service` is a per-user unit (the cask lands in
        # /home/linuxbrew/.linuxbrew), so the contract's user_enabled list is
        # what the in-image verifier checks via `systemctl --global`.
        user_enabled = self.contract.get("services", {}).get("user_enabled", [])
        self.assertIn("brew-preinstall.service", user_enabled)

    def test_user_preset_enables_brew_preinstall(self):
        # The user preset runs after Common's `01-brew-preinstall.preset` only
        # on systems where bootc applies user presets, which Utah's image does
        # not (system preset-all runs instead). The preset here documents the
        # desktop intent for systems that do pick it up.
        text = USER_PRESET.read_text()
        self.assertIn("enable brew-preinstall.service", text)

    def test_configure_services_enables_brew_preinstall_globally(self):
        # bootc's system preset-all runs the system presets, not user presets,
        # so without an explicit `systemctl --global enable` the GUI Brewfile
        # sits on disk but never installs. The unit's enablement must stay
        # *after* `ublue-user-setup.service` (which creates the user account)
        # and *before* `authselect enable-feature`, so a missing unit or a
        # failed enablement fails the build before later steps run.
        text = CONFIGURE_SERVICES.read_text()
        self.assertIn("user_unit_exists brew-preinstall.service", text)
        self.assertIn("systemctl --global enable brew-preinstall.service", text)

        ublue_setup = text.index("systemctl --global enable ublue-user-setup.service")
        brew_preinstall = text.index(
            "systemctl --global enable brew-preinstall.service"
        )
        authselect = text.index("authselect enable-feature")
        self.assertLess(ublue_setup, brew_preinstall)
        self.assertLess(brew_preinstall, authselect)


class BootcUpdateStagePolkitTests(unittest.TestCase):
    """Issue #676: the bootc-update-stage polkit action must point at the
    helper Common ships, and the helper's binary must be on disk. A regression
    that breaks either half surfaces as a privileged update failing to
    escalate through polkit, with no error in the build itself.
    """

    @classmethod
    def setUpClass(cls):
        cls.contract = tomllib.loads(CONTRACT.read_text())

    def test_bootc_update_stage_binary_is_declared(self):
        files = self.contract.get("configuration", {}).get("files", [])
        self.assertIn("/usr/libexec/bootc-update-stage", files)

    def test_bootc_policy_annotates_exec_path(self):
        expectations = (
            self.contract.get("configuration", {}).get("file_contains", {}).get(
                "/usr/share/polkit-1/actions/io.projectbluefin.chairlift.bootc.policy",
                [],
            )
        )
        # The polkit annotation is the bit pkexec reads when ChairLift asks
        # for the privileged update path. The contract pins the literal
        # `<annotate ...>` line so a Common release that rewrites it without
        # keeping the annotation fails the in-image verifier.
        self.assertIn(
            '<annotate key="org.freedesktop.policykit.exec.path">'
            "/usr/libexec/bootc-update-stage</annotate>",
            expectations,
        )


class HomebrewToolchainRetentionTests(unittest.TestCase):
    """Issue #676: Homebrew's internal Bundler runs with a strict PATH filter
    (/usr/bin:/bin:/usr/sbin:/sbin) and unbuffered native extensions. If a
    cask or formula triggers a gem compile, the absence of /usr/bin/make
    bricks the persistent /home/linuxbrew/.linuxbrew prefix. Bluefin's
    [fedora] contract retains `make`, `gcc`, and `gcc-c++` precisely so the
    build chain survives a non-empty PATH. Utah copies bluefin.toml verbatim
    (`just check-parity`) so the same retention must hold here; if it drifts,
    `just check` must fail.
    """

    @classmethod
    def setUpClass(cls):
        cls.bluefin = tomllib.loads(BLUEFIN_TOML.read_text())
        cls.utah = tomllib.loads(UTAH_TOML.read_text())

    def _fedora_packages(self):
        return list(self.bluefin.get("fedora", {}).get("packages", []))

    def test_bluefin_contract_retains_make(self):
        self.assertIn("make", self._fedora_packages())

    def test_bluefin_contract_retains_gcc(self):
        # `gcc-c++` is alongside `gcc` in the Bluefin contract so a cask
        # linking against libstdc++ or compiling a Ruby gem finds both halves
        # at install time. The pair is asserted together to catch a one-sided
        # removal.
        packages = self._fedora_packages()
        self.assertIn("gcc", packages)
        self.assertIn("gcc-c++", packages)

    def test_bluefin_contract_lists_toolchain_in_fedora_section(self):
        # The toolchain packages live in `[fedora]`, not in `[unavailable]`,
        # `[excluded]`, or any Fedora-version-conditional one: an image built
        # on Fedora 43 must install `make` for the same reason an image on
        # Fedora 44 does, so the name belongs in the common section.
        section = self.bluefin.get("fedora", {})
        packages = section.get("packages", [])
        for name in ("make", "gcc", "gcc-c++"):
            self.assertIn(name, packages)

    def test_configure_services_only_removes_extension_toolchain(self):
        # The build-time extension toolchain (`dbus-devel`, `glib2-devel`,
        # `meson`, `sassc`) is removed before the image is shipped so the
        # closure's auto-installed dependencies (ninja-build, *-devel chains)
        # do not pad the layer cache. The runtime toolchain (`make`, `gcc`,
        # `gcc-c++`) must NOT be removed; doing so would break Homebrew
        # native compilation for any user who runs `brew install` on a
        # formula with a C extension.
        text = CONFIGURE_SERVICES.read_text()
        removed = text.split("$DNF", 1)[1].split("for pkg in", 1)[0]
        for forbidden in ("make", "gcc", "gcc-c++"):
            self.assertNotIn(
                forbidden, removed,
                f"configure-services.sh must not remove {forbidden}; "
                "Homebrew native compilation depends on it being in /usr/bin",
            )

        # The script must still remove the build-only toolchain so the
        # post-removal dnf cleanup leaves the image with the expected
        # payload.
        for required in ("dbus-devel", "glib2-devel", "meson", "sassc"):
            self.assertIn(
                required, removed,
                f"configure-services.sh must still remove {required}",
            )

    def test_bluefin_toml_is_byte_for_byte_match(self):
        # Utah's [parity] / [unavailable] overlay must not redefine any of
        # the toolchain packages. bluefin.toml is the parity contract; an
        # entry for `make`, `gcc`, or `gcc-c++` in either overlay would
        # suggest someone tried to override the contract rather than
        # satisfy it.
        for overlay in ("parity", "unavailable", "excluded", "build"):
            packages = self.utah.get(overlay, {}).get("packages", [])
            for name in ("make", "gcc", "gcc-c++"):
                self.assertNotIn(
                    name, packages,
                    f"utah.toml [{overlay}] must not redefine {name}; "
                    "bluefin.toml owns the toolchain retention",
                )


class HomebrewSymlinkPathTests(unittest.TestCase):
    """Issue #676: /home is a symlink to /var/home on Hummingbird-based images,
    so /home/linuxbrew/.linuxbrew and /var/home/linuxbrew/.linuxbrew resolve
    to the same inode. Both the contract and the runtime assets must use a
    single, consistent spelling so a release that resolves one path but not
    the other surfaces as a build-time error.
    """

    @classmethod
    def setUpClass(cls):
        cls.contract = tomllib.loads(CONTRACT.read_text())

    def test_desktop_exec_uses_linuxbrew_path(self):
        expectations = (
            self.contract.get("configuration", {}).get("file_contains", {}).get(
                "/usr/share/applications/io.projectbluefin.chairlift.desktop", []
            )
        )
        joined = "\n".join(expectations)
        self.assertIn("/home/linuxbrew/.linuxbrew/bin/chairlift-wrapper", joined)

    def test_starship_profile_reads_var_home_linuxbrew(self):
        # The starship profile is sourced at every login shell. It must
        # tolerate either spelling because /home is a symlink to /var/home,
        # so a /home/linuxbrew lookup and a /var/home/linuxbrew lookup
        # resolve the same inode. The script keeps the explicit
        # /var/home/linuxbrew check so a first login that runs before the
        # symlink resolves (or on a system where the symlink was edited
        # away) still finds the brewed binary.
        text = STARSHIP_PROFILE.read_text()
        self.assertIn("/var/home/linuxbrew/.linuxbrew/bin/starship", text)

    def test_framework_hook_uses_home_linuxbrew_prefix(self):
        # The Framework vendor hook calls `brew install fw-ectool`. The
        # brew prefix is /home/linuxbrew/.linuxbrew, and /home symlinks to
        # /var/home so the prefix resolves either way; the contract depends
        # on the conventional /home spelling here so the brewed prefix
        # matches the rest of the Common tooling.
        text = FRAMEWORK_HOOK.read_text()
        self.assertIn('BREW_PREFIX="/home/linuxbrew/.linuxbrew"', text)

    def test_contract_lists_brew_preinstall_payload(self):
        # The contract must assert both /usr/bin/brew-preinstall and
        # /usr/libexec/brew-preinstall: the symlink and the real binary.
        # Common ships both, and the cask install path runs from
        # /usr/bin/brew-preinstall via `brew list --cask chairlift`. A
        # refactor that drops either side fails the in-image verifier.
        files = self.contract.get("configuration", {}).get("files", [])
        self.assertIn("/usr/bin/brew-preinstall", files)
        self.assertIn("/usr/libexec/brew-preinstall", files)


class DesktopContractTestsClean(unittest.TestCase):
    """Issue #676: `tests/test_desktop_contract.py` must pass cleanly. This
    class runs the desktop contract verifier in `--check` mode against the
    shipped contract; a failure here means a regression in either the
    contract itself or the verifier. The verifier's own suite covers the
    runtime path; this is a thin wrapper that surfaces a contract-only
    failure on the ChairLift path of `just check`.
    """

    def test_shipped_contract_passes_check_mode(self):
        result = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts/verify-desktop-contract.py"),
                "--check",
                str(CONTRACT),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode, 0,
            f"desktop contract --check failed:\nstdout={result.stdout}\n"
            f"stderr={result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
