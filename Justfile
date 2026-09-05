repo_organization := env_var_or_default("REPO_ORGANIZATION", "projectbluefin")
image := "utah"
kernel_cache_image := "utah-kernel-cache"
base_dir := env_var_or_default("BASE_DIR", "output")
vm_ram := env_var_or_default("VM_RAM", "8192")
vm_cpus := env_var_or_default("VM_CPUS", "4")

default:
    @just --list

check:
    #!/usr/bin/env bash
    set -euo pipefail
    test -f Containerfile
    test -f packages/bluefin.toml
    test -f packages/utah.toml
    test -f packages/utah-packages.repo
    test -f system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset
    test -f system_files/shared/usr/lib/systemd/system/bootc-unified-storage.service.d/10-utah-local-test.conf
    grep -q 'enable gdm.service' system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset
    grep -q 'enable ublue-system-setup.service' system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset
    test -f scripts/configure-services.sh
    bash -n scripts/configure-services.sh
    test -f scripts/configure-branding.sh
    bash -n scripts/configure-branding.sh
    test -f scripts/verify-desktop-contract.py
    test -f scripts/verify-gnome-extensions.py
    test -f contracts/bluefin-desktop.toml
    python3 -m py_compile scripts/verify-desktop-contract.py scripts/verify-gnome-extensions.py
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
    bash -n iso/live/src/configure-live.sh
    bash -n iso/live/src/install-flatpaks.sh
    bash -n iso/scripts/build-iso.sh
    python3 -m json.tool iso/live/src/etc/bootc-installer/images.json >/dev/null
    python3 -m json.tool iso/live/src/etc/bootc-installer/recipe.json >/dev/null
    grep -q 'org.bootcinstaller.Installer' iso/live/src/install-flatpaks.sh
    grep -q 'containers-storage' iso/scripts/build-iso.sh
    grep -q 'UTAH_LIVE' iso/scripts/build-iso.sh
    grep -q 'ENABLE_SSHD' Containerfile
    grep -q 'ENABLE_SSHD="${ENABLE_SSHD:-0}"' Justfile
    grep -q 'ARG PACKAGE_IMAGE_SHA=' Containerfile
    grep -q 'COPY --from=packages /repository /etc/utah-packages' Containerfile
    grep -q '"utah-packages"' scripts/install-packages.py
    python3 -m py_compile scripts/install-packages.py
    python3 -m py_compile scripts/verify-rpm-contract.py
    python3 -m py_compile scripts/check-repo-availability.py
    python3 scripts/install-packages.py --check packages/bluefin.toml
    # Both scripts compose their package list from sections named one by
    # one, so a new section in utah.toml can be live in one and invisible
    # to the other. [services] was unverified that way, and [firmware]
    # briefly was too.
    python3 scripts/check-overlay-sections.py
    python3 scripts/verify-rpm-contract.py --check packages/bluefin.toml
    grep -qE 'reusable-build\.yml@(v1|[0-9a-f]{40} # v1)$' .github/workflows/build.yml
    test -f Containerfile.kernel
    bash -n scripts/install-ogc-kernel.sh
    bash -n scripts/install-nvidia.sh
    bash -n scripts/clean-stage.sh
    bash -n scripts/kernel-cache-tag.sh
    # The cache image must be built from the same base the image is, or the
    # prebuilt NVIDIA module would be linked against a kernel this image never
    # boots.  Two literals, one invariant, so assert it rather than trust it.
    diff <(grep -m1 '^ARG BASE_IMAGE=' Containerfile) \
         <(grep -m1 '^ARG BASE_IMAGE=' Containerfile.kernel)
    python3 -m py_compile scripts/flavors.py
    python3 scripts/flavors.py list >/dev/null
    pip install --quiet pyyaml 2>/dev/null || true
    python3 scripts/check_workflow_outputs.py
    # No workflow may carry its own copy of the flavor list. That drift is what
    # config/flavors.json exists to stop: narrowing the build matrix while
    # promote and release still name images nothing produces fails late.
    ! grep -rn 'utah-nvidia\|utah-gaming' .github/workflows/
    # Nor its own count of them. Promotion asserted `count -eq 4` after it had
    # already copied whatever it found, so a short set advanced :testing for
    # the flavors it did reach and failed afterwards.
    ! grep -nE '\-eq [0-9]+' .github/workflows/post-testing-e2e.yml
    grep -q 'scripts/flavors.py' .github/workflows/post-testing-e2e.yml
    # Promotion must be gated on a real boot, not only on the build's own
    # in-image contract checks. Those cannot see whether the thing starts.
    test -f scripts/verify-boot.sh
    test -f Containerfile.e2e
    bash -n scripts/verify-boot.sh
    grep -q 'e2e-boot' .github/workflows/post-testing-e2e.yml
    grep -qE '^\s*needs: \[resolve, boot\]' .github/workflows/post-testing-e2e.yml
    grep -q 'UTAH_BOOT_OK' scripts/verify-boot.sh
    # The verification layer is a test artifact. If it ever appears in the
    # image's own Containerfile it would ship a unit that powers the machine
    # off shortly after gdm starts.
    ! grep -q 'verify-boot' Containerfile

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
# Checks names only -- it does not assert versions.  Needs network access.
check-repos:
    python3 scripts/check-repo-availability.py packages/bluefin.toml packages/utah.toml

