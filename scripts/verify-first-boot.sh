#!/usr/bin/env bash
# Check the parts of the Bluefin desktop contract that only become observable
# after a Utah image has booted. This is a diagnostic command, not a boot
# dependency: it reports network-dependent Flatpak work as retryable instead of
# turning an offline machine into a permanently failed boot.

set -uo pipefail

STATUS_FILE="${UTAH_FIRST_BOOT_STATUS_FILE:-/var/lib/utah/first-boot-status}"
TAILSCALE_DEFERRED_STATE="${UTAH_TAILSCALE_DEFERRED_STATE:-/var/lib/ublue/setup-services/tailscale.deferred}"
FLATHUB_DESCRIPTOR=/etc/flatpak/remotes.d/flathub.flatpakrepo
FLATPAK_BREWFILE=/usr/share/ublue-os/homebrew/system-flatpaks.Brewfile
DESKTOP_CONTRACT=/usr/share/utah/bluefin-desktop.toml
DESKTOP_VERIFIER=/usr/local/libexec/utah-verify-desktop-contract

declare -a NOTES=()
declare -a MISSING_FLATPAKS=()
HARD_FAILURES=0
RETRYABLE_FAILURES=0

note() {
    NOTES+=("$*")
    printf 'utah-first-boot: %s\n' "$*"
}

hard_fail() {
    HARD_FAILURES=$((HARD_FAILURES + 1))
    note "FAIL: $*"
}

retryable() {
    RETRYABLE_FAILURES=$((RETRYABLE_FAILURES + 1))
    note "RETRYABLE: $*"
}

check_enabled() {
    local unit="$1"
    local state
    state="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
    case "$state" in
        enabled|enabled-runtime)
            note "enabled: ${unit}"
            ;;
        *)
            hard_fail "required unit is not enabled: ${unit} (${state:-not-found})"
            ;;
    esac
}

check_optional_enabled() {
    local unit="$1"
    if systemctl cat "$unit" >/dev/null 2>&1; then
        check_enabled "$unit"
    else
        note "optional unit is absent: ${unit}"
    fi
}

check_failed() {
    local unit="$1"
    local classification="$2"
    if ! systemctl is-failed --quiet "$unit" 2>/dev/null; then
        return 0
    fi

    if [[ "$classification" == retryable ]]; then
        retryable "setup unit is failed and should be retried: ${unit}"
    else
        hard_fail "setup unit is failed: ${unit}"
    fi
    systemctl status "$unit" --no-pager -l 2>&1 | head -40 || true
}

check_hook() {
    local hook="$1"
    if [[ ! -f "$hook" ]]; then
        hard_fail "first-boot hook is missing: ${hook}"
        return
    fi
    if bash -n "$hook"; then
        note "hook syntax is valid: ${hook}"
    else
        hard_fail "first-boot hook has invalid shell syntax: ${hook}"
    fi
}

check_flatpaks() {
    if [[ ! -f "$FLATHUB_DESCRIPTOR" ]]; then
        hard_fail "Flathub descriptor is missing: ${FLATHUB_DESCRIPTOR}"
    fi
    if ! command -v flatpak >/dev/null 2>&1; then
        hard_fail "flatpak is not installed"
        return
    fi

    local remotes
    remotes="$(flatpak remotes --system --columns=name 2>/dev/null || true)"
    if grep -Fxq flathub <<<"$remotes"; then
        note "Flathub system remote is registered"
    else
        # The descriptor is immutable image state; registration can still be
        # pending while flatpak-preinstall waits for network-online.target.
        retryable "Flathub system remote is not registered yet"
    fi

    if [[ ! -f "$FLATPAK_BREWFILE" ]]; then
        hard_fail "declared system Flatpak Brewfile is missing: ${FLATPAK_BREWFILE}"
        return
    fi

    local installed app
    installed="$(flatpak list --system --app --columns=application 2>/dev/null || true)"
    while IFS= read -r app; do
        [[ -n "$app" ]] || continue
        if ! grep -Fxq "$app" <<<"$installed"; then
            MISSING_FLATPAKS+=("$app")
        fi
    done < <(sed -nE 's/^[[:space:]]*flatpak[[:space:]]+"([^"]+)"[[:space:]]*$/\1/p' "$FLATPAK_BREWFILE")

    if ((${#MISSING_FLATPAKS[@]})); then
        local missing
        local old_ifs="$IFS"
        IFS=,
        missing="${MISSING_FLATPAKS[*]}"
        IFS="$old_ifs"
        retryable "declared system Flatpaks are pending or failed: ${missing}"
    else
        note "all declared system Flatpaks are installed"
    fi
}

check_tailscale() {
    if command -v tailscale >/dev/null 2>&1; then
        note "Tailscale binary is available"
        if [[ -f "$TAILSCALE_DEFERRED_STATE" ]]; then
            retryable "Tailscale still has deferred setup state: ${TAILSCALE_DEFERRED_STATE}"
        fi
    elif [[ -s "$TAILSCALE_DEFERRED_STATE" ]]; then
        retryable "Tailscale is optional and setup is deferred; see ${TAILSCALE_DEFERRED_STATE}"
    else
        # The hook may not run until a user logs in. Keep this visible in the
        # report even when the image has not yet had a chance to create its
        # persistent marker.
        retryable "Tailscale binary is absent; privileged setup will be deferred"
    fi
}

previous_boot_id=""
previous_boot_count=0
if [[ -r "$STATUS_FILE" ]]; then
    previous_boot_id="$(sed -n 's/^boot_id=//p' "$STATUS_FILE" | head -1)"
    previous_boot_count="$(sed -n 's/^boot_count=//p' "$STATUS_FILE" | head -1)"
fi
[[ "$previous_boot_count" =~ ^[0-9]+$ ]] || previous_boot_count=0
current_boot_id="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo unknown)"
repeat_boot=0
boot_count=$((previous_boot_count + 1))
if [[ -n "$previous_boot_id" && "$current_boot_id" != unknown && "$current_boot_id" != "$previous_boot_id" ]]; then
    repeat_boot=1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    hard_fail "systemctl is not installed"
else
    system_state="$(systemctl is-system-running 2>/dev/null || true)"
    case "$system_state" in
        running)
            note "systemd is running"
            ;;
        degraded)
            retryable "systemd is degraded; setup-unit results below are authoritative"
            systemctl --failed --no-legend --no-pager || true
            ;;
        *)
            hard_fail "systemd is not settled: ${system_state:-unknown}"
            ;;
    esac

    # Keep this list synchronized with [services] in the desktop contract.
    for unit in \
        bootc-unified-storage.service \
        bluefin-stats-refresh.timer \
        brew-setup.service \
        dconf-update.service \
        flatpak-nuke-fedora.service \
        flatpak-preinstall.service \
        gdm.service \
        input-remapper.service \
        ublue-system-setup.service \
        uupd.timer; do
        check_enabled "$unit"
    done

    if systemctl --global is-enabled ublue-user-setup.service >/dev/null 2>&1; then
        note "enabled globally: ublue-user-setup.service"
    else
        hard_fail "required global user service is not enabled: ublue-user-setup.service"
    fi

    check_optional_enabled tailscaled.service

    for unit in bootc-unified-storage.service gdm.service ublue-system-setup.service \
        flatpak-nuke-fedora.service dconf-update.service input-remapper.service \
        bluefin-stats-refresh.service uupd.service; do
        check_failed "$unit" hard
    done
    check_failed flatpak-preinstall.service retryable
    check_failed brew-setup.service retryable
    if systemctl cat tailscaled.service >/dev/null 2>&1; then
        check_failed tailscaled.service retryable
    fi
