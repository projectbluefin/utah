repo_organization := env_var_or_default("REPO_ORGANIZATION", "projectbluefin")
image := "utah"
kernel_cache_image := "utah-kernel-cache"
base_dir := env_var_or_default("BASE_DIR", "output")
vm_ram := env_var_or_default("VM_RAM", "8192")
vm_cpus := env_var_or_default("VM_CPUS", "4")

default:
    @just --list

# Host-side unit tests for the helper scripts under scripts/. No image is
# needed, so they run inside `just check` rather than waiting for a build to
# fail on a wrong matrix.
#
# Discovery is delegated to tests/run_suite.py, which runs every directory
# under tests/ that holds test modules. Bare `unittest discover` rooted at
# tests/ skipped subdirectories such as tests/unit/ silently -- it reported
# OK whether the tests there passed, failed, or never ran.
test:
    #!/usr/bin/env bash
    set -euo pipefail
    pip install --quiet pyyaml 2>/dev/null || true
    python3 tests/run_suite.py

check:
    #!/usr/bin/env bash
    set -euo pipefail
    # Every tracked shell and Python script is syntax-checked by enumeration,
    # not by a list maintained here by hand. That list had fallen six scripts
    # behind the tree -- including scripts/resolve-e2e-inputs.py, which decides
    # the E2E matrix, and iso/scripts/live-kernel.py, which runs mid-ISO-build
    # -- so a parse error in them surfaced minutes into a build instead of here.
    python3 scripts/check-script-syntax.py
    test -f Containerfile
    test -f packages/bluefin.toml
    test -f packages/.bluefin-parity-ref
    test -f packages/utah.toml
    test -f packages/utah-packages.repo
    test -f system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset
    test -f system_files/shared/usr/lib/systemd/system/bootc-unified-storage.service.d/10-utah-local-test.conf
    grep -q 'enable gdm.service' system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset
    grep -q 'enable ublue-system-setup.service' system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset
    grep -q 'disable bootc-fetch-apply-updates.timer' system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset
    grep -q 'disable bootc-fetch-apply-updates.service' system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset
    grep -q 'bootc-fetch-apply-updates.timer' scripts/configure-services.sh
    # The serial getty is a Hummingbird server leftover that spams the journal
    # on machines without a serial port; the mask and the preset must both
    # survive, and the contract has to declare the mask. See #103.
    grep -q 'serial-getty@ttyS0.service' system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset
    grep -q 'systemctl mask serial-getty@ttyS0.service' scripts/configure-services.sh
    grep -q 'ln -sf /dev/null /usr/lib/systemd/system/serial-getty@ttyS0.service' scripts/configure-services.sh
    grep -q 'serial-getty@ttyS0.service' contracts/bluefin-desktop.toml
    test -f scripts/configure-services.sh
    test -f scripts/configure-branding.sh
    test -f scripts/verify-desktop-contract.py
    test -f scripts/verify-gnome-extensions.py
    test -f scripts/mirror-shim.sh
    test -f contracts/bluefin-desktop.toml
    # The reusable image workflow checks out this repository without
    # submodules. Populate them here before validating the source contract;
    # otherwise CI reports every extension as missing while a developer clone
    # (or the contract workflow's recursive checkout) passes.
    git submodule update --init --recursive
    python3 scripts/verify-desktop-contract.py --check contracts/bluefin-desktop.toml
    python3 scripts/verify-gnome-extensions.py --source
    grep -q '/system_files/bluefin' Containerfile
    grep -q 'flatpak-preinstall.service' scripts/configure-services.sh
    grep -q 'flathub.flatpakrepo' scripts/configure-services.sh
    test -f iso/live/Containerfile
    test -f iso/live/src/configure-live.sh
    test -f iso/live/src/install-flatpaks.sh
    test -f iso/live/src/etc/bootc-installer/images.json
    test -f iso/live/src/etc/bootc-installer/recipe.json
    test -f iso/scripts/build-iso.sh
    python3 -m json.tool iso/live/src/etc/bootc-installer/images.json >/dev/null
    python3 -m json.tool iso/live/src/etc/bootc-installer/recipe.json >/dev/null
    grep -q 'org.bootcinstaller.Installer' iso/live/src/install-flatpaks.sh
    grep -q 'containers-storage' iso/scripts/build-iso.sh
    grep -q 'UTAH_LIVE' iso/scripts/build-iso.sh
    grep -q 'Documented exception (Issue #22)' iso/scripts/build-iso.sh
    if grep -q 'rd.utah.isofile' iso/scripts/build-iso.sh; then echo "rd.utah.isofile found in build-iso.sh" >&2; exit 1; fi
    if grep -q 'loopback.cfg' iso/scripts/build-iso.sh; then echo "loopback.cfg found in build-iso.sh" >&2; exit 1; fi
    grep -q 'ENABLE_SSHD' Containerfile
    grep -q 'ENABLE_SSHD="${ENABLE_SSHD:-0}"' Justfile
    grep -q 'ARG PACKAGE_IMAGE_SHA=' Containerfile
    grep -q 'ARG PACKAGE_IMAGE_REF=' Containerfile
    grep -q -- '--mount=type=bind,from=packages,source=/repository,target=/etc/utah-packages,ro' Containerfile
    # The package repository is bind mounted, never committed; a COPY would ship
    # the whole ~4 GB RPM repository in every image and of every ISO (#128).
    # `! cmd` is exempt from errexit, so a bare `! grep` neither stops this
    # script nor changes its exit status unless it happens to be the last line
    # of the recipe: it reads as a gate and enforces nothing. Use an explicit if.
    if grep -q 'COPY --from=packages' Containerfile; then
      echo 'Containerfile must bind-mount the package repository, not COPY it' >&2
      exit 1
    fi
    test -f packages/RPM-GPG-KEY-redhat-release-2
    # Every executable release asset fetched during composition must be pinned
    # and verified; no build may resolve a mutable latest release.
    python3 scripts/check-download-integrity.py
    # The status page's package grid is generated from the manifests; a stale
    # committed copy would publish a list the image no longer installs.
    python3 scripts/generate-site-data.py --check
    # README.md and docs/skills/package-contract.md quote the same package
    # counts by hand; catch it before the manifests move and the prose does not.
    python3 scripts/check-doc-counts.py
    python3 scripts/install-packages.py --check --repos-dir packages packages/bluefin.toml
    python3 scripts/verify-rpm-contract.py --check packages/bluefin.toml
    # run the host-side unit suite (tests/test_*.py) via its dedicated recipe
    just test
    grep -qE 'reusable-build\.yml@(v1|[0-9a-f]{40} # v1)$' .github/workflows/build.yml
    test -f Containerfile.kernel
    # The cache image must be built from the same base the image is, or the
    # prebuilt NVIDIA module would be linked against a kernel this image never
    # boots.  Two literals, one invariant, so assert it rather than trust it.
    diff <(grep -m1 '^ARG BASE_IMAGE=' Containerfile) \
         <(grep -m1 '^ARG BASE_IMAGE=' Containerfile.kernel)
    python3 scripts/flavors.py list >/dev/null
    python3 scripts/check_workflow_outputs.py
    pip install --quiet jsonschema 2>/dev/null || true
    bash scripts/check-skill-frontmatter.sh
    bash scripts/check-skill-index.sh
    python3 scripts/generate_skill_index.py --check
    # Neither workflows nor the Justfile may carry their own copy of the flavor
    # list or flavored image names. That drift is what config/flavors.json and
    # scripts/flavors.py exist to stop: narrowing the build matrix while
    # promote and release still name images nothing produces fails late.
    if grep -rnE 'utah-(nvidia|gaming)' .github/workflows/; then
      echo 'no workflow may name a flavored image; read it from config/flavors.json' >&2
      exit 1
    fi
    if grep -nE '(utah|\{\{ image \}\})-(nvidia|gaming)' Justfile; then
      echo 'no recipe may name a flavored image; use flavors.py image' >&2
      exit 1
    fi

