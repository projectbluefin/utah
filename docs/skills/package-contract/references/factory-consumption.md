# Consuming factory-published packages

Parity packages the factory builds (`utah-packages`) reach Utah in three steps.
Nothing is requested before it resolves: a name the pinned image publishes can
still fail on a `Requires` that neither the factory nor Hummingbird carries.

1. **Find candidates.** `baselines/triage.toml` entries with status `planned`
   are the Dakota items Utah lacks; map each to the Fedora package that ships
   the same files. List the RPMs in the pinned package image
   (`repository/result/*.rpm` in its payload layer) and in Hummingbird's x86_64
   primary metadata. A name in the factory is only a preflight.
2. **Resolve the real transaction.**
   `python3 scripts/check-repo-availability.py packages/base.toml packages/utah.toml`
   (about 20 seconds: it reads the pinned repodata layer, no RPM download).
   Every unresolved `Requires` is listed under its package. Drop the failing
   names, repeat, and record the missing dependency in the "Left out" comment
   of `[parity]` in `packages/utah.toml`. That comment is the single list of
   what is blocked and why; re-request a name the day its dependency lands.
3. **Wire it.** Resolved names go in `[parity]` and, because the factory
   supplies them, in `[factory].parity` so a silent base substitution fails
   the image build. Move `PACKAGE_IMAGE_SHA` and the `# factory-pin:` stamp in
   `packages/utah-packages.repo` to the same digest in one commit.

Rules applied while choosing names:

- Flatpak-first: no RPM for an app Utah ships as a Flatpak (for example
  `gnome-system-monitor` stays out because Mission Center ships). Libraries,
  thumbnailers and services that a sandboxed app cannot provide are not
  duplicates.
- Nothing in the base manifest's `[excluded]` list (`packages/base.toml`) is installed.
- Hummingbird's `99-default-disable` leaves every unit off, so a service Bluefin
  runs needs a line in `85-utah-desktop.preset`, an `enable_unit` in
  `scripts/configure-services.sh` and an entry in `contracts/bluefin-desktop.toml`
  `[services].enabled`. Bluefin has no preset of its own for these: it inherits
  Fedora's `90-default.preset` from `redhat-systemd-presets`, which is where
  to read whether a unit is enabled. It enables `thermald.service`,
  `intel_lpmd.service`, `tuned.service`, `tuned-ppd.service`, `smartd.service`,
  `nfs-client.target`, `sysstat.service` and its two timers, but not
  `rpcbind`, `autofs`, `brltty`, `dnsmasq`, `saned` or `ipp-usb`. Utah follows
  Dakota rather than Fedora on `sysstat*`: collection is opt-in
  (`elements/bluefin/sysstat.bst` disables `sysstat.service` and its three
  timers), so the preset disables them and the contract's `disabled` list holds
  them off. Utah also disables `nfs-client.target` (Hummingbird's own preset enables
  it): it pulls in `gssproxy.service`, which failed to start in the review VM
  (`STATE_DIRECTORY`, Permission denied; cause not diagnosed).
  `smartmontools` is not installable, so its unit stays unwired.
- Power profiles follow Dakota, not Bluefin: Utah installs and enables
  `power-profiles-daemon` and ships no TuneD. Never install `tuned-ppd`
  beside it: both claim `org.freedesktop.UPower.PowerProfiles`. TuneD also
  called `bootc loader-entries` on every profile switch and hung on the
  ostree sysroot lock, which needed a post-loaded profile workaround; PPD has
  no bootloader plugin, so that workaround is gone.
- A `planned` triage entry for an item Utah now ships closes when
  `just baselines` refreshes the Utah snapshot; do not hand-edit the generated
  snapshot.

Installed is not working. Package presence says nothing about whether a daemon
starts or does its job: thermald stands down on unsupported CPUs, and
power-profiles-daemon's API is only observable from GNOME Settings (or
`powerprofilesctl`) on hardware.

## Closing baseline gaps

After refreshing image snapshots, run `python3 scripts/image-baseline.py check`
and remove each entry it reports as "no longer gaps"
from `baselines/triage.toml`. Those notices are advisory, so a passing
check does not mean the triage cleanup is complete. If the refresh is
still in an open PR, validate the cleanup with its snapshots and land
the refresh first; the old snapshots still require those triage entries.