# packages/bluefin.toml is a verbatim copy of Bluefin's base.toml.  Drift here
# is a parity bug, so make it loud rather than letting it accumulate quietly.
check-parity:
    #!/usr/bin/env bash
    set -euo pipefail
    upstream=$(mktemp)
    trap 'rm -f "$upstream"' EXIT
    curl -fsSL -o "$upstream" \
      https://raw.githubusercontent.com/projectbluefin/bluefin/main/build_files/packages/base.toml
    if diff -u "$upstream" packages/bluefin.toml; then
      echo "packages/bluefin.toml matches projectbluefin/bluefin"
    else
      echo "packages/bluefin.toml has drifted from projectbluefin/bluefin" >&2
      exit 1
    fi

image_name base_name stream flavor:
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{ flavor }}" in
      main) echo "{{ image }}" ;;
      nvidia) echo "{{ image }}-nvidia" ;;
      gaming) echo "{{ image }}-gaming" ;;
      nvidia-gaming) echo "{{ image }}-nvidia-gaming" ;;
      *) echo "unknown Utah image flavor: {{ flavor }}" >&2; exit 2 ;;
    esac

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
    case "{{ flavor }}" in
      main) image_name="{{ image }}" ;;
      nvidia) image_name="{{ image }}-nvidia" ;;
      gaming) image_name="{{ image }}-gaming" ;;
      nvidia-gaming) image_name="{{ image }}-nvidia-gaming" ;;
      *) echo "unknown Utah image flavor: {{ flavor }}" >&2; exit 2 ;;
    esac
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

generate-build-tags base_name stream flavor kernel_pin build_number version event_name event_number:
    @echo "{{ stream }} {{ version }}"

tag-images image_name default_tag alias_tags:
    #!/usr/bin/env bash
    set -euo pipefail
    for tag in {{ alias_tags }}; do podman tag "localhost/{{ image_name }}:{{ default_tag }}" "localhost/{{ image_name }}:$tag"; done

gen-sbom base_name stream flavor syft_cmd:
    #!/usr/bin/env bash
    set -euo pipefail
    image_name="$(just image_name '{{ base_name }}' '{{ stream }}' '{{ flavor }}')"
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

# The runner has no KVM, so this is TCG emulation and slow -- minutes, not
# seconds. That is still the cheapest honest answer to "does it boot", and it
# is the one gate the build-time contract checks cannot supply.
#
# Boot an image headless and assert what only a boot can show.
e2e-boot image_ref timeout="1800":
    #!/usr/bin/env bash
    set -euo pipefail
    work="$(mktemp -d)"
    trap 'rm -rf "$work"' EXIT
    # The verification layer is built here and never pushed; see
    # Containerfile.e2e for why it is not part of the image under test.
    test_ref="localhost/utah-e2e:$(date +%s)"
    podman build --build-arg IMAGE="{{ image_ref }}" \
      --tag "$test_ref" --file Containerfile.e2e .
    disk="$work/boot.raw"
    truncate -s 20G "$disk"
    # console=ttyS0 is what makes the verifier readable: the harness has no way
    # into this VM other than the serial port.
    sudo podman run --rm --privileged --pid=host \
      --security-opt label=type:unconfined_t \
      -v "$work:/data:Z" \
      "$test_ref" bootc install to-disk \
        --via-loopback /data/boot.raw --filesystem btrfs --wipe \
        --generic-image --karg console=ttyS0,115200 --karg utah.local
    sudo chown "$(id -u):$(id -g)" "$disk"
    # A fresh VM has no NVRAM entry, so install the removable-media fallback
    # the same way generate-bootable-image does.
    loop="$(sudo losetup --find --show --partscan "$disk")"
    esp="$work/esp"; mkdir -p "$esp"
    sudo mount "${loop}p2" "$esp"
    sudo install -d "$esp/EFI/BOOT"
    sudo install -m 0644 "$esp/EFI/fedora/shimx64.efi" "$esp/EFI/BOOT/BOOTX64.EFI"
    sudo install -m 0644 "$esp/EFI/fedora/grubx64.efi" "$esp/EFI/BOOT/grubx64.efi"
    sudo umount "$esp"
    sudo losetup -d "$loop"
    cp /usr/share/OVMF/OVMF_VARS_4M.fd "$work/vars.fd" 2>/dev/null \
      || cp /usr/share/OVMF/OVMF_VARS.fd "$work/vars.fd"
    code=/usr/share/OVMF/OVMF_CODE_4M.fd
    test -f "$code" || code=/usr/share/OVMF/OVMF_CODE.fd
    log="$work/console.log"
    set +e
    timeout "{{ timeout }}" qemu-system-x86_64 \
      -machine q35 -m 4096 -smp "$(nproc)" -no-reboot -nographic \
      -drive "if=pflash,format=raw,unit=0,readonly=on,file=$code" \
      -drive "if=pflash,format=raw,unit=1,file=$work/vars.fd" \
      -drive "file=$disk,format=raw,if=virtio" \
      -serial "file:$log" -display none
    set -e
    echo "--- serial console"
    cat "$log" || true
    echo "--- end serial console"
    if grep -q '^UTAH_BOOT_OK ' "$log"; then
      grep '^UTAH_BOOT_OK ' "$log"
      exit 0
    fi
    echo "The VM never reported a successful boot." >&2
    grep -E 'utah-boot:|Kernel panic|emergency|Failed to' "$log" >&2 || true
    exit 1

# Build a single-architecture UEFI live ISO. This first slice proves the
# Utah live boot path; installer payload integration is intentionally the next
# ISO milestone.
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
    image_name="$(just image_name '{{ base_name }}' '{{ default_tag }}' '{{ flavor }}')"
    podman run --rm --entrypoint /bin/sh "localhost/$image_name:{{ default_tag }}" -c 'test -e /usr/lib/modules || test -e /boot'
