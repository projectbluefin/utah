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
    ts="${id#audit(}"; ts="${ts%%.*}"
    echo "-- journal $((ts - 2))..$((ts + 2)):"
    journalctl -b --no-pager -o short-precise --since="@$((ts - 2))" --until="@$((ts + 2))" 2>/dev/null | head -n 60
done
