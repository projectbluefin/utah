# Repository authenticity

Every allowlisted repository is attested on three axes. Its **origin** is pinned
in `[repositories.baseurls]`: `verify-rpm-contract.py` fails a build that
enables an allowlisted repository with a different `baseurl`, a `metalink`/
`mirrorlist` (which DNF merges with any `baseurl` the section declares), or no
`baseurl` at all. Its **RPM GPG key** is pinned in `[repositories.gpgkeys]`:
every `gpgkey=` value in an allowlisted section must match one of the URLs or
file paths listed there, and a `gpgkey=` with no manifest entry at all is
rejected (#617). The gate has no other way to know which key the repository
should be presenting, and an override drop-in could swap in a key the
attacker signed. `utah-packages` has no entry because its section declares
no `gpgkey=` (the bind-mounted RPM repository authenticates by OCI provenance
rather than an RPM GPG key); it is not exempt, so a drop-in that adds a
`gpgkey=` to it is rejected like any other unpinned key. Pins
normalize scheme and host case and braced DNF variables; URL paths, including trailing slashes, remain exact. Its **fetch
integrity** is attested too: the same check rejects `proxy=`, `sslverify=0`,
`gpgcheck=0` (or its libdnf5 alias `pkg_gpgcheck=0`), and `repo_gpgcheck=0`
on an allowlisted repository (#345). `proxy` and `sslverify=0` reroute or
blind the fetch and are never approved; `gpgcheck`/`repo_gpgcheck` disable RPM
signature verification and are rejected unless the repository is named in
`[repositories.security]` with the option it is approved to leave disabled
(`gpgcheck` covers both `gpgcheck` and `pkg_gpgcheck`). A repository not named
there may not explicitly disable signature verification (an omitted option
falls back to the dnf5 default and is not rejected). The same options set to a
disabled value in the resolved dnf5 `[main]` configuration are always rejected,
since they apply to every repository and no per-repository approval covers
them. A `[repositories.security]` or `[repositories.gpgkeys]` entry for a
repository not in `[repositories.allowed]` is rejected as approving nothing,
as is any `[repositories.security]` option other than `gpgcheck` or
`repo_gpgcheck`. The two documented signature-check exceptions are
`utah-packages` (RPMs are authenticated by the pinned package image and its
OCI provenance, so both signature checks are disabled) and
`nvidia-container-toolkit` (NVIDIA signs only its repomd.xml, so only package
signature verification is disabled). `[repositories.gpgkeys]` pins
`public-hummingbird-x86_64-rpms` (the local
`file:///etc/pki/rpm-gpg/RPM-GPG-KEY-redhat-release-2` path) and
`nvidia-container-toolkit` (the key URL NVIDIA publishes); `utah-packages`
declares no `gpgkey=` and has no entry.

The install-source identity is single-sourced in `packages/*.repo`. Each repository
participating in the package install transaction carries a `# utah-install: true`
annotation (either directly preceding or within the `[section]` header in
`packages/utah-packages.repo` and `packages/hummingbird.repo`).
`scripts/install-packages.py` derives the `--enablerepo` set from these annotations
ordered by priority (ascending), so rebuilds in `utah-packages` (`priority=1`)
precede base Hummingbird packages (`priority=10`). Repositories without this marker
(such as `nvidia-container-toolkit` or builder-only `fedora-44`) are excluded from
the desktop package transaction.

The pinned package image is an RPM repository, not a runtime dependency. It is
bind-mounted into the package-contract and flavor-specific install RUN steps in
[`Containerfile`](../../Containerfile), both identified by
`--mount=type=bind,from=packages,source=/repository,target=/etc/utah-packages,ro`,
and never copied into a layer: a COPY of the whole ~4 GB repository would leave
a permanent layer behind, so reproducibility now comes from the digest-pinned
`packages` stage being the only source the package transaction can see rather
than from the repository contents living in the image.

Repository override drop-ins are held to the same `gpgkey=` pin: a named
override that sets `gpgkey=` is checked against `[repositories.gpgkeys]` even
when it is partial (no origin key) or disables the repository, and a wildcard
override may not set `gpgkey=` at all, because a glob can match repositories
with different key pins.
