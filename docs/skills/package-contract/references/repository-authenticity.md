# Repository authenticity (gpgkey=)

The `[repositories.gpgkeys]` section pins the RPM GPG key URLs each allowlisted
repository may declare, and the `scripts/verify-rpm-contract.py` gate refuses
`gpgkey=` outside that set. The policy is summarised in
[`../../package-contract.md`](../../package-contract.md); this reference is the operator-facing detail.

## Why a third axis

A repository's `baseurl=` reroutes the package fetch; its `gpgkey=` reroutes
the trust anchor. Two failure modes are pre-existing:

- A drop-in `[nvidia-container-toolkit]\ngpgkey=https://attacker/key` on an
  allowlisted id fetches the key from an attacker-controlled URL, and any
  signature that URL serves is accepted.
- A `[*]\ngpgkey=…` wildcard override would rewrite the trust anchor of every
  matching repo with no way to enumerate the matches.

Before the gate, `repo_security_option_errors` checked `proxy=`, `sslverify=`,
`gpgcheck=`, `pkg_gpgcheck=`, and `repo_gpgcheck=` but not `gpgkey=`. The same
gap existed for both the reposdir scan and the override-dir scan (#617).

## What the manifest declares

```toml
[repositories.gpgkeys]
public-hummingbird-x86_64-rpms = ["file:///etc/pki/rpm-gpg/RPM-GPG-KEY-redhat-release-2"]
nvidia-container-toolkit = ["https://nvidia.github.io/libnvidia-container/gpgkey"]
```

- The list is **what each allowlisted `.repo` actually declares**, not what
  could plausibly work. `packages/hummingbird.repo` ships the file path; the
  URL is not referenced by anything in the tree and is not pinned here.
- `utah-packages` is absent because `packages/utah-packages.repo` declares no
  `gpgkey=`. The OCI pin (`PACKAGE_IMAGE_SHA` in the Containerfile plus the
  `# factory-pin:` stamp in `packages/utah-packages.repo`) is the trust anchor;
  adding a `gpgkey=` to it would be rejected by the same check.
- A `[repositories.gpgkeys]` entry for a repository not in
  `[repositories.allowed]` is rejected at parse time, mirroring
  `[repositories.security]`. The empty list is also rejected -- the absence
  is the prohibition, not a default.

## What the gate checks

`scripts/verify-rpm-contract.py` exposes three functions that participate in
the check:

- `normalize_gpgkey(url)` -- lowercase the scheme and host, fold `${var}` to
  `$var` (the dnf5 expansion), and keep the path exact including trailing
  slashes. A trailing slash distinguishes a key directory from a key file in
  a GPG keyring; the path is part of the pin.
- `split_gpgkeys(raw)` -- split the dnf5 `gpgkey=` value on whitespace and
  commas, the same way dnf5 accepts multiple keys per section.
- `repo_gpgkey_pin_errors(...)` -- returns errors for any unpinned `gpgkey=`.
  The check is opt-in via the `expected_gpgkeys` parameter; passing `None`
  skips the check entirely, matching the existing skip semantics for
  `expected_baseurls`.

The check is reached from `check_repo_sections` **before** every `continue`
branch, so a partial override (`[id]\npriority=1\ngpgkey=https://attacker/key`)
that would otherwise pass through the `enabled` check is rejected. A
wildcard override (`[*]\ngpgkey=…`) is rejected once, by
`glob_override_errors`, since the gate cannot enumerate the matches. A
disabled repo with `gpgkey=` is also rejected -- allowlisted or not; the
trust-anchor check is not gated on `enabled=` or allowlist membership, so a
base-image `.repo` that ships `gpgkey=` for an id with no
`[repositories.gpgkeys]` entry fails the build.

The check fires at three call sites in `main()`:

- `--check` on the source `packages/` tree (`verify_repository_policy` against
  the manifest's directory).
- The on-image `reposdir=` scan against `/etc/yum.repos.d`,
  `/etc/distro.repos.d`, `/usr/share/dnf5/repos.d` (whatever dnf5 resolves at
  runtime, #454, #513, #536).
- The on-image override-dir scan against `/etc/dnf/repos.override.d` and
  `/usr/share/dnf5/repos.override.d` (#524).

## Override semantics

A drop-in section may not set `gpgkey=` on any id, even when the id is
allowlisted. The reason is asymmetry: dnf5 merges the drop-in onto the
underlying repo's `gpgkey=` value (or, in dnf5's glob case, applies the
override to every matching id), so the gate cannot tell which keys the
underlying repo originally shipped. Pinning at the manifest level is the
only path that survives the dnf5 merge.

A wildcard override (`[*]`, `[utah-*]`) is rejected for `gpgkey=` before
the glob match, mirroring how it is rejected for `enabled=1`, `proxy=`, and
signature checks. No `[repositories.security]` or `[repositories.gpgkeys]`
approval applies to a glob: the gate cannot tell which repo the glob
matches, so it cannot tell which approval to look up.

## Test coverage

`tests/test_verify_rpm_contract.py` covers:

- `normalize_gpgkey`: scheme/host lowercase, `${var}` → `$var` fold, trailing
  slash preserved, `file://` path preserved.
- `repo_gpgkey_pin_errors`: pass when `gpgkey=` is unset, pass when set to a
  pinned URL, pass on a case-insensitive match, fail on an unpinned URL on
  an allowlisted id, fail when the id has no `[repositories.gpgkeys]` entry,
  fail on an override section (pinned or not), pass when `expected_gpgkeys`
  is `None` (the opt-in skip).
- `check_repo_sections`: fail on an allowlisted repo with unpinned `gpgkey=`,
  fail on a disabled allowlisted repo with `gpgkey=`, fail on a partial
  override with `gpgkey=`, fail on a wildcard override with `gpgkey=`, pass
  when `gpgkey=` matches the pin, pass when `expected_gpgkeys` is `None`.
- Manifest parsing (`--check`): fail on a `[repositories.gpgkeys]` entry for
  a non-allowlisted repo, fail on a non-table value, fail on an empty list,
  fail on a non-string entry, fail on an unpinned `gpgkey=` in a repo file,
  pass on a pinned `gpgkey=`.
