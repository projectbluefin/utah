---
name: lifecycle-e2e
version: "1.0"
last_updated: "2026-10-29"
id: lifecycle-e2e
one_line_purpose: Run the QEMU lifecycle harness and validate bootc upgrade, rollback, and BLS boot files.
entry_point: docs/skills/lifecycle-e2e.md
category: testing
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [qemu, bootc, lifecycle, iso, e2e]
description: >-
  Atomic bootc lifecycle transitions between two immutable Utah digests in QEMU:
  baseline, staging, reboot, and rollback. BLS boot-file and boot-manager
  checks, verify-boot-files.sh, uupd versus bootc staging, and the e2e harness.
metadata:
  type: runbook
---

# Lifecycle e2e

## Lifecycle upgrade and rollback testing

`just lifecycle-test <candidate-target-image> [disk-or-iso] [baseline-image]` runs
`iso/scripts/lifecycle-e2e.sh` and `scripts/bootc_lifecycle.py` to validate
atomic lifecycle transitions between two immutable Utah digests in QEMU:
baseline deployment verification, staging with atomic staging invariants
preserved, reboot into the candidate deployment with graphical desktop
verification, rollback execution, and reboot verification returning to the
baseline digest.
Phase-keyed diagnostics (`evidence/lifecycle-*.json`, `lifecycle-summary.json`)
and screendumps identify the active deployment and digest at every phase.