# Verify branding, desktop defaults, first-boot Flatpak policy, and service
# enablement in an already-composed image. The same verifier runs in the
# Containerfile, so this is useful for a local image or a CI artifact.
check-desktop-contract image_ref="localhost/utah:testing":
    #!/usr/bin/env bash
    set -euo pipefail
    podman run --rm --entrypoint /usr/bin/python3 \
      -v "$PWD/contracts/bluefin-desktop.toml:/tmp/bluefin-desktop.toml:ro" \
      -v "$PWD/scripts/verify-desktop-contract.py:/tmp/verify-desktop-contract.py:ro" \
      "{{ image_ref }}" /tmp/verify-desktop-contract.py /tmp/bluefin-desktop.toml
    podman run --rm --entrypoint /usr/bin/python3 \
      "{{ image_ref }}" /usr/local/libexec/utah-verify-gnome-extensions

# Fail fast when a contract package is in none of the repositories the image
# actually enables, instead of discovering it twenty minutes into a build.
#
# The pinned base image is pulled from a remote registry here, so a CDN that
# drops a blob mid-read fails this gate before a single package is evaluated.
# Exit 125 is the container engine refusing to run the container at all, which
# is that failure and not a verdict about the package set. The flavor builds
# already retry their registry work, and this gate gates them, so a transient
# here is strictly more expensive than one there. Retry 125 and nothing else:
# a real resolution failure must stay immediate and loud rather than paying for
# three slow dnf resolves to reach the same answer.
#
# Resolves dependencies on the pinned base and package image. Needs podman and network.
check-repos:
    #!/usr/bin/env bash
    set -uo pipefail
    for attempt in 1 2 3; do
      python3 scripts/check-repo-availability.py packages/bluefin.toml packages/utah.toml
      status=$?
      if [ "$status" -ne 125 ]; then
        exit "$status"
      fi
      echo "check-repos: container engine could not run (exit 125), attempt ${attempt}/3" >&2
      if [ "$attempt" -ne 3 ]; then
        sleep $(( attempt * 15 ))
      fi
    done
    echo "check-repos: giving up after 3 engine failures; the registry is not serving the pinned image" >&2
    exit 125

