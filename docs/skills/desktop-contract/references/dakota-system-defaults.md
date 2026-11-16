## Dakota system defaults

Utah copies these small system defaults from Dakota (`testing`) and asserts
each in `contracts/bluefin-desktop.toml` (`[configuration].files` plus
`file_contains`):

- `system_files/shared/usr/share/wireplumber/wireplumber.conf.d/51-disable-pcsp.conf`
  (Dakota `files/wireplumber/`, `elements/bluefin/wireplumber-pcsp.bst`) hides
  the PC-speaker ALSA card so it never becomes the default sink after resume.
- `system_files/shared/usr/lib/sysctl.d/60-kexec-disable.conf` (Dakota
  `elements/bluefin/kexec-disable.bst`) sets `kernel.kexec_load_disabled = 1`;
  bootc upgrades and soft-reboot do not use kexec, and Easy Anti-Cheat
  requires it off.
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

## Lid and sleep

Lid and sleep handling stays at systemd-logind's defaults (suspend). Dakota
ships no logind or sleep override, and Utah's only swap is zram, so the
suspend-then-hibernate override inherited from bluefin-lts's `40-services.sh`
had no resume target and was removed from `scripts/configure-services.sh`.