fi

for hook in \
    /usr/share/ublue-os/privileged-setup.hooks.d/10-tailscale.sh \
    /usr/share/ublue-os/privileged-setup.hooks.d/11-framework-ucsi-workaround.sh \
    /usr/share/ublue-os/privileged-setup.hooks.d/99-flatpaks.sh \
    /usr/share/ublue-os/user-setup.hooks.d/12-gnupg.sh \
    /usr/share/ublue-os/user-setup.hooks.d/20-framework.sh \
    /usr/share/ublue-os/user-setup.hooks.d/99-privileged.sh; do
    check_hook "$hook"
done

check_flatpaks
check_tailscale

if [[ ! -x "$DESKTOP_VERIFIER" ]]; then
    hard_fail "desktop contract verifier is missing: ${DESKTOP_VERIFIER}"
elif [[ ! -f "$DESKTOP_CONTRACT" ]]; then
    hard_fail "desktop contract is missing: ${DESKTOP_CONTRACT}"
else
    if "$DESKTOP_VERIFIER" "$DESKTOP_CONTRACT"; then
        note "static desktop contract passed from the booted image"
    else
        hard_fail "static desktop contract failed from the booted image"
    fi
fi

if [[ "${UTAH_EXPECT_REPEAT:-0}" == 1 && "$repeat_boot" -ne 1 ]]; then
    hard_fail "repeat-boot validation requested, but no previous boot was recorded"
fi

if ((${#MISSING_FLATPAKS[@]})); then
    missing_flatpaks="${MISSING_FLATPAKS[*]}"
else
    missing_flatpaks=none
fi

write_status() {
    local result="$1"
    local status_dir status_tmp note_line
    status_dir="$(dirname "$STATUS_FILE")"
    if ! mkdir -p "$status_dir" 2>/dev/null; then
        printf 'utah-first-boot: unable to create status directory: %s\n' "$status_dir" >&2
        return
    fi
    status_tmp="${STATUS_FILE}.tmp.$$"
    {
        printf 'format=1\n'
        printf 'result=%s\n' "$result"
        printf 'boot_id=%s\n' "$current_boot_id"
        printf 'boot_count=%s\n' "$boot_count"
        printf 'repeat_boot=%s\n' "$repeat_boot"
        printf 'hard_failures=%s\n' "$HARD_FAILURES"
        printf 'retryable_failures=%s\n' "$RETRYABLE_FAILURES"
        printf 'missing_flatpaks=%s\n' "$missing_flatpaks"
        for note_line in "${NOTES[@]}"; do
            printf 'note=%s\n' "$note_line"
        done
    } >"$status_tmp" && mv -f "$status_tmp" "$STATUS_FILE"
}

if ((HARD_FAILURES)); then
    write_status failed
    printf 'UTAH_FIRST_BOOT_FAIL hard_failures=%s retryable_failures=%s\n' \
        "$HARD_FAILURES" "$RETRYABLE_FAILURES"
    exit 1
elif ((RETRYABLE_FAILURES)); then
    write_status retryable
    printf 'UTAH_FIRST_BOOT_RETRYABLE retryable_failures=%s\n' "$RETRYABLE_FAILURES"
    exit 0
else
    write_status passed
    printf 'UTAH_FIRST_BOOT_OK boot_count=%s repeat_boot=%s\n' "$boot_count" "$repeat_boot"
    exit 0
fi
