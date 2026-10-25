# Testsuite triage runbook

`post-testing-e2e.yml` runs the upstream `projectbluefin/testsuite` suites
(`smoke`, `common`, `nvidia`) against each exact built digest, pinned via the
`test_ref` input. When legs go red, the failures are usually Utah image gaps,
not harness bugs. Triage in this order; each step names where its evidence
lives.

## Where the evidence is

- Per-leg `e2e-results-<image>-<suite>/results.json` artifacts: feature and
  scenario status plus the failing step's assertion text. Start here.
- `vm-serial-log-<image>-<suite>/vm-serial.log`: the guest journal. Failed
  systemd units, `bootupctl` errors, and service restart storms show up here.
- `screenshot_smoke_failed_*` PNGs in the same artifact: what the desktop
  actually showed when a GUI step failed.
- The job log's `::error::` lines: harness-level failures (e.g. no desktop
  screenshot, exit 125 loading the runner container).

Download with `gh run download <run-id> --repo projectbluefin/utah
--pattern 'e2e-results-*' --dir <dir>`.

## Failure taxonomy

**No `results.json`, runner-container load exits 125.** The testsuite loads
its runner container into the guest with podman; on the OGC kernel this fails
with `bwrap: Creating new namespace failed` or `cannot re-exec process`
because the kernel lacks `USER_NS` (likewise `/dev/uinput` missing means
`INPUT_UINPUT` is off). Fix in `scripts/install-ogc-kernel.sh`
(`required_config` + `scripts/config` + `require_config`), covered by
`OgcKernelConfigGateTests`. Editing that script invalidates the kernel-cache
tag and forces a ~45-minute rebuild by design.

**`rpm -q` / missing binary.** The test expects an RPM Utah does not ship.
Compare `baselines/bluefin/rpms.tsv` against `baselines/utah/rpms.tsv`: if
Bluefin ships it, it is parity debt. Verify with `just check-repos`: a `No
match` means no enabled repository provides it — record under
`packages/utah.toml [unavailable]` with a tracking issue (which must name the
factory request), never silently skip. If it resolves, add it to the install
contract.

**Extension "is not enabled" (state 99/6).** Distinguish installed from
enabled: Dakota deliberately ships some bling extensions installed-but-
disabled, and the testsuite asserts both states in different features. Do not
enable an extension to satisfy the test if that diverges from Bluefin
behavior — file the inconsistency upstream (`projectbluefin/testsuite`)
instead, and keep the Utah decision recorded on the tracking issue.

**Failed systemd units at boot.** Read the unit names in the assertion, then
the serial log. Utah-specific causes seen so far: `bootupctl
adopt-and-update` failing on systemd-boot layouts (no `/boot/grub2` — guard
the hook to skip without committing), and burst-restart limiter trips on
exit-0 services during test unmask/reload storms (widen the window with a
drop-in, asserted in the desktop contract).

**Empty provider/feature lists despite shipped RPMs.** The RPM may be a
factory rebuild that omits data or backend files — verify in the published
image (`podman run ... rpm -ql <pkg>` and list the data dir), then file
against `projectbluefin/utah-packages` with the file-level evidence. No Utah
manifest change can fix a factory packaging gap.

## Scoping exclusions

When a failing scenario asserts something no enabled repository can provide
and the factory request is filed, scope it out with the upstream `extra-tags`
input (e.g. `extra-tags: "~@orca"`) on the `testsuite` call, with a comment
naming the tracking issue, the factory request, and the return condition
(remove the exclusion when the packages move into the install contract). The
input expands to a single `--tags` value, so one tag expression per call —
comma-separated negations OR together and exclude almost nothing. Pin the
scope with a unit test asserting the exact value plus its tracking
references, so the exclusion cannot silently widen or lose its return
condition. Never exclude a scenario to hide a Utah-side defect.

## Manual review-VM dispatch

If push-triggered PR builds stop firing for a branch (only `prow` runs),
dispatch explicitly: `gh workflow run build.yml --ref <branch> -f
contract_only=true -f review_vm=true -f sha=<head-sha>`. This runs the full
image build including `utah-verify-desktop-contract` plus an installed-VM
check. `review_iso_compression_level=3` with `review_iso_all_flavors=true`
measures the production ISO budget; per-flavor `evidence/iso-size.json`
lands in the artifacts whether or not the 8 GiB guard passes.
