#!/usr/bin/bash
# Exercise the shared CountMe client on a booted Utah image without sending a
# production ping. The VM supplies its real image-info and bootc status; only
# curl is replaced so the request payload can be inspected locally.
#
# The shipped timer (OnActiveSec=0) also starts the real service at boot, before
# this script runs, and that unstubbed run does attempt to reach
# countme.projectbluefin.io. It cannot get there: luks-e2e.sh boots the
# installed disk on a QEMU user netdev with restrict=on, which gives the guest
# no outbound route. That isolation, not this stub, keeps the review VM's boot
# from being counted.
set -euo pipefail

expected_flavor="${1:?expected flavor required}"
[[ "${expected_flavor}" =~ ^[a-z-]+$ ]] || {
    echo "invalid expected flavor: ${expected_flavor}" >&2
    exit 2
}

timer=projectbluefin-countme.timer
service=projectbluefin-countme.service
client=/usr/libexec/projectbluefin-countme
opt_out=/etc/projectbluefin/countme/disabled

[[ "$(systemctl is-enabled "${timer}")" == enabled ]] || {
    echo "${timer} is not enabled" >&2
    exit 1
}
systemctl is-active --quiet "${timer}" || {
    echo "${timer} is not active" >&2
    exit 1
}
[[ -x "${client}" ]] || {
    echo "shared CountMe client is missing: ${client}" >&2
    exit 1
}
[[ ! -e "${opt_out}" ]] || {
    echo "CountMe opt-out marker is unexpectedly present in the image" >&2
    exit 1
}

work="$(mktemp -d /tmp/utah-countme.XXXXXX)"
# Leave the system as shipped: no opt-out marker and the timer running again
# after the opt-out check below stops it.
trap 'rm -rf "${work}"; rm -f "${opt_out}"; systemctl start "${timer}" >/dev/null 2>&1 || true' EXIT
mkdir -p "${work}/bin" "${work}/state"
cat > "${work}/bin/curl" <<'EOF'
#!/usr/bin/bash
printf '%s\0' "$@" > "${COUNTME_CAPTURE:?}"
EOF
chmod 0755 "${work}/bin/curl"

COUNTME_CAPTURE="${work}/curl-args" \
STATE_DIRECTORY="${work}/state" \
COUNTME_HOST=http://127.0.0.1 \
PATH="${work}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    "${client}"
[[ -s "${work}/curl-args" && -e "${work}/state/last" ]] || {
    echo "CountMe client did not issue the mocked request" >&2
    exit 1
}
python3 - "${work}/curl-args" "${expected_flavor}" <<'PY'
import json
import pathlib
import sys

args = [arg for arg in pathlib.Path(sys.argv[1]).read_bytes().split(b"\0") if arg]
try:
    payload_index = args.index(b"--json") + 1
    payload = json.loads(args[payload_index])
except (ValueError, IndexError, json.JSONDecodeError) as error:
    raise SystemExit(f"CountMe request payload is invalid: {error}")

image = payload.get("image", "")
expected_prefix = f"utah/{sys.argv[2]}:"
if not image.startswith(expected_prefix):
    raise SystemExit(f"CountMe reported {image!r}, expected {expected_prefix!r}*")
if args[-1] != b"http://127.0.0.1/v1/ping":
    raise SystemExit(f"CountMe request escaped the local test endpoint: {args[-1]!r}")
print(f"CountMe reports {image} to the local test endpoint")
PY

# The service-level condition, not the client, enforces opt-out. Start the
# service with the canonical marker present and prove ExecStart was skipped.
systemctl stop "${timer}" "${service}" >/dev/null 2>&1 || true
prior_start_time="$(systemctl show --property=ExecMainStartTimestamp --value "${service}")"
install -D -m 0644 /dev/null "${opt_out}"
sleep 1
systemctl start "${service}"
condition_result="$(systemctl show --property=ConditionResult --value "${service}")"
[[ "${condition_result}" == no ]] || {
    echo "${service} did not honor the CountMe opt-out marker (ConditionResult=${condition_result})" >&2
    exit 1
}
start_time="$(systemctl show --property=ExecMainStartTimestamp --value "${service}")"
[[ "${start_time}" == "${prior_start_time}" ]] || {
    echo "${service} executed despite the CountMe opt-out marker" >&2
    exit 1
}
echo "CountMe opt-out prevented service execution"
