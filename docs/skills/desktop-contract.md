---
name: desktop-contract
version: "1.0"
last_updated: "2026-09-30"
id: desktop-contract
one_line_purpose: Maintain Utah identity, Bluefin desktop defaults, and first-boot Flatpak policy.
entry_point: docs/skills/desktop-contract.md
category: contracts
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [desktop, branding, gnome, flatpak]
description: >-
  The runtime desktop contract in contracts/bluefin-desktop.toml and its
  in-image verifiers. Use when changing branding, os-release, service
  presets, GNOME extensions, or first-boot Flatpak behavior.
metadata:
  type: policy
---

# Desktop Contract

`contracts/bluefin-desktop.toml` is the runtime contract for Utah's
Bluefin-derived desktop experience on top of Hummingbird. It is intentionally
separate from `packages/bluefin.toml`: package parity proves RPMs; this file
proves Utah identity, Bluefin desktop defaults, and first-boot Flatpak policy
(header comment, `contracts/bluefin-desktop.toml`). The contract is data; two
verifiers enforce it — `scripts/verify-desktop-contract.py` for the TOML
itself and `scripts/verify-gnome-extensions.py` for the bundled extensions.

## What the contract asserts

The TOML's sections are the contract's table of contents:

- **`[branding]`** — files that must exist (Bluefin logos, backgrounds, the
  `zz0-bluefin-modifications` gschema override, fastfetch and Bazaar count
  files) plus the os-release identity. The identity fields are exact values:
  `NAME=Utah`, `ID=hummingbird`, `ID_LIKE=fedora`, `VERSION_CODENAME=Utahraptor`,
  `DEFAULT_HOSTNAME=utah`, `IMAGE_ID=utah`, and the projectbluefin.io URLs.
  `ID`, `VERSION_ID` and `CPE_NAME` are left at the Hummingbird base values
  (issue #377) so CVE scanners still recognise the image as Hummingbird-based;
  `CPE_NAME` is asserted as a base-identity pattern
  (`^cpe:/a:redhat:hummingbird:\d+$`) rather than a utah-branded exact value.
  `[branding.os_release_patterns]` shapes the fields the build generates:
  `PRETTY_NAME` is `Utah (Version: ...)`, `VERSION` carries `(Hummingbird)`,
  `VARIANT_ID` starts with `utah`. The verifier checks both `/usr/lib/os-release` and
  `/etc/os-release` (the file the GNOME About panel reads, which is a regular
  file rather than a symlink on some bases), and fails if either is missing or
  drifts from the contract.
- **`[branding.image_info]`** — `/usr/share/ublue-os/image-info.json` must
  name image `utah`, vendor `projectbluefin`, base `hummingbird`, with the
  flavor pattern `(main|nvidia|gaming|nvidia-gaming)` and the matching
  `ostree-image-signed` ref pattern.
- **`[configuration]`** — the dconf distro databases and locks under
  `/etc/dconf/db/distro.d/` must exist, plus the GDM keyfile under
  `/etc/dconf/db/gdm.d/01-bluefin-gdm-logo` that overrides
  `org.gnome.login-screen.logo` to point at the Bluefin mark; otherwise
  gnome-shell falls back to `fedora-logos`' Fedora wordmark at the greeter
  (#378). `file_contains` pins the live configuration: the gschema override
  references Bazaar and the Bluefin background path, the custom command menu
  points at `docs.projectbluefin.io`, the keybindings set
  `xdg-terminal-exec`, the GDM keyfile declares the login-screen schema and
  the Bluefin asset path.
- **`[flatpak]`** — first-boot policy: the Flathub remote
  (`https://dl.flathub.org/repo/`), the Bazaar preinstall, the
  `99-flatpaks.sh` privileged-setup hook, and the system-flatpaks Brewfile
  whose app list the contract enumerates.
- **`[services]`** — systemd units the preset must enable: `gdm.service`,
  `bluetooth.service`, `ublue-system-setup.service`, `flatpak-preinstall.service`,
  `flatpak-nuke-fedora.service`, `brew-setup.service`, `dconf-update.service`,
  `bootc-unified-storage.service`, `uupd.timer`. Update policy delegates
  background updates to `uupd.timer`; `bootc-fetch-apply-updates.timer` and
  `bootc-fetch-apply-updates.service` are masked in `/etc` and `/usr/lib` (and
  disabled in `85-utah-desktop.preset`) so cross-vendor `/etc` 3-way merges
  (e.g. switching from Bluefin) do not carry active `timers.target.wants`
  symlinks that bypass uupd staging or undo manual rollbacks. Switchers can
  also manually verify or mask them if a local `/etc` symlink was preserved.

  A unit being enabled in the built image is not the same as it being enabled
  on a booted one. bootc applies presets on first boot, and Hummingbird's
  `99-default-disable.preset` turns off every unit no preset names, so a build
  -time `systemctl enable` without a line in `85-utah-desktop.preset` (or a
  vendor preset such as brew's `01-homebrew.preset`) is undone. That is how
  `flatpak-preinstall.service` shipped "enabled" and booted disabled, leaving
  non-ISO installs without Bazaar. `DesktopUnitEnablementTests` holds the
  script and the preset in agreement. Check a booted VM with `systemctl
  is-enabled <unit>` when in doubt.

  The Flathub remote is a `/etc/flatpak/remotes.d` descriptor, applied to the
  repo once, when `/var/lib/flatpak/repo` is created. Flathub's descriptor has
  no collection ID, while common's preinstall entries pin
  `CollectionID=org.flathub.Stable`, and `flatpak preinstall` silently skips a
  remote whose ID differs ("Nothing to do."). `configure-services.sh` adds
  `DeployCollectionID=org.flathub.Stable` to the descriptor at build time.
  Editing the descriptor on a booted system changes nothing once the repo
  exists (`xa.applied-remotes`); use `flatpak remote-modify --collection-id`.

  Once enabled, the preinstall's Flathub download held `graphical.target` for
  60 s on first boot: common's unit is a oneshot wanted by `multi-user.target`.
  `flatpak-preinstall.service.d/10-utah-background.conf` sets
  `DefaultDependencies=no` with the equivalent explicit ordering, so it runs
  in the background (the unit keeps its own `network-online.target` ordering).

The common image pinned in `Containerfile` (`COMMON_IMAGE_SHA`) includes
`projectbluefin/common#1284`, which removed Warehouse and smile from Bluefin's
default Brewfile. Utah follows that upstream default set; the ordered app
contract changes with the pinned artifact rather than overriding its Brewfile.
Read the live pin from `Containerfile` — Renovate rewrites it there, so no
digest is repeated in this document. The verifier still compares the actual
inherited Brewfile. This source change does not
introduce an uninstall for existing user-installed applications.

The same pinned Common artifact owns ChairLift's alpha.4 helper and ublue
policy. Their payload matches the release and requires administrator
authentication for all nine actions. The image contract requires both files;
Utah does not layer a duplicate release install over Common's copy.

## NoNewPrivileges and SELinux domain transitions

Hummingbird's hardened units can set `NoNewPrivileges=yes` where Fedora's do
not. Under NNP a domain transition needs an explicit `nnp_transition`
permission, and when the targeted policy lacks one the daemon silently runs in
`init_t` and fails on its first labelled write. avahi-daemon (0.9~rc4) is the
known case (#443): it exited 255 every start, so mDNS never worked.
`avahi-daemon.service.d/10-utah-selinux.conf` sets `NoNewPrivileges=no`; the
daemon still runs confined as `avahi_t`. Spot others on a booted VM with
`journalctl -b | grep nnp_transition` and `ps -eZ` (a daemon showing
`init_t` is the symptom).

## Tolerating a non-zero exit in a unit file

Tolerate an expected non-zero exit per command with the `ExecStart=-` prefix
rather than with `SuccessExitStatus=`. `SuccessExitStatus=1` is unit-wide, so
it also masks a genuine exit 1 from a *later* command in the same unit — for
example the `touch` in `flatpak-nuke-fedora.service` that stamps
`/var/lib/flatpak/.fedora-initialized`, whose real failure would be reported as
success.

Two rules follow:

- A unit that must ignore something possibly being absent (a remote, a file)
  takes `ExecStart=-` on that one command. Nothing else in the unit is affected.
- Create the parent directory in an `ExecStartPre=` when the stamp target's
  directory is absent on a freshly installed image — `/var/lib/flatpak` is, so
  `flatpak-nuke-fedora.service` runs `mkdir -p` before `touch`.

Do not reach for `flatpak remote-delete --force` to make a re-run succeed: it
only changes the "remote has installed refs" guard, so on a non-interactive
rebase it deletes the `fedora` remote *along with* the apps installed from it,
leaving those refs with no origin to update from. The `-` prefix alone already
covers the missing-remote case.

This rule is enforced, not just documented:
`tests/test_systemd_exit_tolerance.py` scans every unit shipped under
`system_files/` and `iso/live/` and fails if one carries a
`SuccessExitStatus=`, an unprefixed `remote-delete`, or a `--force` on one. It
runs inside `just test`, which `just check` invokes, so the `--force` variant
of a `remote-delete` fails the build instead of waiting to be caught in review.
The scan covers unit files only: a shell script may still use `--force` on a
throwaway remote it created itself, as `iso/live/src/install-flatpaks.sh` does
for the live image's own `installer-local` remote.

## GNOME extensions are pinned submodules

Bluefin's GNOME extension submodules are retained with their normal build
step. `.gitmodules` pins eight of them by URL and branch under
`system_files/shared/usr/share/gnome-shell/extensions/` — appindicator,
bazaar-integration, blur-my-shell, caffeine, custom-command-list,
dash-to-dock, gradia-integration, and gsconnect. Search Light was dropped:
its shader code calls `set_shader_source`, which GNOME 51 removed, so the
extension errored at load and failed the ISO end-to-end test on every
flavor.

`scripts/verify-gnome-extensions.py` asserts every one declares GNOME 51 in
its `metadata.json`. It runs in two modes from the same script:

- **Source mode** — `--source` checks the submodule checkouts in the working
  tree; this is what `just check` runs.
- **Installed mode** — the default checks `/usr/share/gnome-shell/extensions`
  under an image root; this is what runs in the Containerfile as
  `/usr/local/libexec/utah-verify-gnome-extensions`, right after
  `utah-build-gnome-extensions`.

Building GSConnect runs meson install. Because `desktop-file-utils` is not
published by Hummingbird or Utah's repository, `scripts/build-gnome-extensions.sh`
disables GSConnect's `update_desktop_database` meson post-install hook to avoid
failing on the missing utility. MIME and schema databases are handled by the
system and glib-compile-schemas.

Declaring GNOME 51 in `metadata.json` is necessary, not sufficient: both
failures below passed the metadata check, built cleanly, and only showed up
in a booted session's journal (`journalctl _UID=1000 | grep -i extension`).

- **GSConnect** tracks upstream `GSConnect/gnome-shell-extension-gsconnect`
  at `v73`. Earlier GSConnect subclassed `GjsPrivate.DBusImplementation`, a
  final GType since GNOME 48, in `shell/clipboard.js`, `wl_clipboard.js` and
  `service/utils/dbus.js`. Utah once text-patched only the first, so the
  daemon still threw "Cannot inherit from a final type" on every login and
  GSConnect never started. v73 stopped subclassing it (upstream 11be9b7f); the
  build now greps the whole `src/` tree and fails if a subclass returns. The
  `projectbluefin` fork it used to track only added the GNOME 51 declaration,
  which upstream carries itself.
- **Dash to Dock** is pinned to the v109 release commit on `master` (upstream
  publishes no `extensions.gnome.org-v1xx` branch past v106). v106 imports
  `resource:///org/gnome/shell/ui/pointerWatcher.js`, which GNOME 51 removed,
  so the dock silently never loaded; upstream 38545156 moved it to
  `Meta.CursorTracker`.

`tests/test_gnome_extensions.py` reads the pinned sources for both, so a pin
moved back to an affected revision fails `just check`. To validate an
extension bump without an image build, copy the built tree over the installed
one on a booted VM under `bootc usr-overlay`, restart `gdm`, and read the
session journal and `gnome-extensions info <uuid>` (`State: ACTIVE`).

## The GDM greeter logo is Bluefin, not Fedora (#378)

Without an `org.gnome.login-screen.logo` override, GDM shows the schema
default — `/usr/share/pixmaps/fedora-gdm-logo.png`, shipped by
`fedora-logos`. The greeter on every installed Utah therefore opened with
the Fedora wordmark.

A first attempt at the fix was a pixmap overlay: `common` already ships a
Bluefin-branded `system_files/bluefin/usr/share/pixmaps/fedora-gdm-logo.png`,
and Utah copies `common`'s full `system_files/bluefin/` tree into the
image at `Containerfile:118` (`cp -a /tmp/utah-bluefin/. /`). That overlay
would have replaced the Fedora wordmark without any dconf change. It is
not effective in practice, however: `utah-install-packages` runs
afterwards at `Containerfile:148-150`, and `baselines/utah/rpms.tsv` shows
`fedora-logos 42.0.1-6.hum1` in the image — the package reinstalls its
own `/usr/share/pixmaps/fedora-*.png` (GDM logo, plymouth logo,
about-dialog logo, system-logo-white), clobbering every overlaid
`pixmaps/fedora-*` file. The root-cause ticket is #398; this fix uses a
second mechanism, not the pixmap overlay.

GDM uses its own dconf profile (`/etc/dconf/profile/gdm`, provided by the
gdm RPM). Utah ships a single keyfile,
`system_files/shared/etc/dconf/db/gdm.d/01-bluefin-gdm-logo`, that sets
`logo` to `/usr/share/pixmaps/bluefin-gdm-logo.png`, a 150x61 Bluefin
wordmark Utah ships in `system_files/shared/usr/share/pixmaps/`. It is a
copy of `common`'s `fedora-gdm-logo.png` under a Utah-owned name, so no logos
RPM owns or erases it.

**Do not point `logo` at `bluefin-logos/bluefin.png`.** gnome-shell draws the
greeter logo at its natural size; that file is 372x493 and fills the login
screen. Bluefin-LTS keeps the greeter logo small by using `common`'s 150x61
`fedora-gdm-logo.png`. A unit test caps the shipped logo at 256x128.

`scripts/configure-branding.sh` runs `dconf update` after stamping the
contract files, so the greeter database is compiled at build time and a
malformed keyfile fails the build rather than the post-install E2E that
originally caught the regression. The compile is guarded on `/usr/bin/dconf`
so it is a no-op on a host without the gnome-desktop stack (CI without
`dnf install` of it).

`dconf update` does **not** validate the logo path — it compiles keyfiles
and stores `logo` as an opaque string, so a dangling path compiles
cleanly. The image itself is guarded by the new `[branding].files`
entry for `/usr/share/pixmaps/bluefin-gdm-logo.png` in
`contracts/bluefin-desktop.toml`, enforced by
`utah-verify-desktop-contract` in the same `RUN` layer.

`[configuration].files` asserts the keyfile's path on disk;
`[configuration].file_contains` pins both the schema header and the
asset path so a stray edit that points `logo` somewhere else fails the
build.

## Services and login defaults

Hummingbird defaults to a server preset and disables unlisted services, so
the desktop policy is applied explicitly. `scripts/configure-services.sh`
mirrors bluefin-lts's `40-services.sh`: it applies the desktop presets,
enables GDM, input-remapper, firmware updates, Tailscale, uupd, user setup and resolved,
configures authselect, and removes the extension build toolchain before
cleanup (Containerfile RUN comment; originated in `docs/building.md`'s former
design section and now lives in this skill).

Hummingbird's base does not include `systemd-resolved` by default; it is listed
under `[services]` in `packages/utah.toml` and configured in
`scripts/configure-services.sh`, which also disables `PrivateTmp` on
`systemd-resolved.service` for bootc early-boot DNS resolution.

### The serial getty is masked (#103)

Two Hummingbird defaults compose into a desktop bug. The base declares the
serial console as a kernel argument in `/usr/lib/bootc/kargs.d/00-base.toml`
(`console=ttyS0,115200n8`), and systemd-getty-generator instantiates
`serial-getty@ttyS0.service` for every serial `console=` on the cmdline. On
hardware with no serial port the agetty dies on EIO and respawns roughly every
ten seconds for the whole session — 197 journal entries in one boot on the
ThinkPad X230 that filed it, and a pointless wakeup each time on battery.
Bluefin carries neither half, which is why it is silent.

The karg cannot be withdrawn from this repository. bootc's `kargs.d` is
additive only, and removing an argument the base image declared there is
documented undefined behavior — so Utah pins the outcome instead of the cause
and masks the unit from both directions the repo already uses:

- `scripts/configure-services.sh` runs `systemctl mask serial-getty@ttyS0.service`
  and writes the `/usr/lib/systemd/system/serial-getty@ttyS0.service` → `/dev/null`
  symlink, the same `/etc` + `/usr/lib` pair the update timer uses so a
  cross-vendor 3-way merge cannot resurrect it.
- `85-utah-desktop.preset` carries `disable serial-getty@ttyS0.service`.
  Presets are read in lexicographic order and the first match wins, so 85-*
  outranks the base's `90-systemd.preset`.
- `[services].masked` in `contracts/bluefin-desktop.toml` lists the unit, so
  the in-image verifier fails the build if the mask is dropped.

The mask names the instance, not `serial-getty@.service`: `ttyS0` is the port
the base names, and a genuinely attached serial device on another port still
gets its login. A mask is also the only lever that works here — the generator's
`getty.target.wants` symlink is created in `/run` at boot, so it cannot be
deleted at build time, and a preset entry alone would not stop it.

### The fwupd-refresh unit pins a static user (#385)

Hummingbird builds fwupd with `-Dsystemd_unit_user=""`, which expands the
`@user@` template in `data/motd/fwupd-refresh.service.in` to `DynamicUser=yes`.
Combined with the unit's `CacheDirectory=fwupdmgr`, every boot triggers
systemd's pre-existing-public → `/var/cache/private/fwupdmgr` migration; in
this bootc image the rename returns `EACCES` (a policy denial on `/var/cache`,
not a plain-ownership problem), the unit exits 1, and firmware metadata never
refreshes.

The fix pins the unit to a static user so the migration code never runs.
Both halves must land together — the drop-in alone leaves the service with no
user, and the sysusers fragment alone leaves the unit running as a dynamic
user and still failing:

- `system_files/shared/usr/lib/systemd/system/fwupd-refresh.service.d/10-utah-fwupd-refresh-user.conf`
  sets `User=fwupd-refresh` / `Group=fwupd-refresh` / `DynamicUser=no`,
  plus the hardening directives that `DynamicUser=yes` would have implied
  (`NoNewPrivileges=yes`, `PrivateTmp=yes`, `RemoveIPC=yes`,
  `RestrictSUIDSGID=yes`, `ProtectSystem=strict`). `DynamicUser=no` is the
  load-bearing directive: systemd's `exec_directory_is_private()`
  (`src/core/execute.c`) gates the pre-existing-public → `/var/cache/private`
  migration on `context->dynamic_user` alone, not on whether `User=` is set,
  so without it the migration still fires and the unit still fails the same
  way. `User=` + `DynamicUser=yes` is a documented legal combination
  (`systemd.exec(5)`), but it is not the combination we want here. The user
  name `fwupd-refresh` is load-bearing: upstream
  `policy/org.freedesktop.fwupd.rules` grants `refresh-remote` /
  `get-remotes` to `subject.user == "fwupd-refresh"` unconditionally, so
  renaming the user would break refresh at the polkit layer.
- `system_files/shared/usr/lib/sysusers.d/utah-fwupd-refresh.conf` allocates
  the user with auto-allocated UID/GID. systemd-sysusers runs from
  `systemd-sysusers.service` before `local-fs.target`, so by the time
  `fwupd-refresh.timer` fires the user exists. `sysusers.d(5)` defaults
  `HOME` to `/` when the HOME field is omitted.

The daemon (`fwupd.service`) is unaffected: it runs as root with no
`DynamicUser=` and uses `CacheDirectory=fwupd`, so its cache lives under
`/var/cache/fwupd` directly with no dynamic-user migration path. Firmware
flashing still goes through the daemon, which keeps its existing root
lifecycle. The pairing is asserted by `FwupdRefreshDropInTests` in
`tests/test_desktop_contract.py`.

## The verifiers run twice

The same verifier runs in the Containerfile and on demand, so a local image
or a CI artifact can be checked after the fact (recipe comment, `Justfile`,
`check-desktop-contract`):

- **In the image build** — the desktop RUN step ends with
  `utah-verify-desktop-contract /usr/share/utah/bluefin-desktop.toml`, after
  branding and services are configured; a contract failure fails the build.
  The extension verifier runs earlier in the same step.
- **On demand** — `just check-desktop-contract <ref>` (default
  `localhost/utah:testing`) podman-runs both verifiers inside an
  already-composed image: both verifiers and the desktop contract are
  bind-mounted read-only from the working tree under `/tmp`. The extension
  verifier uses installed mode to check the image's bundled extensions.
  Build-time helpers under `/usr/local/libexec` are unavailable after
  `clean-stage.sh` removes `/var/usrlocal` (the target of `/usr/local`), so
  on-demand checks must not depend on those helpers surviving cleanup.
- **Off-image** — `verify-desktop-contract.py --check` validates the contract
  TOML itself in source-only CI and is part of `just check`; it asserts
  nothing about any image.

## Verification

```bash
python3 scripts/verify-desktop-contract.py --check contracts/bluefin-desktop.toml
python3 scripts/verify-gnome-extensions.py --source
just check-desktop-contract localhost/utah:testing  # requires a locally built image
```
