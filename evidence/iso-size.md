# ISO size experiments (#875)

## Summary

Utah's tacklebox live ISO sits at 6.79 GiB, over the 6 GiB guard. PR #876 raised
the guard to 8 GiB as a stopgap; this work is the actual investigation the issue
asked for. The measurement harness `iso/scripts/measure-iso-store.sh` assembles
the live rootfs the same way `build-iso.sh` does — embed the offline container
store, squashfs it — but measures only the squashfs root under a matrix of
storage-driver, compression, block-size and dedupe strategies, and writes the
table to `evidence/iso-size.json`. The table is empty pending the first run: the
CI runner that owns real podman/skopeo/mksquashfs numbers has not executed it
yet, and it cannot be run against a 6 GiB+ image on a normal workstation.

---

## Why the squashfs root is the number that matters

The live ISO is a squashfs root plus a small ESP/boot overlay (roughly 150-300
MiB of firmware, kernel, initramfs and GRUB). The offline container store is
embedded *inside* that squashfs, so it is the single dominant term in the total
size. Shrinking anything else shrinks the ISO by the same fraction; shrinking
the root is where the bytes actually are. The harness therefore measures the
squashfs root directly, which is what `build-iso.sh` feeds into the 8 GiB budget
guard.

The full-ISO byte counts from post-testing E2E and diagnostic runs are recorded
in the `full_iso_reference` block of `evidence/iso-size.json` and in
`docs/skills/ci-workflows/references/iso-e2e.md`; they are a different metric and
are kept out of the strategy table so the two are never confused.

## Method

`measure-iso-store.sh` runs, per flavor, one assembly per strategy:

1. `podman image mount` the live image into a temp dir.
2. `cp -a` the mounted tree into a fresh graphroot.
3. Write a `storage.conf` with the strategy's `graphdriver` (`overlay` or
   `vfs`) and run `podman run` with `--graph` pointed at it, so the offline
   store is unpacked into that driver's store exactly as `build-iso.sh` does.
4. `skopeo copy` the offline store into the graphroot.
5. `mksquashfs` the graphroot with the strategy's compression and block size.
6. `du` the squashfs and the uncompressed root; emit one JSON row.

A hardlink dedupe row additionally scans the squashfs root and the embedded
store for identical files and hardlinks the store copies onto the root copies,
which is the one strategy where the two copies overlap.

## Strategy matrix

| id            | driver  | comp_level | block_size | dedupe     | what it tests                                    |
|---------------|---------|------------|------------|------------|--------------------------------------------------|
| baseline      | overlay | 3          | 131072     | none       | current production settings                      |
| vfs-store     | vfs     | 3          | 131072     | none       | experiment 1: vfs vs overlay store driver        |
| zstd-7        | overlay | 7          | 131072     | none       | experiment 3: mid zstd level                       |
| zstd-11       | overlay | 11         | 131072     | none       | experiment 3: higher zstd level                    |
| zstd-15       | overlay | 15         | 131072     | none       | experiment 3: maximum zstd level                   |
| block-1m      | overlay | 3          | 1048576    | none       | experiment 3: 1 MiB squashfs block size          |
| hardlink-dedupe | overlay | 3        | 131072     | hardlink   | experiment 2: dedupe between rootfs and store    |

## Results

Filled in by the first `just measure-iso` run. Squashfs-root bytes; divide by
`1024**3` for GiB.

| flavor         | strategy      | squashfs_bytes | rootfs_uncompressed_bytes |
|----------------|---------------|----------------|---------------------------|
| main           | baseline      | _pending_      | _pending_                 |
| main           | vfs-store     | _pending_      | _pending_                 |
| main           | zstd-7        | _pending_      | _pending_                 |
| main           | zstd-11       | _pending_      | _pending_                 |
| main           | zstd-15       | _pending_      | _pending_                 |
| main           | block-1m      | _pending_      | _pending_                 |
| main           | hardlink-dedupe | _pending_    | _pending_                 |
| nvidia-gaming  | baseline      | _pending_      | _pending_                 |
| nvidia-gaming  | vfs-store     | _pending_      | _pending_                 |
| nvidia-gaming  | zstd-7        | _pending_      | _pending_                 |
| nvidia-gaming  | zstd-11       | _pending_      | _pending_                 |
| nvidia-gaming  | zstd-15       | _pending_      | _pending_                 |
| nvidia-gaming  | block-1m      | _pending_      | _pending_                 |
| nvidia-gaming  | hardlink-dedupe | _pending_    | _pending_                 |

## Environment note

The harness needs podman (overlay or vfs graphdriver), skopeo and mksquashfs,
and a working graphroot for each flavor's image. The numbers above are pending
because the environment that owns those tools has not run the harness yet, and a
real 6 GiB+ assembly is not feasible on a normal workstation disk. The table is
populated by running `just measure-iso` (optionally with `NAME=ghcr.io/...`
image overrides) on a runner that can build the flavors, then committing the
updated `evidence/iso-size.json`. The committed `strategies` array stays empty
until then, so `just check`'s `json.tool` gate passes against the schema now.