# packages/bluefin.toml is a verbatim copy of Bluefin's base.toml pinned to
# the revision in packages/.bluefin-parity-ref.  Drift here is a parity bug,
# so make it loud rather than letting it accumulate quietly.
check-parity:
    #!/usr/bin/env bash
    set -euo pipefail
    ref=$(tr -d '[:space:]' < packages/.bluefin-parity-ref)
    if [[ ! "$ref" =~ ^[0-9a-f]{40}$ ]]; then
      echo "packages/.bluefin-parity-ref must contain a full 40-character commit SHA" >&2
      exit 1
    fi
    upstream=$(mktemp)
    trap 'rm -f "$upstream"' EXIT
    curl -fsSL -o "$upstream" \
      "https://raw.githubusercontent.com/projectbluefin/bluefin/${ref}/build_files/packages/base.toml"
    if diff -u "$upstream" packages/bluefin.toml; then
      echo "packages/bluefin.toml matches projectbluefin/bluefin@${ref}"
    else
      echo "packages/bluefin.toml has drifted from projectbluefin/bluefin@${ref}" >&2
      exit 1
    fi

image_name base_name stream flavor:
    @python3 scripts/flavors.py image "{{ flavor }}"

generate-default-tag stream build_number:
    @echo "{{ stream }}"

setup-cache base_name stream build_number event_name:
    @echo "utah-{{ stream }} 1"

# The half-hour OGC kernel compile and the NVIDIA module build live in their own
# image, keyed by their own inputs, so a push that touches neither does not pay
# for them.  See Containerfile.kernel.
kernel-cache-tag:
    @./scripts/kernel-cache-tag.sh

kernel-cache-ref:
    @echo "ghcr.io/{{ repo_organization }}/{{ kernel_cache_image }}:$(./scripts/kernel-cache-tag.sh)"

build-kernel-cache:
    #!/usr/bin/env bash
    set -euo pipefail
    ref="ghcr.io/{{ repo_organization }}/{{ kernel_cache_image }}:$(./scripts/kernel-cache-tag.sh)"
    echo "Building $ref"
    podman build --tag "$ref" --file Containerfile.kernel .

