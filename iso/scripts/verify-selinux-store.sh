#!/usr/bin/env bash
# Run as root in a booted Utah guest by iso/scripts/lifecycle-e2e.sh: prove
# that persistent SELinux changes work and survive an update (#551).
#
# Four kinds of local change stand in for everything the standard tools can
# persist: a boolean (`setsebool -P`), a file-context rule (`semanage
# fcontext`), a port label (`semanage port`) and a local module (`semodule
# -i`). The harness applies them before an update and verifies them after
# the reboot into the new deployment, then removes them.
#
#   probe    print `persistent` when the module store lives under writable
#            /etc/selinux, `immutable` when it is still under /usr.
#   apply    make the four sample changes; fail if any tool fails or the
#            change is not in effect afterwards.
#   verify   the four changes are in effect, the store is listable, SELinux
#            is enforcing, home directories keep their labels, and every
#            type and module of the image's own policy is part of the
#            running policy (a local change must not hold the policy back).
#   reject   offer semodule a module it must refuse (invalid CIL); the install
#            must fail and leave the store, the running policy and the local
#            changes exactly as they were, so the machine stays bootable.
#   remove   undo the four changes and confirm none is left in effect.
#
# The three paths can be overridden for host-side fixture tests; nothing on
# the host is touched when they point into a scratch tree.
set -euo pipefail

mode="${1:-}"
SEMANAGE_CONF="${UTAH_SEMANAGE_CONF:-/etc/selinux/semanage.conf}"
PRISTINE_STORE="${UTAH_PRISTINE_STORE:-/usr/etc/selinux/targeted}"
HOME_ROOT="${UTAH_HOME_ROOT:-/var/home}"

BOOLEAN=virt_use_samba
FCONTEXT_DIR=/srv/utah-e2e
FCONTEXT_RULE="${FCONTEXT_DIR}(/.*)?"
FCONTEXT_TYPE=httpd_sys_content_t
PORT=18081
PORT_TYPE=http_port_t
MODULE=utah_e2e
MODULE_TYPE=utah_e2e_t
BAD_MODULE=utah_e2e_bad

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

run() {
    echo "+ $*"
    "$@" || fail "'$*' exited $?"
}

store_is_persistent() {
    grep -qx 'store-root=/etc/selinux' "$SEMANAGE_CONF"
}

boolean_is() {
    [[ "$(getsebool "$BOOLEAN")" == *"--> $1" ]]
}

fcontext_listed() {
    local rules
    rules=$(semanage fcontext -l -C | grep -F -- "$FCONTEXT_RULE" || true)
    [[ "$rules" == *"$FCONTEXT_TYPE"* ]]
}

port_listed() {
    local ports
    ports=$(semanage port -l -C | grep -E "^${PORT_TYPE} +tcp +" || true)
    [[ " ${ports//,/ } " == *" ${PORT} "* ]]
}

module_listed() {
    grep -qx "$MODULE" <<<"$(semodule -l)"
}

type_in_running_policy() {
    [[ "$(seinfo -t "$MODULE_TYPE" 2>/dev/null || true)" == *"$MODULE_TYPE"* ]]
}

labelled_as() {
    [[ "$(stat -c '%C' "$1")" == *":$2:"* ]]
}

require_enforcing() {
    local mode_now
    mode_now=$(getenforce)
    [[ "$mode_now" == Enforcing ]] || fail "getenforce reports ${mode_now}, want Enforcing"
    echo "getenforce: ${mode_now}"
}

require_home_labels() {
    local got
    got=$(matchpathcon -m dir "$HOME_ROOT" | awk '{print $2}')
    [[ "$got" == *:home_root_t:* ]] || fail "${HOME_ROOT} resolves to ${got}, want home_root_t"
    got=$(matchpathcon -m dir "${HOME_ROOT}/someone" | awk '{print $2}')
    [[ "$got" == *:user_home_dir_t:* ]] || fail "${HOME_ROOT}/someone resolves to ${got}, want user_home_dir_t"
    got=$(stat -c '%C' "$HOME_ROOT")
    [[ "$got" == *:home_root_t:* ]] || fail "${HOME_ROOT} is labelled ${got} on disk, want home_root_t"
    echo "home labels: ${HOME_ROOT} home_root_t, ${HOME_ROOT}/<user> user_home_dir_t"
}

# The image's own policy must be the running policy, with the local changes
# on top: every type of the pristine compiled policy under /usr/etc and
# every module shipped in the pristine store must be present. A machine
# whose local store held back the new image's policy fails here.
require_image_policy() {
    local pristine_policy missing name active_modules
    pristine_policy=$(find "${PRISTINE_STORE}/policy" -maxdepth 1 -name 'policy.*' 2>/dev/null | sort -V | tail -n 1)
    [[ -n "$pristine_policy" ]] || fail "no compiled policy under ${PRISTINE_STORE}/policy"
    missing=$(comm -23 \
        <(seinfo "$pristine_policy" -t | tail -n +2 | sed 's/^ *//' | sort) \
        <(seinfo -t | tail -n +2 | sed 's/^ *//' | sort))
    [[ -z "$missing" ]] || fail "types of the image's policy are missing from the running policy: $(echo "$missing" | head -n 5 | tr '\n' ' ')"
    missing=""
    active_modules=$(semodule -l)
    for dir in "${PRISTINE_STORE}"/active/modules/*/*/; do
        [[ -d "$dir" ]] || continue
        name=$(basename "$dir")
        if [[ ! -e "${dir}/disabled" ]] && ! grep -qx "$name" <<<"$active_modules"; then
            missing+=" ${name}"
        fi
    done
    [[ -z "$missing" ]] || fail "modules of the image's store are missing from the active store:${missing}"
    echo "image policy: every pristine type and module is in the running policy"
}

