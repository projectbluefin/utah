#!/usr/bin/env bash
# Service-state checks shared by luks-e2e.sh and lifecycle-e2e.sh. Sourced,
# never run.
#
# The caller provides ssh_target and scp_target (the installed system, as
# TEST_USER), fail (report and exit), ROOT, TEST_USER and TEST_PASSWORD.
#
# verify_setup_state is a content gate: it holds the booted image to the
# [services] lists of its contract, a check added after earlier :testing
# images were built (#1282). luks-e2e.sh runs it on every image it installs
# except the lifecycle's baseline (UTAH_E2E_BASELINE=1); lifecycle-e2e.sh runs
# it on the upgraded candidate instead.

check_system_unit_state() {
    local unit="$1" required_enabled="${2:-false}" required_run="${3:-false}" boot_pass="${4:-first}"
    local state load_state="" active_state="" result="" start_time="" key value
    state="$(ssh_target "printf '%s\\n' '${TEST_PASSWORD}' | sudo -S -p '' systemctl show --property=LoadState --property=ActiveState --property=Result --property=ExecMainStartTimestamp '${unit}'" 2>/dev/null || true)"
    while IFS='=' read -r key value; do
        case "${key}" in
            LoadState) load_state="${value}" ;;
            ActiveState) active_state="${value}" ;;
            Result) result="${value}" ;;
            ExecMainStartTimestamp) start_time="${value}" ;;
        esac
    done <<< "${state}"
    [[ "${load_state}" == loaded ]] || fail "required system unit ${unit} is ${load_state:-unavailable}"
    # Result= is success/exit-code/signal/timeout/...; it is never "failed".
    [[ "${active_state}" != failed && "${result}" == success ]] \
        || fail "system unit ${unit} failed during ${boot_pass} boot (ActiveState=${active_state:-unknown} Result=${result:-unknown})"
    if [[ "${required_run}" == true && ( -z "${start_time}" || "${start_time}" == "n/a" ) ]]; then
        fail "required system unit ${unit} did not execute during ${boot_pass} boot"
    fi
    if [[ "${required_enabled}" == true ]]; then
        state="$(ssh_target "printf '%s\\n' '${TEST_PASSWORD}' | sudo -S -p '' systemctl is-enabled '${unit}'" 2>/dev/null || true)"
        [[ "${state}" == enabled ]] || fail "required system unit ${unit} is ${state:-not enabled}"
    fi
    echo "  ${unit}: ${result:-unknown}"
}

check_user_unit_state() {
    local unit="$1" required_run="${2:-false}" boot_pass="${3:-first}" offline_failure_ok="${4:-false}"
    local state load_state="" active_state="" result="" start_time="" enabled="" key value
    state="$(ssh_target "systemctl --user show --property=LoadState --property=ActiveState --property=Result --property=ExecMainStartTimestamp '${unit}'" 2>/dev/null || true)"
    while IFS='=' read -r key value; do
        case "${key}" in
            LoadState) load_state="${value}" ;;
            ActiveState) active_state="${value}" ;;
            Result) result="${value}" ;;
            ExecMainStartTimestamp) start_time="${value}" ;;
        esac
    done <<< "${state}"
    [[ "${load_state}" == loaded ]] || fail "user unit ${unit} is ${load_state:-unavailable}"
    # Result= is success/exit-code/signal/timeout/...; it is never "failed".
    if [[ "${offline_failure_ok}" != true ]]; then
        [[ "${active_state}" != failed && "${result}" == success ]] \
            || fail "user unit ${unit} failed during ${boot_pass} boot (ActiveState=${active_state:-unknown} Result=${result:-unknown})"
    fi
    if [[ "${required_run}" == true && ( -z "${start_time}" || "${start_time}" == "n/a" ) ]]; then
        fail "user unit ${unit} did not execute during ${boot_pass} boot"
    fi
    enabled="$(ssh_target "systemctl --user is-enabled '${unit}'" 2>/dev/null || true)"
    [[ "${enabled}" == enabled ]] || fail "user unit ${unit} is ${enabled:-not enabled}"
    echo "  ${unit}: ${result:-unknown}; enabled"
}

# Print one [services] list (enabled or user_enabled) of the contract the
# installed image ships, so the runtime check follows the image under test
# rather than a list copied into this script. The parser is the repo's
# verifier, copied over because /usr/local/libexec does not survive into a
# bootc deployment.
contract_units() {
    local kind="$1" units
    scp_target "${ROOT}/scripts/verify-desktop-contract.py" \
        "${TEST_USER}@127.0.0.1:/tmp/utah-e2e-verify-desktop-contract.py" >/dev/null \
        || fail "could not copy the contract parser to the installed system"
    units="$(ssh_target "python3 /tmp/utah-e2e-verify-desktop-contract.py --services '${kind}' /usr/share/utah/bluefin-desktop.toml" 2>/dev/null)" \
        || fail "could not read [services].${kind} from the installed contract"
    [[ -n "${units}" ]] || fail "the installed contract lists no [services].${kind} units"
    printf '%s\n' "${units}"
}

verify_setup_state() {
    local boot_pass="${1:-first}" listed unit
    local -a units
    echo "Checking ${boot_pass}-boot setup units and desktop enablement..."
    # These two do their work on every boot, so they must have run, not only
    # be enabled.
    check_system_unit_state flatpak-preinstall.service true true "${boot_pass}"
    check_system_unit_state ublue-system-setup.service true true "${boot_pass}"
    check_user_unit_state ublue-user-setup.service true "${boot_pass}"
    # Every unit the installed contract requires enabled must still be enabled
    # after first-boot presets ran, and must not have failed. Units whose
    # conditions skip them in a VM (thermald, intel_lpmd) end inactive with
    # Result=success and pass. Timers are checked, not the network jobs they
    # trigger, because the luks VM has no network.
    # The lists are read into arrays first: ssh_target reads stdin, so a
    # `while read ... <<< "${units}"` loop lost every unit after the first.
    # Command substitution, not process substitution, so a contract_units
    # failure still stops the script.
    listed="$(contract_units enabled)"
    mapfile -t units <<< "${listed}"
    for unit in "${units[@]}"; do
        [[ -n "${unit}" ]] || continue
        check_system_unit_state "${unit}" true false "${boot_pass}"
    done
    listed="$(contract_units user_enabled)"
    mapfile -t units <<< "${listed}"
    for unit in "${units[@]}"; do
        [[ -n "${unit}" ]] || continue
        case "${unit}" in
            # Installs Homebrew packages from the network, so it fails in the
            # offline luks VM by design; its enablement is still asserted.
            brew-preinstall.service) check_user_unit_state "${unit}" false "${boot_pass}" true ;;
            *) check_user_unit_state "${unit}" false "${boot_pass}" ;;
        esac
    done
    # kanata replaced input-remapper (#1222): installed and loaded, opt-in, so
    # neither enablement nor a run is required.
    check_system_unit_state kanata.service false false "${boot_pass}"
}