build-ghcr base_name stream flavor kernel_pin="":
    #!/usr/bin/env bash
    set -euo pipefail
    version="{{ stream }}-$(date -u +%Y%m%d)-$(git rev-parse --short HEAD)"
    image_name="$(python3 scripts/flavors.py image '{{ flavor }}')"
    # The kernel cache image and the layer cache below are both published
    # private by default, and the reusable build workflow only logs in to GHCR
    # for non-PR events -- so pulling either would 401 on exactly the runs that
    # need them most.  It passes GITHUB_TOKEN through to this recipe, so use it.
    if [ -n "${GITHUB_TOKEN:-}" ]; then
      echo "${GITHUB_TOKEN}" | podman login ghcr.io -u "${GITHUB_ACTOR:-x}" --password-stdin
    fi
    # main builds neither the OGC kernel nor an NVIDIA module, so it keeps the
    # pristine Hummingbird base and does not pull the cache image at all.  The
    # other three take the cache image as their base; the install scripts find
    # /utah-cache there and unpack rather than compile.
    base_args=()
    if [ "{{ flavor }}" != main ]; then
      cache_ref="$(./scripts/kernel-cache-tag.sh)"
      cache_ref="ghcr.io/{{ repo_organization }}/{{ kernel_cache_image }}:${cache_ref}"
      base_args=(--build-arg BASE_IMAGE="$cache_ref")
    fi
    # Registry layer cache, the same arrangement Bluefin uses.  The package
    # transaction is the one expensive layer in the Containerfile and its
    # inputs move rarely: the two manifests, the repo files, the pinned package
    # image and the install script.  With --cache-from an unchanged layer is
    # pulled instead of rebuilt; with --cache-to (REGISTRY_CACHE_WRITE=1, which
    # the reusable workflow sets for non-PR events only) it is published for
    # the next run.  Pull-request and local builds are read-only, so nothing a
    # PR does can poison what testing builds from.
    #
    # The cache lives in the image's own GHCR package as SHA-keyed blobs, so it
    # needs no package of its own and GITHUB_TOKEN already has write access to
    # it.  Podman 5 wants an untagged ref here.  The probe is skopeo list-tags,
    # which succeeds on any readable package and fails on one that is private
    # to us or not yet pushed, in which case the cache is simply off.
    layer_cache_ref="ghcr.io/{{ repo_organization }}/${image_name}"
    layer_cache_args=()
    layer_cache_readable=false
    if command -v skopeo >/dev/null 2>&1 && skopeo list-tags "docker://${layer_cache_ref}" >/dev/null 2>&1; then
      layer_cache_readable=true
      layer_cache_args+=(--cache-from "$layer_cache_ref")
    fi
    if [ "${REGISTRY_CACHE_WRITE:-0}" = "1" ]; then
      layer_cache_args+=(--cache-to "$layer_cache_ref")
      echo "Registry layer cache: read=${layer_cache_readable} write=true (${layer_cache_ref})"
    elif [ "$layer_cache_readable" = true ]; then
      echo "Registry layer cache: read-only (${layer_cache_ref})"
    else
      echo "Registry layer cache: off (${layer_cache_ref} is not readable from here)"
    fi
    podman build \
      "${base_args[@]}" \
      "${layer_cache_args[@]}" \
      --build-arg IMAGE_NAME="$image_name" \
      --build-arg IMAGE_ID="{{ image }}" \
      --build-arg IMAGE_FLAVOR={{ flavor }} \
      --build-arg IMAGE_VENDOR={{ repo_organization }} \
      --build-arg VERSION="$version" \
      --build-arg SHA_HEAD_SHORT="$(git rev-parse --short HEAD)" \
      --build-arg ENABLE_SSHD="${ENABLE_SSHD:-0}" \
      --tag "localhost/$image_name:{{ stream }}" \
      --file Containerfile .

# Compose with an RPM repository already in local containers-storage. This uses
# the same Containerfile transaction as CI without waiting for publication.
build-local stream="testing" package_image="localhost/utah-packages:local-merged":
    #!/usr/bin/env bash
    set -euo pipefail
    package_image="{{ package_image }}"
    podman image exists "$package_image" || {
      echo "Local package image not found: $package_image" >&2
      exit 1
    }
    version="local-{{ stream }}-$(git rev-parse --short HEAD)"
    podman build \
      --build-arg PACKAGE_IMAGE_REF="$package_image" \
      --build-arg IMAGE_NAME="{{ image }}" \
      --build-arg IMAGE_ID="{{ image }}" \
      --build-arg IMAGE_FLAVOR=main \
      --build-arg IMAGE_VENDOR="{{ repo_organization }}" \
      --build-arg VERSION="$version" \
      --build-arg SHA_HEAD_SHORT="$(git rev-parse --short HEAD)" \
      --build-arg ENABLE_SSHD="${ENABLE_SSHD:-1}" \
      --tag "localhost/{{ image }}:{{ stream }}" \
      --file Containerfile .

# Install Utah from its live ISO onto an encrypted disk in QEMU, boot the
# result, answer Plymouth's passphrase prompt, and confirm it comes up. Needs a
# debug ISO -- `just iso testing 1` -- because the install phase drives the
# installer over SSH.
luks-test iso_path="output/utah-live.iso" image="ghcr.io/projectbluefin/utah:testing":
    bash iso/scripts/luks-e2e.sh "{{ iso_path }}" "{{ image }}"

