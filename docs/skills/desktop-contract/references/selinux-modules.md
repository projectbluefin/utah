# SELinux policy modules

Utah's own policy modules live under
`system_files/shared/usr/share/selinux/packages/utah/` as CIL. The desktop RUN
step in the `Containerfile` loads each one with `semodule -n -i`, before
`utah-verify-desktop-contract` runs. `-n` skips the reload, because a build
has no selinuxfs. The contract's `[selinux] modules` list is checked with
`semodule -l`. If the module is missing, or semodule is missing or can't list
modules, the build fails.

Load modules only at build time. Hummingbird keeps the module store at
`store-root=/usr/lib/selinux`, which is read-only once booted. On an installed
system, `semodule -i` and even `semodule -l` fail (#551). The store survives
updates: ostree's per-deployment `semodule -N --refresh` rebuilds from the new
image's store, and `utah-fix-home-labels --check` proves that this rebuild
leaves the home-directory rules unchanged.

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

The module is a single rule:
`(dontaudit unconfined_service_t self (capability2 (mac_admin)))`.

- **dontaudit, not allow.** bootc treats the denial as the answer and works
  without the capability. An allow would let every unconfined service write
  labels the policy doesn't know, and would change what the probe decides.
- **As narrow as the language allows.** A capability check has no target
  object except the process itself, so a domain plus one permission is the
  smallest rule. The cost: a `mac_admin` denial from any other
  `unconfined_service_t` process is hidden too.
- The kernel still logs `SELINUX_ERR op=setxattr
  invalid_context=...invalid_bootcinstall_testlabel_t` for the probe. That
  record is not an AVC, and the gate ignores it.

Pinned by `tests/test_selinux_bootc_probe.py`. The verifier side is covered
in `tests/test_desktop_contract_verify.py` (`SelinuxModuleTests`). On a booted
system, `iso/scripts/luks-e2e.sh` (the `review_vm` run) fails on any AVC
denial whose `comm` is `chcon` or `bootc`.
