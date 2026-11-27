# Disposable review VM

On a reviewed verification branch, dispatch `build.yml` with `contract_only`
and `review_vm` true. Also set `review_all_flavors=true` to cover every flavor
in `config/flavors.json`; without it, the main flavor runs alone.
`review_backend` picks the medium's bootc backend (`auto`, the default:
composefs on a kernel with the btrfs fs-verity fix, else ostree). The contract
job supplies the matrix. Resolve image names with `scripts/flavors.py image`,
never a second hardcoded image list.

The `review_vm` path builds locally only when paired with `contract_only`; it
does not publish or sign an image. If a change modifies the OGC or NVIDIA
kernel-cache inputs, build the matching cache through the normal signed build
pipeline first. The review runner must consume that cache; do not weaken
signing or add a signing permission to this workflow to make the test pass.

Each leg builds the candidate with `just build-ghcr`, records its manifest and
`provenance.json`, moves it into root's storage on `/mnt`, and builds the
gnome-setup install medium from it (`iso/raw/`, the install Post-Testing E2E
gates `:testing` on, #1569): live layer, `build-raw-image.sh`, Flatpak bake,
shrink. `iso/raw/raw_e2e.py` then drives the whole install in QEMU with a
software TPM: live boot, gnome-setup GUI install into LUKS2, first boot,
gnome-initial-setup, desktop, `bootc switch` to the published `:testing`
digest of that flavor and `bootc rollback`. `candidate-common.yml` runs the
same steps on its local Common candidate before it publishes.

Evidence (`review-vm-<flavor>-<sha>`): `evidence/` (build log, manifest,
provenance, install log), every stage's screenshot, the harness's
`results.json`, and the medium's raw-evidence. The medium and disks carry test
credentials and are never uploaded.
