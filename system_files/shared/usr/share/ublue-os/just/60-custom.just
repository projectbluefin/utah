# Utah's entry point imports these at a shallower depth than Common's recipes.

# Show device diagnostics locally; optionally upload when fpaste is installed
[group('System')]
device-info:
    #!/usr/bin/bash
    set -euo pipefail
    report="$(mktemp "${TMPDIR:-/tmp}/utah-device-info.XXXXXX")"
    trap 'rm -f "$report"' EXIT
    {
        echo '--#--# bootc status --verbose:'
        bootc status --verbose
        if command -v fpaste >/dev/null; then
            echo '--#--# fpaste --sysinfo --printonly:'
            fpaste --sysinfo --printonly
        else
            echo '--#--# OS:'
            cat /etc/os-release
            echo '--#--# Kernel:'
            uname -a
            echo '--#--# CPU:'
            lscpu
            echo '--#--# Block devices:'
            lsblk
            echo '--#--# Memory:'
            free -h
        fi
        echo '--#--# flatpak list --columns=application,version,options:'
        flatpak list --columns=application,version,options
    } > "$report"
    cat "$report"
    if ! command -v fpaste >/dev/null; then
        echo 'fpaste is unavailable on Utah; diagnostics are shown above. Review them before sharing.' >&2
    elif gum confirm 'Submit this information to the CentOS pastebin?'; then
        fpaste < "$report"
    fi

# Show release notes, using plain text when glow is unavailable
changelogs:
    #!/usr/bin/bash
    set -euo pipefail
    IMAGE_INFO_FILE="${IMAGE_INFO_FILE:-/usr/share/ublue-os/image-info.json}"
    TAG="$(jq -r '.["image-tag"]' < "$IMAGE_INFO_FILE")"
    IMAGE_NAME="$(jq -r '.["image-name"] // empty' < "$IMAGE_INFO_FILE")"
    # Resolve the owning repo through ublue-image-repo. utah is not in the
    # shared grammar, so it falls through to --default; that default must be
    # Utah's own repo, not bluefin's (otherwise ujust changelogs opens
    # projectbluefin/bluefin releases). See projectbluefin/utah#466.
    REPO="$("${UBLUE_IMAGE_REPO_BIN:-/usr/libexec/ublue-image-repo}" \
        --default 'projectbluefin/utah' "$IMAGE_NAME" "$TAG")"
    CONTENT=''
    if [[ "$TAG" =~ gts$|stable$|^lts ]]; then
        DATE="$(grep -oP 'OSTREE_VERSION=.*\d{2}\.\K\d{8}[.0-9]*' /etc/os-release || true)"
        CONTENT="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases" | \
            jq -r --arg tag "${TAG}-${DATE}" '.[] | select(.tag_name == $tag) | .body // empty')"
    fi
    if [[ -z "$CONTENT" ]]; then
        echo 'WARN: Could not find a release specific to your image' >&2
        CONTENT="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" | jq -er '.body // empty')"
    fi
    if command -v glow >/dev/null; then
        printf '%s\n' "$CONTENT" | glow -p
    else
        printf '%s\n' "$CONTENT"
    fi

# Explain Utah's pending module-signing support without changing MOK state
[group('System')]
enroll-secure-boot-key:
    #!/usr/bin/bash
    echo 'Utah does not yet ship a kernel-module signing key to enroll.' >&2
    echo 'Secure Boot module signing and enrollment are tracked at https://github.com/projectbluefin/utah/issues/395' >&2
    exit 1

# Install gdb, elfutils and sysprof-cli until reboot to diagnose shell hangs
[group('System')]
install-debug-tools:
    #!/usr/bin/bash
    set -euo pipefail
    if [[ "$(id -u)" -ne 0 ]]; then
        exec sudo "$0" "$@"
    fi
    # /usr is read-only on a bootc system; the overlay is discarded on reboot.
    # bootc refuses when an overlay already exists, so only that case is
    # tolerated; any other failure leaves /usr read-only and stops here.
    if ! overlay_err="$(bootc usr-overlay 2>&1)"; then
        if [[ ! -w /usr ]]; then
            printf '%s\n' "$overlay_err" >&2
            echo 'error: could not unlock /usr with bootc usr-overlay.' >&2
            exit 1
        fi
        echo 'Using the existing /usr overlay.' >&2
    fi
    DNF="$(command -v dnf5 || command -v dnf)"
    "$DNF" install -y gdb-headless elfutils sysprof-cli
    echo 'Installed until the next reboot. Attach with: sudo gdb -p $(pgrep -u "$USER" -x gnome-shell)'

# Set up VMM and local libvirt together, or disable activation without removing data
[group('System')]
manage-virtualization: (_virtualization "virtualization")

# Install Virtual Machine Manager together with its working system libvirt backend
[group('System')]
setup-virtualization: (_virtualization "setup")

# Dakota's recipes (files/just-overrides/system.just) run its devmode helper
# directly. Utah ships QEMU but not libvirt yet, which the helper drives, so
# say so instead of letting it refuse on units it cannot find (#1232).
_virtualization action:
    #!/usr/bin/bash
    set -euo pipefail
    if [[ ! -e "${LIBVIRT_SOCKET_UNIT:-/usr/lib/systemd/system/virtqemud.socket}" ]]; then
        echo 'libvirt is not in this image yet (projectbluefin/utah-packages#1021).' >&2
        echo 'QEMU/KVM, toolbox and the VM guest agents are installed.' >&2
        exit 1
    fi
    exec "${DAKOTA_DEVMODE:-/usr/libexec/dakota-devmode}" "{{ action }}"