report_finalize_log() {
    # Informational: the previous boot's deployment finalization is where
    # ostree rebuilds the policy. Journal persistence is not guaranteed.
    journalctl -b -1 -u ostree-finalize-staged.service --no-pager 2>/dev/null \
        | grep -i -E 'selinux|semodule|policy' || echo "(no SELinux lines from the previous boot's ostree-finalize-staged.service)"
}

case "$mode" in
    probe)
        if store_is_persistent; then
            echo persistent
        else
            echo immutable
        fi
        ;;
    apply)
        store_is_persistent || fail "store-root in ${SEMANAGE_CONF} is not /etc/selinux; persistent changes cannot work"
        require_enforcing
        run setsebool -P "$BOOLEAN" 1
        boolean_is on || fail "${BOOLEAN} is not on after setsebool -P"
        run semanage fcontext -a -t "$FCONTEXT_TYPE" "$FCONTEXT_RULE"
        fcontext_listed || fail "file context rule for ${FCONTEXT_RULE} is not listed"
        mkdir -p "$FCONTEXT_DIR"
        : > "${FCONTEXT_DIR}/index.html"
        run restorecon -R "$FCONTEXT_DIR"
        labelled_as "${FCONTEXT_DIR}/index.html" "$FCONTEXT_TYPE" \
            || fail "${FCONTEXT_DIR}/index.html did not take ${FCONTEXT_TYPE}"
        run semanage port -a -t "$PORT_TYPE" -p tcp "$PORT"
        port_listed || fail "port ${PORT}/tcp is not labelled ${PORT_TYPE}"
        module_dir=$(mktemp -d)
        printf '(type %s)\n' "$MODULE_TYPE" > "${module_dir}/${MODULE}.cil"
        run semodule -i "${module_dir}/${MODULE}.cil"
        rm -rf "$module_dir"
        module_listed || fail "module ${MODULE} is not listed by semodule -l"
        type_in_running_policy || fail "${MODULE_TYPE} is not in the running policy"
        require_home_labels
        require_enforcing
        echo "PASS: four local SELinux changes applied and in effect"
        ;;
    verify)
        store_is_persistent || fail "store-root in ${SEMANAGE_CONF} is not /etc/selinux"
        require_enforcing
        report_finalize_log
        run semodule -l >/dev/null
        run semanage boolean -l >/dev/null
        boolean_is on || fail "${BOOLEAN} is not on"
        fcontext_listed || fail "file context rule for ${FCONTEXT_RULE} is not listed"
        labelled_as "${FCONTEXT_DIR}/index.html" "$FCONTEXT_TYPE" \
            || fail "${FCONTEXT_DIR}/index.html lost ${FCONTEXT_TYPE}"
        port_listed || fail "port ${PORT}/tcp is not labelled ${PORT_TYPE}"
        module_listed || fail "module ${MODULE} is not listed by semodule -l"
        type_in_running_policy || fail "${MODULE_TYPE} is not in the running policy"
        require_image_policy
        require_home_labels
        echo "PASS: local SELinux changes in effect alongside the image's policy"
        ;;
    remove)
        run setsebool -P "$BOOLEAN" 0
        boolean_is off || fail "${BOOLEAN} is still on"
        run semanage fcontext -d "$FCONTEXT_RULE"
        ! fcontext_listed || fail "file context rule for ${FCONTEXT_RULE} is still listed"
        run semanage port -d -p tcp "$PORT"
        ! port_listed || fail "port ${PORT}/tcp is still labelled"
        run semodule -r "$MODULE"
        ! module_listed || fail "module ${MODULE} is still listed"
        ! type_in_running_policy || fail "${MODULE_TYPE} is still in the running policy"
        rm -rf "$FCONTEXT_DIR"
        require_home_labels
        require_enforcing
        echo "PASS: local SELinux changes removed"
        ;;
    reject)
        store_is_persistent || fail "store-root in ${SEMANAGE_CONF} is not /etc/selinux"
        require_enforcing
        module_dir=$(mktemp -d)
        printf '(type %s\n(this is not valid CIL\n' "$BAD_MODULE" > "${module_dir}/${BAD_MODULE}.cil"
        echo "+ semodule -i ${module_dir}/${BAD_MODULE}.cil (must be rejected)"
        if semodule -i "${module_dir}/${BAD_MODULE}.cil"; then
            rm -rf "$module_dir"
            fail "semodule accepted the invalid module ${BAD_MODULE}"
        fi
        rm -rf "$module_dir"
        run semodule -l >/dev/null
        ! grep -qx "$BAD_MODULE" <<<"$(semodule -l)" || fail "rejected module ${BAD_MODULE} is listed"
        if [[ -n "$(find /etc/selinux/targeted/active -name "${BAD_MODULE}*" 2>/dev/null)" ]]; then
            fail "rejected module ${BAD_MODULE} left files in the store"
        fi
        boolean_is on || fail "${BOOLEAN} changed after the rejected module"
        module_listed || fail "module ${MODULE} vanished after the rejected module"
        type_in_running_policy || fail "${MODULE_TYPE} left the running policy after the rejected module"
        require_image_policy
        require_home_labels
        require_enforcing
        echo "PASS: invalid local module rejected; store, policy and labels unchanged"
        ;;
    *)
        echo "Usage: $0 probe|apply|verify|reject|remove" >&2
        exit 2
        ;;
esac
