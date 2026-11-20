#!/usr/bin/env bash
# Exit codes and the wall-clock deadline shared by luks-e2e.sh and
# lifecycle-e2e.sh. Sourced, never run.
#
# Exit codes (docs/skills/lifecycle-e2e.md#exit-codes):
#   0       passed
#   1       usage error, or a failure before the first phase
#   11-17   luks-e2e.sh failed in phase 1-7 (10 + phase number)
#   20      lifecycle-e2e.sh failed preparing or booting the installed disk
#           (an install-phase failure keeps luks-e2e.sh's own code instead)
#   21-25   lifecycle-e2e.sh failed in phase 1-5 (20 + phase number)
#   75      E2E_WALL_CLOCK_DEADLINE reached (EX_TEMPFAIL); evidence was kept
#   77      a host dependency (QEMU, OVMF) is missing
#
# A harness sets E2E_PHASE_EXIT at each phase heading. Its EXIT trap reports
# `e2e_status`: E2E_EXIT_CODE when a failure named its own code, otherwise the
# phase's code for any failure -- a `fail` call and a bare command tripping
# `set -e` in the same phase are the same class.

E2E_EXIT_DEADLINE=75
E2E_EXIT_DEPENDENCY=77
E2E_PHASE_EXIT=1
E2E_EXIT_CODE=""

# e2e_status STATUS: the exit status to report for a harness that is exiting
# with STATUS.
e2e_status() {
    if [[ -n "${E2E_EXIT_CODE}" ]]; then
        echo "${E2E_EXIT_CODE}"
    elif (( $1 == 0 )); then
        echo 0
    else
        echo "${E2E_PHASE_EXIT}"
    fi
}

# e2e_exit CODE MESSAGE: fail with an explicit code.
e2e_exit() {
    E2E_EXIT_CODE="$1"
    shift
    echo "FAIL: $*" >&2
    exit "${E2E_EXIT_CODE}"
}

# e2e_deadline_start HANDLER
#
# When E2E_WALL_CLOCK_DEADLINE (epoch seconds) is set, run HANDLER in this
# shell once it passes. HANDLER records what it can and calls
# `e2e_exit "${E2E_EXIT_DEADLINE}" ...`, so the harness fails with its
# evidence while the CI job still has time to upload it, rather than being
# killed by timeout-minutes with nothing kept.
#
# Bash runs a signal trap only after the foreground command returns, so the
# watcher signals first and, if the harness is still running after
# E2E_DEADLINE_GRACE seconds (default 30), terminates the harness's child
# processes to unblock it. The grace lets a short command finish and lets a
# nested harness that watches the same deadline fail on its own first.
e2e_deadline_start() {
    local handler="$1" deadline="${E2E_WALL_CLOCK_DEADLINE:-}"
    [[ -n "${deadline}" ]] || return 0
    [[ "${deadline}" =~ ^[0-9]+$ ]] \
        || e2e_exit 1 "E2E_WALL_CLOCK_DEADLINE must be epoch seconds, got '${deadline}'"
    (( EPOCHSECONDS < deadline )) \
        || e2e_exit "${E2E_EXIT_DEADLINE}" "the wall-clock deadline passed before the harness started"
    echo "Wall-clock deadline: $(date -u -d "@${deadline}" '+%Y-%m-%d %H:%M:%S UTC') ($(( (deadline - EPOCHSECONDS) / 60 )) min from now)"
    trap "${handler}" USR1
    local main=$$ grace="${E2E_DEADLINE_GRACE:-30}"
    (
        trap - USR1
        while kill -0 "${main}" 2>/dev/null; do
            local left=$(( deadline - EPOCHSECONDS ))
            if (( left <= 0 )); then
                kill -USR1 "${main}" 2>/dev/null || exit 0
                sleep "${grace}"
                kill -0 "${main}" 2>/dev/null && pkill -TERM -P "${main}" 2>/dev/null
                exit 0
            fi
            sleep $(( left < 5 ? left : 5 ))
        done
    ) </dev/null >/dev/null 2>&1 &
}