# Boot the disk luks-test installed, with VNC and a browser console, so the
# verified result can be driven by hand instead of only asserted about.
try-installed:
    bash iso/scripts/boot-installed.sh

generate-build-tags base_name stream flavor kernel_pin build_number version event_name event_number:
    @echo "{{ stream }} {{ version }}"

tag-images image_name default_tag alias_tags:
    #!/usr/bin/env bash
    set -euo pipefail
    for tag in {{ alias_tags }}; do podman tag "localhost/{{ image_name }}:{{ default_tag }}" "localhost/{{ image_name }}:$tag"; done

gen-sbom base_name stream flavor syft_cmd:
    #!/usr/bin/env bash
    set -euo pipefail
    image_name="$(python3 scripts/flavors.py image '{{ flavor }}')"
    mkdir -p "sbom_out/$image_name"
    "{{ syft_cmd }}" "localhost/$image_name:{{ stream }}" -o json >"sbom_out/$image_name/sbom.json"

# Install the locally built bootc image to a sparse disk for QEMU. This follows
# Bluefin's bootc-to-disk path rather than trying to boot an OCI layer directly.
generate-bootable-image stream="testing":
    #!/usr/bin/env bash
    set -euo pipefail
    ref="localhost/{{ image }}:{{ stream }}"
    # bootc needs the host root namespace. Local image builds normally use
    # rootless Podman, so transfer that image into rootful storage once before
    # running the disk install.
    if sudo podman image exists "$ref"; then
      PODMAN=(sudo podman)
    elif podman image exists "$ref"; then
      echo "Loading rootless $ref into rootful Podman for bootc..."
      archive="$(mktemp -p /var/tmp --suffix=.oci.tar)"
      trap 'rm -f "$archive"' EXIT
      podman save --format oci-archive -o "$archive" "$ref"
      sudo podman load -i "$archive"
      PODMAN=(sudo podman)
    else
      echo "Image $ref not found; run: just build-ghcr {{ image }} {{ stream }} main" >&2
      exit 1
    fi
    mkdir -p "{{ base_dir }}"
    disk="$(realpath "{{ base_dir }}")/bootable.raw"
    rm -f "$disk"
    fallocate -l 30G "$disk"
    echo "Installing $ref to $disk"
    install_args=(
      --via-loopback /data/bootable.raw
      --filesystem btrfs
      --wipe
      --generic-image
    )
    volumes=(
      -v "$(realpath "{{ base_dir }}"):/data:Z"
    )
    # The local OCI ref is not available from the guest's localhost registry;
    # mark this disposable disk so its published-image-only unified-storage
    # service is skipped instead of retrying forever.
    if [[ "$ref" == localhost/* ]]; then
      install_args+=( --karg=utah.local )
    fi
    # Inject the developer key into root for headless boot diagnostics. This
    # does not enable password login and is only present in the disposable
    # locally generated disk, never in the OCI image.
    if [ -f "$HOME/.ssh/id_ed25519.pub" ]; then
      volumes+=( -v "$HOME/.ssh:/ssh:ro" )
      install_args+=( --root-ssh-authorized-keys /ssh/id_ed25519.pub )
    fi
    "${PODMAN[@]}" run --rm --privileged --pid=host \
      "${volumes[@]}" \
      --security-opt label=type:unconfined_t \
      "$ref" bootc install to-disk "${install_args[@]}"
    # bootupd writes the vendor EFI entry but a fresh QEMU VM has no NVRAM
    # entry. Install the standard removable-media fallback so QEMU firmware
    # can find the image without importing host firmware variables.
    loop="$(sudo losetup --find --show --partscan "$disk")"
    esp="$(mktemp -d)"
    trap 'sudo umount "$esp" 2>/dev/null || true; sudo losetup -d "$loop" 2>/dev/null || true; rm -rf "$esp"' EXIT
    sudo mount "${loop}p2" "$esp"
    sudo install -d "$esp/EFI/BOOT"
    sudo install -m 0644 "$esp/EFI/fedora/shimx64.efi" "$esp/EFI/BOOT/BOOTX64.EFI"
    # Fedora's shim looks for its GRUB loader beside the removable-media
    # fallback path when no vendor NVRAM entry exists.
    sudo install -m 0644 "$esp/EFI/fedora/grubx64.efi" "$esp/EFI/BOOT/grubx64.efi"
    sudo umount "$esp"
    sudo losetup -d "$loop"
    trap - EXIT
    rm -rf "$esp"
    sync
    echo "Bootable disk ready: $disk"

# Build a single-architecture UEFI live ISO. `just luks-test` drives the
# bootc-installer payload this produces through an offline, LUKS2-encrypted
# install (see docs/verification); this recipe only builds the media.
iso stream="testing" debug="0":
    #!/usr/bin/env bash
    set -euo pipefail
    ref="localhost/{{ image }}:{{ stream }}"
    podman image exists "$ref" || { echo "Image $ref not found; run just build-ghcr {{ image }} {{ stream }} main" >&2; exit 1; }
    mkdir -p "{{ base_dir }}"
    bash iso/scripts/build-iso.sh "$ref" "$(realpath "{{ base_dir }}")/utah-live.iso" "Utah Live" "{{ debug }}" "ghcr.io/{{ repo_organization }}/{{ image }}:{{ stream }}"

# Boot the live ISO with QEMU-for-Docker and expose its noVNC console.
boot-iso:
    #!/usr/bin/env bash
    set -euo pipefail
    iso="$(realpath "{{ base_dir }}/utah-live.iso")"
    test -f "$iso" || { echo "Missing $iso; run just iso" >&2; exit 1; }
    port=8006
    while ss -H -tln "sport = :$port" 2>/dev/null | grep -q .; do port=$((port + 1)); done
    echo "Utah live ISO console: http://127.0.0.1:$port"
    podman run --rm --privileged --device /dev/kvm --pull=always \
      --publish "127.0.0.1:${port}:8006" \
      --env NETWORK=user \
      --env CPU_CORES="{{ vm_cpus }}" \
      --env RAM_SIZE="{{ vm_ram }}" \
      --env TPM=y \
      --env BOOT_MODE=uefi \
      --env ARGUMENTS=-snapshot \
      --volume "$iso:/boot.iso:ro" \
      ghcr.io/qemus/qemu:latest

# Boot the installed Utah disk through QEMU-for-Docker. Open the printed URL
# and confirm GDM appears and the GNOME Shell desktop renders. The disk is
# mounted at /boot.img and -snapshot keeps the test disposable.
boot-vm:
    #!/usr/bin/env bash
    set -euo pipefail
    disk="$(realpath "{{ base_dir }}/bootable.raw")"
    test -f "$disk" || { echo "Missing $disk; run just generate-bootable-image" >&2; exit 1; }
    port=8006
    while ss -H -tln "sport = :$port" 2>/dev/null | grep -q .; do
      port=$((port + 1))
    done
    ssh_port=2222
    while ss -H -tln "sport = :$ssh_port" 2>/dev/null | grep -q .; do
      ssh_port=$((ssh_port + 1))
    done
    echo "QEMU web console: http://127.0.0.1:$port"
    echo "SSH diagnostics: ssh -p $ssh_port root@127.0.0.1"
    podman run --rm --privileged --device /dev/kvm --pull=always \
      --publish "127.0.0.1:${port}:8006" \
      --publish "127.0.0.1:${ssh_port}:22" \
      --env USER_PORTS=22 \
      --env NETWORK=user \
      --env CPU_CORES="{{ vm_cpus }}" \
      --env RAM_SIZE="{{ vm_ram }}" \
      --env TPM=y \
      --env BOOT_MODE=uefi \
      --env ARGUMENTS=-snapshot \
      --volume "$disk:/boot.img" \
      ghcr.io/qemus/qemu:latest

secureboot base_name default_tag flavor:
    #!/usr/bin/env bash
    set -euo pipefail
    image_name="$(python3 scripts/flavors.py image '{{ flavor }}')"
    podman run --rm --entrypoint /bin/sh "localhost/$image_name:{{ default_tag }}" -c 'test -e /usr/lib/modules || test -e /boot'

# Regenerate the status page's package data from the manifests. Run this after
# changing packages/bluefin.toml or packages/utah.toml; `just check` fails if
# the committed copy is stale.
site-data:
    python3 scripts/generate-site-data.py

# Serve the status page locally at http://localhost:8000 for a visual check.
# The live status and roadmap cards call the public GitHub API from the browser.
site-serve: site-data
    python3 -m http.server 8000 --directory site
