#!/usr/bin/bash
# Explain each chcon/bootc AVC denial on this boot: the audit records that
# share its serial (SYSCALL carries ppid and exe; PROCTITLE the command line),
# the parent's systemd unit as the journal recorded it, and the journal around
# the denial. luks-e2e.sh runs it on the installed system when #1198's check
# fails, so a failure names its source instead of a timing guess.
set -u
log=/var/log/audit/audit.log
grep -hE 'avc: +denied .*comm="(chcon|bootc)"' "$log" 2>/dev/null \
    | grep -oE 'audit\([0-9.]+:[0-9]+\)' | sort -u | while read -r id; do
    echo "== ${id}"
    records="$(grep -hF "msg=${id}" "$log")"
    printf '%s\n' "$records"
    title="$(grep -oE 'proctitle=[0-9A-F]+' <<<"$records" | head -n1 | cut -d= -f2)"
    [ -n "$title" ] && echo "-- command: $(python3 -c 'import sys; print(bytes.fromhex(sys.argv[1]).replace(b"\0", b" ").decode(errors="replace"))' "$title")"
    ppid="$(grep -oE ' ppid=[0-9]+' <<<"$records" | head -n1 | cut -d= -f2)"
    if [ -n "$ppid" ]; then
        echo "-- parent ${ppid}:"
        journalctl -b "_PID=${ppid}" -o verbose --no-pager 2>/dev/null \
            | grep -E '^\s*(_SYSTEMD_UNIT|_COMM|_CMDLINE|_EXE)=' | sort -u | head -n 8
    fi
    # This image loads no audit rules, so there is no SYSCALL record and no
    # ppid. A parent's PID is almost always just below its child's: list the
    # services whose main process started in the 300 PIDs before the denial.
    pid="$(grep -oE ' pid=[0-9]+' <<<"$records" | head -n1 | cut -d= -f2)"
    if [ -n "$pid" ]; then
        echo "-- services with ExecMainPID in $((pid - 300))..${pid}:"
        systemctl list-units --all --type=service --plain --no-legend 2>/dev/null | awk '{print $1}' \
            | xargs -r systemctl show -p Id -p ExecMainPID -p ExecMainStartTimestamp 2>/dev/null \
            | awk -v lo="$((pid - 300))" -v hi="$pid" -F= '
                $1=="Id"{id=$2} $1=="ExecMainPID"{p=$2}
                $1=="ExecMainStartTimestamp"{ if (p+0>=lo && p+0<=hi) print p, id, $2 }' | sort -n
    fi
    ts="${id#audit(}"; ts="${ts%%.*}"
    echo "-- journal $((ts - 1))..$((ts + 1)):"
    journalctl -b --no-pager -o short-precise --since="@$((ts - 1))" --until="@$((ts + 1))" 2>/dev/null | head -n 400
done
