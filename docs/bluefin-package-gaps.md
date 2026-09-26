# Utah vs Bluefin package gaps — decision record (DRAFT)

Status: Final. Accepted by the owner in the grill session of 2026-09-26
("ok finalize decioms").

## Evidence

- `podman run --rm ghcr.io/projectbluefin/bluefin:stable rpm -qa` → 1770 packages (`/tmp/rpms-bluefin-stable.txt`)
- `podman run --rm ghcr.io/projectbluefin/utah:testing rpm -qa` → 1047 packages (`/tmp/rpms-utah-testing.txt`)
- 982 shared names; 973 differ (Utah retags `.hum1`/`.hum1.bfin` at newer upstreams: GNOME/Adwaita 51 vs 50, NM 1.58 vs 1.56).
- 785 names only in Bluefin; 62 only in Utah.
- Repo ground truth: `packages/utah.toml` (398 lines, documents most gaps with tracking issues), `packages/bluefin.toml`.

## Clusters

### A. Deliberate strategy (documented in utah.toml) — confirm standing
- firefox RPM out, Flatpak in via desktop contract (#35, utah-packages#112).
- CUDA out (7.68 GB baked in vs container via toolkit; nvidia-container.repo present).
- grub2* excluded (boot breakage; returns with utah-packages#238).
- anaconda-live/slitherer out (factory installer policy, #83).

### B. Blocked on factory/Hummingbird (tracked) — confirm watch list
fish, zsh, libgda(-sqlite) (#106), ppp (#107), microcode_ctl, emoji/math default fonts
(utah-packages#147), alsa-utils (#146), cmake (Hummingbird missed rebuild: needs
libjsoncpp.so.26, repo has .27).

### C. Devel leakage (NEW, from this diff) — undecided
`systemd-devel libselinux-devel libsepol-devel libffi-devel libblkid-devel
libmount-devel pcre2-devel sysprof-capture-devel meson-srpm-macros ninja-build`
ship in the image. `[build]` only declares dbus-devel/glib2-devel/meson/sassc/unzip;
the rest likely rode in as their dependency chains and survived
`configure-services.sh` removal, which strips top-level names only.

### D. Unexplained deltas — undecided
vscode (`code`), toolbox, nerd-fonts, gnome-shell-extension pack
(apps-menu/places/window-list/launch-new-instance), kernel-modules-extra,
kmod-v4l2loopback, vpnc, rpmfusion ffmpeg (Utah: ffmpeg-free), vim/samba present
in Utah but not Bluefin.

## Decisions

### D1 (DECIDED, still Draft): strip the devel leakage
Remove the ~10 build-time packages (cluster C) from the shipping image —
extend the post-extension-build removal to the full build dep closure
(or multistage the extension build). Rationale: compiler headers in a
stable-bound image widen the update surface for zero runtime benefit.

## Questions (asked one at a time in the grill)
1. Q1: devel leakage — ~~strip or accept?~~ DECIDED: strip (D1).

### D2 (DECIDED, still Draft): Flatpak-first browser and container-only CUDA are permanent Utah identity
Not temporary divergence. Rationale (owner): Utah is newer and Bluefin is
stuck with old decisions — relitigating per release is pure cost. The firefox
exclusion stays test-enforced; CUDA stays out (7.68 GB baked in vs toolkit +
`podman run --device nvidia.com/gpu=all`).
2. Q2: Flatpak-first / container-CUDA posture — ~~permanent or temporary?~~ DECIDED: permanent (D2).

### D3 (DECIDED, still Draft): shells and fonts
- fish stays OUT permanently — not a factory watch item, an exclusion.
- zsh is wanted on the image (unblocks when the ruby4.0/texi2html buildroot
  conflict resolves), but NOT as default shell — bash stays default; a
  default-shell change is too controversial to bundle here.
- Core emoji (google-noto-emoji-color-fonts) is wanted, but NOT Bluefin's font
  sprawl — no nerd-fonts bulk, no math-fonts excess.
3. Q3: factory-blocked list — ~~watch or fork?~~ DECIDED per D3; remainder
   (libgda, ppp, microcode_ctl, alsa-utils, cmake) stays on upstream watch.

### D4 (DECIDED, still Draft): cluster D split
- toolbox and vscode (`code`) stay OUT — both live in Homebrew now, same
  reasoning as D2's Flatpak-first posture.
- GNOME Shell extensions move to git submodules under
  `system_files/usr/share/gnome-shell/extensions/`, matching bluefin-lts
  (verified `.gitmodules` there; Dakota submodule claim not confirmed via API).
  Replaces/augments the pinned `build-gnome-extensions.sh` approach.
- v4l2loopback (kmod) is WANTED — virtual-camera support in scope.
- kernel-modules-extra and vpnc stay OUT on delegated judgment: the former is
  an unscoped dragnet, the latter legacy (modern VPN paths exist); both return
  only with a named use case.
4. Q4: cluster D deltas — ~~intended or backlog?~~ DECIDED: split (D4).

## Settled contract (pending explicit acceptance)
- Goal: Utah's package set is a deliberate, newer divergence from Bluefin —
  smaller core, Flatpak/brew-first apps, container-only CUDA — not a parity backlog.
- Non-goals: Bluefin version parity (GNOME 51 vs 50 is fine); default-shell
  change; own forks of factory-blocked packages.
- Decisions: D1 strip devel leakage; D2 permanent Flatpak-first/container-CUDA;
  D3 fish out, zsh wanted (bash stays default), core emoji yes / font sprawl no;
  D4 toolbox+vscode out (brew), extensions via git submodules, v4l2loopback in,
  kernel-modules-extra + vpnc out.
- Constraints: wanted-but-blocked packages all have open tracking issues —
  zsh #289, emoji #290, v4l2loopback #291, microcode_ctl #292, alsa-utils #293,
  cmake #294, libgda #106, ppp #107, grub2-cdboot #253 (plus factory-side
  utah-packages#112 #146 #147 #238). No forks; upstream watch only.
- Risks: devel-strip must remove dep closures, not just top-level names, or the
  leakage recurs; submodule move changes extension pinning/review flow.
- Validation: re-run the rpm-qa diff Bluefin:stable vs Utah:stable after
  implementation; devel list must be empty, D4 inclusions present.
- Unresolved: Dakota submodule claim unconfirmed (LTS pattern verified instead).
- Owning record: this document. Implementation (D1 strip, submodule move,
  v4l2loopback/zsh/emoji additions as unblocks land) needs separate requests.
