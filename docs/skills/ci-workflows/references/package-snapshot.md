# Package snapshot proof

`.github/workflows/package-snapshot.yml` runs `ci/package-snapshot.py` on
manual dispatch or changes to its own implementation and tests. It is an
evaluation lane, not a production build or required check. It does not publish
an image, alter the Containerfile, or replace the normal package transaction.

The factory repository is already frozen by its OCI digest. Hummingbird's
repository is mutable. This proof installs the real transaction on the pinned
base with `keepcache=True`, seals the downloaded Hummingbird RPMs and generated
repodata with SHA-256, then replays on a separate fresh base with
`buildah run --network=none`. The factory remains mounted from its same pinned
OCI image. The complete RPM NEVRA inventory must match the online reference,
all runtime and extension-build requests must be present, and GNOME major
versions must match `packages/utah.toml`. Missing packages or mismatched
inventories fail the lane.

Use `install-packages.py`'s `contract`, `section`, and `install_repos` functions
instead of recursively collecting every TOML `packages` list: that would ask
for excluded, unavailable, multimedia replacement, and other Fedora releases'
packages. Read the Fedora major from the pinned base rather than assuming 44.
Keep the original repository priorities, factory GRUB exclusions, and
Hummingbird signature verification during replay. Factory RPMs can be consumed
directly from `file://` without appearing in DNF's cache; their existing digest
pin is the immutable input, not a reason to silently omit them from replay.

Use rootful Buildah's container filesystem and mount API for this proof.
Manually extracting OCI tar layers mishandles whiteouts, permissions, and
absolute symlinks. In particular, rewriting an absolute symlink relative to a
host extraction directory changes its container meaning. The engine already
implements these filesystem semantics.

Evidence artifacts contain the lock and proof JSON only, with seven-day
retention. They record the source commit, pinned base and factory images,
requested names, RPM hashes, reference inventory, and replay verdict. RPMs and
containers are disposable runner data. A later production integration would
need durable snapshot distribution and renewal policy; this lane only proves
that freezing the mutable dependency transaction can preserve the package set.
