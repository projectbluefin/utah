## Installed guest probes

The review VM must check statistics timer enablement after installation, then
create Common's countme opt-out marker and require the service condition to
skip execution. Remove that marker and restart the timer in a `finally` block
so the probe leaves the enabled-by-default installation intact.

Installed-guest file copies require the installed SSH port and test-user
password, using SCP's uppercase `-P`. Exercise that helper after merging
harness changes; a missing function can otherwise fail only after installation.

Flatpak `list --columns=ref` displays ID/arch/branch without app/runtime kind.
Query each kind explicitly and restore its prefix before comparing full declared
refs. Exercise the formatter with actual three-component displayed refs; the
installed VM can run Ghostty while every fully namespaced comparison fails.

## All-flavor offline ISO review

For complete offline ISO coverage, also set `review_iso_all_flavors=true`.
The disposable VM matrix comes from `config/flavors.json` through the contract
job; derive image names with `scripts/flavors.py image`, and keep manifest,
provenance and screenshots in separate per-flavor artifacts. Fail-fast must
remain disabled so one failed kernel does not hide the other flavor results.
Installed probes must match the requested flavor to image-info, require the
running gaming kernel to match its OGC release receipt, and check NVIDIA module
vermagic against the running kernel. A QEMU installation can prove these files
and kernel boot paths; it cannot prove physical NVIDIA initialization, suspend,
or Secure Boot enrollment. Keep those results explicitly separate.
