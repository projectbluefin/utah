## Dakota system defaults

Utah installs Dakota's and GNOME OS's configuration elements as the factory's
`dakota-config-*` and `gnome-os-config-*` subpackages (#1265; element list and
ownership rules in utah-packages `docs/skills/config-packages.md`), never as
copies in `system_files`, and asserts the files it relies on in
`contracts/bluefin-desktop.toml` (`[configuration].files` plus
`file_contains`):

- `dakota-config-wireplumber-pcsp`: `51-disable-pcsp.conf` hides the
  PC-speaker ALSA card so it never becomes the default sink after resume.
- `dakota-config-kexec-disable`: `60-kexec-disable.conf` sets
  `kernel.kexec_load_disabled = 1`; bootc upgrades and soft-reboot do not use
  kexec, and Easy Anti-Cheat requires it off.
- `dakota-config-udev-groups`, `-firstboot-date`, `-uupd`, `-flatpak-apps`
  (BudsLink preinstall), `-custom-command-menu-dconf`
  (`08-dakota-extension-defaults`), and the rest of the adopted list in
  `packages/utah.toml` `[parity]`. `dakota-config-firstboot-date`'s
  `ublue-firstboot-date.path` waits for systemd-timesyncd's sync flag, so the
  install date is recorded only once timesyncd replaces chrony (#1228).
- Dakota's `files/polkit/org.freedesktop.NetworkManager.rules` (local wheel
  users modify system connections without a password) is already shipped,
  with an identical rule body, by Hummingbird's NetworkManager RPM at
  `/usr/share/polkit-1/rules.d/`. Utah asserts it in the contract and does not
  overlay a copy over the package-owned file.
- Dakota's `/etc/gitconfig` (#1259) is freedesktop-sdk `components/git-lfs.bst`'s
  integration command, which appends the `[filter "lfs"]` stanza
  (`required`, `clean`, `smudge`, `process`). Hummingbird's `git-lfs` RPM
  writes the same four keys from its `%post` (`git lfs install --system`), so
  Utah installs the package and asserts the stanza; it ships no copy.
- `system_files/shared/etc/pkcs11/modules/opensc.module` (#1257) is GNOME OS's
  `files/opensc/opensc.module`: `disable-in` keeps goa-daemon, gsd-smartcard,
  the two Evolution data-server factories and gnome-shell from loading opensc,
  so they never hold a smart card open and GnuPG keeps exclusive access.
  p11-kit reads `/etc/pkcs11/modules/` in place of a same-named file in
  `/usr/share/p11-kit/modules/`, where the opensc RPM's plain copy lives;
  Utah's file can't go there, because the package transaction runs after
  `system_files` is copied and would overwrite it.

## Firewall and fallback DNS

Both come from the factory's `gnome-os-config` package, built from
gnome-build-meta at the commit Dakota's junction tracks:

- `gnome-os-config-firewalld` ships GNOME OS's `lax` zone
  (`/usr/lib/firewalld/zones/lax.xml`): ssh, mDNS, Samba client,
  DHCPv6 client, TCP/UDP 1025-65535 and forwarding. `configure-services.sh`
  applies `gnomeos-deps/firewalld.bst`'s two `firewalld.conf` edits,
  `DefaultZone=lax` and `IPv6_rpfilter=loose`, with `sed --follow-symlinks`
  so firewalld's variant symlink stays. Bluefin classic used Silverblue's
  FedoraWorkstation zone, which is the same zone without mDNS.
- `gnome-os-config-resolved-fallback-dns` ships
  `/usr/lib/systemd/resolved.conf.d/50-gnome-os-fallback-dns.conf`, generated
  from `core-deps/systemd-base.bst`'s `fallback-dns-servers` (Cloudflare,
  DNS-over-TLS server names included). GNOME OS compiles that list into
  systemd-resolved; Fedora builds with none. resolved only uses it when no
  link or global DNS server is configured.

## Swap: zswap over /var/swap/swapfile

Swap follows Dakota (#1224), not zram: `zram-generator(-defaults)` are gone.

- `usr/lib/bootc/kargs.d/20-zswap.toml` and `usr/lib/sysctl.d/60-zswap.conf`
  are Dakota's `elements/bluefin/zswap-config.bst`, byte for byte.
- `bluefin-swapfile-init` (+ `.service`) and `var-swap-swapfile.swap` are
  Dakota's `files/swapfile/`. The init unit writes min(RAM/2, 8 GiB) on first
  need and skips small disks and filesystems that cannot hold a swapfile (the
  live ISO's overlay). The `.swap` unit is pulled in by a static
  `swap.target.wants` link, so existing installs get it on upgrade.
- Utah-only: Hummingbird builds zstd as a module (`CONFIG_CRYPTO_ZSTD=m`),
  and zswap resolves `zswap.compressor=zstd` during kernel init, before the
  root is mounted. `usr/lib/dracut/dracut.conf.d/50-utah-zswap-zstd.conf`
  puts the module in the initramfs, and `scripts/regenerate-initramfs.sh`
  fails the build when a modular zstd is missing from it. Without it zswap
  falls back to lzo with only a kernel log line.
- Utah-only: Dakota has no SELinux. Fedora's policy has no rule for
  `/var/swap`, so the file would be `var_t` and swapon denied.
  `usr/share/selinux/packages/utah-swapfile.cil` labels
  `/var/swap/swapfile` `swapfile_t`; `configure-services.sh` installs it with
  `semodule -n -i` into the store under `/usr/lib/selinux` (so ostree's
  per-deployment rebuild keeps it) and checks `matchpathcon`. The init
  script runs `restorecon` on the file before writing it.
- The gaming flavors' OGC kernel is built from `x86_64_defconfig`, which has
  no zswap; `scripts/install-ogc-kernel.sh` builds in `ZSWAP` and
  `CRYPTO_ZSTD` and gates on both, so the kargs take effect there too
  (`docs/skills/kernel-cache.md`).

## Fonts

Dakota ships two fonts Utah's base manifest does not (#1240), both factory
packages in `packages/utah.toml` `[parity]`:

- `google-noto-serif-cjk-vf-fonts`: GNOME OS's Noto Serif CJK, next to the
  Sans CJK set the base manifest already carries.
- `nerd-fonts-symbols`: Dakota's `bluefin/nerd-fonts-symbols.bst`, the
  icon-only Symbols Nerd Font that common's fastfetch config falls back to.
  The package ships `10-nerd-font-symbols.conf` in
  `/usr/share/fontconfig/conf.avail` and enables it with the same
  `/etc/fonts/conf.d` link the element creates, so Utah adds no fontconfig
  file of its own; the contract asserts both paths and the family name.

## Lid and sleep

Lid and sleep handling stays at systemd-logind's defaults (suspend). Dakota
ships no logind or sleep override, and Utah's swapfile has no `resume=`
argument (nor has Dakota's), so the suspend-then-hibernate override inherited
from bluefin-lts's `40-services.sh` has no resume target and was removed from
`scripts/configure-services.sh`.
