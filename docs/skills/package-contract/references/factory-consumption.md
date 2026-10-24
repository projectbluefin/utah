# Consuming factory-published Bluefin tools

Parity packages the factory builds (`utah-packages`) reach Utah in three steps.
Nothing is requested before it resolves: a name the pinned image publishes can
still fail on a `Requires` that neither the factory nor Hummingbird carries.

1. **Find candidates.** `baselines/triage.toml` entries with status `planned`
   are the Bluefin names Utah lacks. List the RPMs in the pinned package image
   (`repository/result/*.rpm` in its payload layer) and in Hummingbird's x86_64
   primary metadata. A name in the factory is only a preflight.
2. **Resolve the real transaction.**
   `python3 scripts/check-repo-availability.py packages/bluefin.toml packages/utah.toml`
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
- Nothing in Bluefin's `[excluded]` list (`packages/bluefin.toml`) is installed.
- Hummingbird's `99-default-disable` leaves every unit off, so a service Bluefin
  runs needs a line in `85-utah-desktop.preset`, an `enable_unit` in
  `scripts/configure-services.sh` and an entry in `contracts/bluefin-desktop.toml`
  `[services].enabled`. Bluefin has no preset of its own for these: it inherits
  Fedora's `90-default.preset` from `redhat-systemd-presets`, which is where
  to read whether a unit is enabled. It enables `thermald.service`,
  `intel_lpmd.service`, `tuned.service`, `tuned-ppd.service`, `smartd.service`,
  `nfs-client.target`, `sysstat.service` and its two timers, but not
  `rpcbind`, `autofs`, `brltty`, `dnsmasq`, `saned` or `ipp-usb`. Utah wires
  `sysstat*` and disables `nfs-client.target` (Hummingbird's own preset enables
  it): it pulls in `gssproxy.service`, which failed to start in the review VM
  (`STATE_DIRECTORY`, Permission denied; cause not diagnosed). `tuned` (Requires `rpm-ostree if bootc`,
  and rpm-ostree is waived and unpublished) and `smartmontools` are not
  installable, so their units stay unwired. Bluefin ships `tuned-ppd` rather
  than `power-profiles-daemon`; never install both.
- A `planned` triage entry for a name Utah now installs closes when
  `just baselines` refreshes the Utah snapshot; do not hand-edit the generated
  snapshot.

Installed is not working. Package presence says nothing about whether a daemon
starts or does its job: thermald and intel_lpmd stand down on unsupported CPUs,
and tuned-ppd's power-profiles API is only observable from GNOME Settings on
hardware.
