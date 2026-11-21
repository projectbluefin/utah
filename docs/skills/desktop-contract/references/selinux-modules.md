# SELinux policy modules

Utah's own policy modules live under
`system_files/shared/usr/share/selinux/packages/utah/` as CIL. The desktop RUN
step in the `Containerfile` loads each one with `semodule -n -i`, before
`utah-verify-desktop-contract` runs. `-n` skips the reload, because a build
has no selinuxfs. The contract's `[selinux] modules` list is checked with
`semodule -l`. If the module is missing, or semodule is missing or can't list
modules, the build fails.

Ship image modules from the build. `utah-relocate-selinux-store` moves the
store from Hummingbird's `/usr/lib/selinux` to `store-root=/etc/selinux` (#551),
so users can also add local modules on a booted system, but only the build's
modules are part of the image. On update, ostree's per-deployment
`semodule -N --refresh` rebuilds from the merged `/etc` store, and
`utah-fix-home-labels --check` proves that this rebuild leaves the
home-directory rules unchanged.

## utah_bootc_probe (#1198)

`bootc status` probes for `mac_admin` by running `chcon` with a label the
policy doesn't define (bootc `lsm.rs`, `selinux_ensure_install`). Outside
`install_t`, the probe is denied. bootc re-executes once, probes again, and
then carries on. When bootc runs from a plain service, the source domain is
`unconfined_service_t` and each boot logs two denials:

```
avc: denied { mac_admin } comm="chcon" capability=33
     scontext=system_u:system_r:unconfined_service_t:s0 tclass=capability2
```

The testsuite's SELinux gate counts these. Common's
`projectbluefin-countme` (`bootc status --json`, as root) was confirmed as the
boot-time source: with CountMe skipped, the review VM logged none (#1198,
#1247). Running bootc as a unit's bare `ExecStart` doesn't change the domain:
#1220's VM still logged both denials.

A unit whose `ExecStart` is a shell (`/bin/sh <script>`) runs in `initrc_t`
instead (`type_transition init_t shell_exec_t:process initrc_t`), and bootc
called from it logs the same two denials in `initrc_t`. bootc-installer
v2026.11.16 writes one such unit into every install:
`/etc/systemd/system/fisherman-boot-receipt.service`
(`ExecStart=/bin/sh /etc/fisherman/boot-receipt.sh`) runs `bootc status` once,
on the first boot. Every flavor of review run 44663789174 failed the gate on it
(#1129).

The module also carries a third rule for #1329:
`(dontaudit systemd_nsresourced_t self (capability (dac_read_search)))`.
systemd-nsresourced logs `{ dac_read_search } comm=systemd-nsresou` at boot,
carries on without the capability, and the gate counted it and failed the utah and
utah-nvidia flavors of Post-Testing E2E run 44791358885 (the gaming flavors passed). The two rules below are the bootc probe.

The module is two rules for the probe, one per domain:
`(dontaudit unconfined_service_t self (capability2 (mac_admin)))` and
`(dontaudit initrc_t self (capability2 (mac_admin)))`.

- **dontaudit, not allow.** bootc treats the denial as the answer and works
  without the capability. An allow would let every such service write
  labels the policy doesn't know, and would change what the probe decides.
- **As narrow as the language allows.** A capability check has no target
  object except the process itself, so a domain plus one permission is the
  smallest rule. The cost: a `mac_admin` denial from any other
  `unconfined_service_t` or `initrc_t` process is hidden too.
- The kernel still logs `SELINUX_ERR op=setxattr
  invalid_context=...invalid_bootcinstall_testlabel_t` for the probe. That
  record is not an AVC, and the gate ignores it.

Pinned by `tests/test_selinux_bootc_probe.py`. The verifier side is covered
in `tests/test_desktop_contract_verify.py` (`SelinuxModuleTests`). On a booted
system, `iso/scripts/luks-e2e.sh` (the `review_vm` run) fails on any AVC
denial whose `comm` is `chcon` or `bootc`.