After each lifecycle phase the harness also captures the BLS Type #1
entries under the Fedora/bootc layout's BLS root `/boot/loader/entries/`
(ostree writes them here per the sysroot deploy) and any
`/boot/efi/loader/entries/` if present, then runs `validate-bootmgr`
against them.
This is the surface that an `ostree-finalize-staged` regression could
silently leave behind: `bootc status` would still report the new
deployment as queued, but the boot manager would have no entry to chain
to it and the next reboot would boot the old kernel set. The validator
parses the `ostree=/ostree/boot.N/<stateroot>/<bootcsum>/<serial>` path
from each BLS entry's `options` line, groups both the expected
deployments and the captured entries by their `(stateroot,
deploy_serial)` tuple, and requires the count of entries in each group
to be at least the count of deployments. A purely-by-serial match would
let one BLS entry satisfy two deployments (ostree allocates
`deployserial` per `(osname, commit)`, so two commits with no prior
deployment at that commit both receive serial 0) and silently miss a
missing-entry regression; the count check forces a failure in that
case. The `<bootcsum>` segment in the path is the kernel+initramfs
layout hash (ostree's `ostree_deployment_get_bootcsum`), NOT the
commit checksum the deployment exposes as `ostree.checksum`, and is
intentionally not used as a match key -- bootc's `BootEntryOstree`
JSON does not expose it. Phase 3 (post-upgrade) and phase 4
(post-rollback) both check the `booted` and `rollback` slots so a
missing entry for the non-booted slot is caught even though the guest
necessarily booted from the `booted` entry that already exists. The
validator then confirms each matched entry carries a `linux` line and
at least one of `initrd` or `options`. Evidence is written to
`evidence/loader-entries-<phase>.txt` (the raw BLS listing) and
`evidence/bootmgr-<phase>.json` (which slot matched which entry,
which slots had no entry, and any entries whose backend kernel argument did
not parse).
For composefs-backed deployments, the match key is `composefs.verity` from
`bootc status`, paired with the digest in `composefs=<hex>`,
`composefs=?<hex>`, or `composefs.digest=v<N>-sha512-12:<hex>`. Backend-qualified
keys keep OSTree and composefs entries distinct; both require enough entries
to assign a unique one to every expected deployment.

Supporting these entries does not change the installer default. The composefs
default proposal is tracked separately in #882 / PR #938. Synthetic fixtures
prove validator behavior, not emitted bootc formats or successful installation,
upgrade, and rollback; retain actual status, BLS, and boot evidence for those
claims.

Each phase also runs `iso/scripts/verify-boot-files.sh` as root in the guest
and saves `evidence/boot-files-<phase>.txt`. For every published bootc BLS
Type #1 entry (OSTree or composefs), `linux` and every repeated `initrd`
directive must reference a nonempty regular file on the **same ESP/XBOOTLDR
filesystem as the entry**.
A BLS `/ostree/...` path is partition-relative, not relative to the guest's
`/` or the harness host; do not satisfy it from a different mounted partition.
Missing directives, missing files, and an empty entry set fail the phase.
The staged phase checks only already published entries: the pending deployment's
entry/files are published during shutdown finalization and checked after reboot.
This file gate complements deployment-to-entry matching (issue #361 / PR #571);
it does not itself prove which deployment an entry represents. Fixture tests
run the same guest checker via `python3 -m unittest discover -s tests -p test_boot_files.py`.

The baseline image (default `ghcr.io/projectbluefin/utah:testing`, the target
ref `just iso testing` builds with) is the ref a live ISO installs its offline
payload under, and phase 1 fails unless the booted deployment tracks it. The
candidate is what gets staged, so it has no default: with the `bootc` policy it
must be a different ref from the baseline, since `bootc switch` to the ref
already booted stages nothing. The staged slot is checked against that
candidate, not against itself: it must come from the candidate's repository,
and a digest-pinned candidate must stage exactly that digest.

The default `bootc` policy stages the candidate with `bootc switch`, so the
candidate may come from any repository. `UTAH_LIFECYCLE_POLICY=uupd` instead
runs the shipped `uupd.service` in the guest, which is the unit the production
timer triggers. uupd follows the image reference the booted deployment already
tracks, so that policy requires a candidate in the same repository as the
booted deployment and fails closed rather than falling back to `bootc switch`.
Passing a live ISO instead of an installed disk runs the LUKS install harness
first; `UTAH_E2E_WORK` overrides where that install phase writes its disk.
The harness drives the guest over SSH as the `utahtest` password account the
installer provisions, so it defaults to the debug ISO (`just iso testing 1`).
The disk from `just generate-bootable-image` has no such account and cannot be
used directly. An installed disk may be passed instead when it carries that
account; its image format is detected before the overlay is created, so raw
and qcow2 disks both work.

All privileged guest steps feed the test password to `sudo -S` over SSH;
membership in `wheel` alone does not allow passwordless, non-tty sudo. Clean
reboots use `systemctl reboot --no-block`. A non-transport sudo/reboot failure
fails immediately; SSH exit 255 is accepted only with fresh serial shutdown
evidence and an observed disconnected SSH session. A successful request also
requires SSH to go down before the next boot gates run. Unlock, graphical
login and digest checks then prove the new deployment actually booted; never
hard-reset a staged deployment. QEMU runs without `-no-reboot` and uses its
normal guest-reboot reset action, retaining the same disk and firmware state.
Every desktop milestone also requires `/etc/os-release` to identify
`ID=hummingbird`, `NAME=Bluefin`, and `PRETTY_NAME=Bluefin (Version: ...)`, matching
the desktop contract. A readable os-release file is not identity proof.

After each baseline, upgraded and rollback boot, the harness enters the
test account through GDM with QEMU monitor key events, using the same
single-account greeter/password flow as `luks-e2e.sh`, and waits for that
user's GNOME Shell before taking desktop evidence. An SSH connection or
active GDM service alone does not establish a graphical user session.
The disposable disk must offer the test account selected at the greeter;
this does not enable autologin or alter the published image's login policy.

```bash
just lifecycle-test ghcr.io/projectbluefin/utah@sha256:<candidate-digest>
```

```bash
just check
```

Then the manual runbook, when the change touches the boot path:

```bash
just build-ghcr utah testing main
just generate-bootable-image testing
just boot-vm     # success: GDM appears and GNOME Shell renders in noVNC
just iso testing
just boot-iso    # success: live session renders; serial shows UTAH_LIVE_READY
```

See the [disposable ISO review matrix](local-testing/references/iso-review.md)
for read-only, all-flavor offline installation tests on KVM runners.
