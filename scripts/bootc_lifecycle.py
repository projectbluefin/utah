#!/usr/bin/env python3
"""Bootc upgrade and rollback lifecycle validator and diagnostics helper.

Parses `bootc status --format=json` output, validates lifecycle transitions
across baseline, staged, upgraded, and rollback phases, and formats structured
failure diagnostics identifying the active deployment and digest at each phase.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class DeploymentInfo:
    slot: str
    image: str
    transport: str
    digest: str
    version: str | None = None
    timestamp: str | None = None
    pinned: bool = False
    ostree_checksum: str | None = None
    stateroot: str | None = None
    deploy_serial: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _extract_slot_deployment(slot_name: str, entry: dict[str, Any] | None) -> DeploymentInfo | None:
    if not entry or not isinstance(entry, dict):
        return None

    # Handle image container info: entry may contain "image" dictionary
    img_data = entry.get("image")
    if not isinstance(img_data, dict):
        return None

    # Extract image reference and transport
    ref_obj = img_data.get("image")
    if isinstance(ref_obj, dict):
        image_name = ref_obj.get("image", "")
        transport = ref_obj.get("transport", "")
    elif isinstance(ref_obj, str):
        image_name = ref_obj
        transport = img_data.get("transport", "")
    else:
        image_name = ""
        transport = ""

    # Extract image digest: handle imageDigest, image_digest, digest
    digest = (
        img_data.get("imageDigest")
        or img_data.get("image_digest")
        or img_data.get("digest")
        or entry.get("imageDigest")
        or entry.get("image_digest")
        or ""
    )

    version = img_data.get("version") or entry.get("version")
    timestamp = img_data.get("timestamp") or entry.get("timestamp")
    pinned = bool(entry.get("pinned", False))

    # bootc's BootEntry exposes an "ostree" object with the commit checksum,
    # the stateroot name, and the deploy serial. The boot-manager checks key
    # off (stateroot, deploy_serial), which the BLS entry's `options` line
    # carries in its `ostree=/ostree/boot.N/<stateroot>/<bootcsum>/<serial>`
    # path. The `<bootcsum>` segment is NOT this commit checksum (see
    # _parse_ostree_karg_path), so never match on it.
    ostree_obj = entry.get("ostree")
    if not isinstance(ostree_obj, dict):
        ostree_obj = entry.get("Ostree") if isinstance(entry.get("Ostree"), dict) else None
    ostree_checksum: str | None = None
    stateroot: str | None = None
    deploy_serial: int | None = None
    if isinstance(ostree_obj, dict):
        csum_raw = ostree_obj.get("checksum") or ostree_obj.get("Checksum")
        if csum_raw:
            ostree_checksum = str(csum_raw)
        stateroot_raw = ostree_obj.get("stateroot") or ostree_obj.get("Stateroot")
        if stateroot_raw:
            stateroot = str(stateroot_raw)
        # `or` short-circuits on a falsy serial (legitimate value: 0), so
        # prefer `deploySerial` only if it is present (and not None), and
        # fall back to `deploy_serial` for non-upstream variants. The
        # BootEntryOstree serde uses `deploy_serial` in snake_case and
        # `deploySerial` in camelCase depending on the field-rename rule;
        # try both.
        serial_raw: object | None
        if "deploySerial" in ostree_obj:
            serial_raw = ostree_obj.get("deploySerial")
        elif "deploy_serial" in ostree_obj:
            serial_raw = ostree_obj.get("deploy_serial")
        else:
            serial_raw = None
        if serial_raw is not None:
            try:
                deploy_serial = int(serial_raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                deploy_serial = None

    return DeploymentInfo(
        slot=slot_name,
        image=str(image_name),
        transport=str(transport),
        digest=str(digest),
        version=str(version) if version is not None else None,
        timestamp=str(timestamp) if timestamp is not None else None,
        pinned=pinned,
        ostree_checksum=ostree_checksum,
        stateroot=stateroot,
        deploy_serial=deploy_serial,
    )


def image_repository(ref: str) -> str:
    """Return the repository portion of an image reference, without tag or digest.

    uupd upgrades whatever reference the booted deployment already tracks, so the
    lifecycle harness compares repositories rather than full pinned references
    before handing the upgrade to uupd.
    """
    ref = ref.strip()
    if not ref:
        return ""
    ref = ref.split("@", 1)[0]
    head, sep, last = ref.rpartition("/")
    if ":" in last:
        last = last.split(":", 1)[0]
    return f"{head}{sep}{last}"


def parse_bootc_status(raw_data: str | dict[str, Any]) -> dict[str, DeploymentInfo]:
    """Parse raw JSON string or dict from `bootc status --format=json`."""
    if isinstance(raw_data, str):
        try:
            data = json.loads(raw_data)
        except Exception as exc:
            raise ValueError(f"Invalid bootc status JSON: {exc}") from exc
    elif isinstance(raw_data, dict):
        data = raw_data
    else:
        raise ValueError(f"Expected str or dict, got {type(raw_data).__name__}")

    # The JSON schema wraps HostStatus either under top-level 'status' or at root
    host_status = data.get("status", data)
    if not isinstance(host_status, dict):
        raise ValueError("Invalid bootc status structure: status is not an object")

    deployments: dict[str, DeploymentInfo] = {}

    for slot in ("booted", "staged", "rollback"):
        dep = _extract_slot_deployment(slot, host_status.get(slot))
        if dep:
            deployments[slot] = dep

    return deployments


# A systemd-boot entry follows the Boot Loader Specification (BLS) Type #1:
# one `key value` line per option (whitespace-separated, NOT `key=value`),
# with continuation lines starting with whitespace. Only the keys the
# lifecycle suite cares about are read -- the whole file is not modelled
# because bootc-generated entries use a small, stable subset, and adding
# every key would just trade noise for failures when an unrelated
# extension key shows up.


@dataclass
class LoaderEntry:
    """A parsed BLS Type 1 boot loader entry.

    `filename` is the path the listing recorded the entry under (relative to
    the ESP root). It is preserved so failure diagnostics can name the file a
    human can inspect. `raw` keeps the original content for callers that want
    to do further matching beyond the parsed keys.

    `paths` is a list of `(path, present)` tuples for the linux/initrd/
    image lines the harness captured at listing time. A BLS entry that
    points at a pruned kernel or initrd surfaces here as `(path, False)`,
    so the validator can fail it as a missing-file regression even when
    the entry's `linux`/`initrd` lines are syntactically present.
    """

    filename: str
    fields: dict[str, str]
    raw: str
    paths: list[tuple[str, bool]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "fields": dict(self.fields),
            "paths": [
                {"path": p, "present": present} for p, present in self.paths
            ],
        }


def parse_loader_entry(
    filename: str,
    content: str,
    paths: list[tuple[str, bool]] | None = None,
) -> LoaderEntry:
    """Parse a single BLS Type #1 entry file's `key value` lines.

    The Boot Loader Specification (BLS) Type #1 format uses one or more
    spaces as the key/value separator, not `=`; the spec is explicit that
    "the first word of a line is used as key and is separated by one or
    more spaces from the value", and the rest of the line is the value
    verbatim. systemd-boot and bootc both follow that form, so the
    parser must too.

    Lines starting with `#` are comments and ignored; continuation lines
    start with whitespace and belong to the previous key (a `linux` value
    can in principle span lines, though bootc's emitter does not produce
    them). The first occurrence of a key wins, matching what bootc emits
    so a stray duplicate does not shadow the real setting.
    """
    fields: dict[str, str] = {}
    current_key: str | None = None
    for raw_line in content.splitlines():
        if not raw_line:
            continue
        # Continuation lines belong to the prior key; the leading whitespace
        # is significant in the spec, so append verbatim with a single
        # separating space.
        if raw_line[0] in (" ", "\t") and current_key is not None:
            appended = (fields.get(current_key, "") + " " + raw_line.strip()).strip()
            fields[current_key] = appended
            continue
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition(" ")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if key not in fields:
            fields[key] = value
            current_key = key
        else:
            current_key = None
    return LoaderEntry(filename=filename, fields=fields, raw=content, paths=paths or [])


# The ESP listing is captured as a single blob so the lifecycle harness can
# pass it through stdin: each entry is delimited by a header line of the
# form "=== ENTRY <path> ===" followed by the entry's content, terminated
# by an "=== END ===" sentinel. The format is intentionally trivial -- the
# harness assembles it with a `for` loop over `find` results -- so the
# parser can stay strict without surprising the producer.

LOADER_LISTING_HEADER = "=== ENTRY"
LOADER_LISTING_END = "=== END ==="


def parse_loader_listing(text: str) -> list[LoaderEntry]:
    """Parse a harness-emitted listing into LoaderEntry objects.

    Accepts an empty listing (no entries found) without raising; an
    installation whose boot manager failed to write any BLS entry must
    surface as a clear validation failure rather than a parser exception.

    Each entry begins with a header line of the form
    `=== ENTRY <path> ===` and ends with a standalone `=== END ===`
    sentinel. The closing `===` on the header is part of the format and
    must be stripped before the entry is recorded, otherwise downstream
    diagnostics quote a path that does not exist on disk.

    `STAT <entry-file> <path> present|missing` lines that follow an
    entry's `=== END ===` are captured into the entry's `paths` list so
    validate_bootmgr_entries can fail the entry when its kernel or
    initrd is missing on disk -- not just syntactically absent.
    """
    entries: list[LoaderEntry] = []
    if not text:
        return entries
    current_name: str | None = None
    current_buf: list[str] = []
    current_paths: list[tuple[str, bool]] = []
    for line in text.splitlines():
        if line.startswith(LOADER_LISTING_HEADER):
            # Commit the previous entry only when the next one starts,
            # so STAT lines emitted AFTER `=== END ===` (and before
            # the next `=== ENTRY` header) attach to the right entry.
            if current_name is not None and current_buf:
                entries.append(
                    parse_loader_entry(
                        current_name, "\n".join(current_buf) + "\n", current_paths
                    )
                )
            rest = line[len(LOADER_LISTING_HEADER):].strip()
            # The trailing `===` is the close of the header sentinel, not
            # part of the path; trim it so a quoting consumer sees the
            # exact string the harness read off the ESP.
            if rest.endswith("==="):
                rest = rest[:-3].rstrip()
            current_name = rest
            current_buf = []
            current_paths = []
            continue
        if line.strip() == LOADER_LISTING_END:
            # Do NOT commit here. STAT lines follow; keep current_name,
            # current_buf, and current_paths until the next `=== ENTRY`
            # header (or end of input) commits the entry. Also skip
            # appending `=== END ===` itself to current_buf.
            continue
        # STAT lines are emitted by the harness AFTER `=== END ===`
        # so they do not belong to current_buf. They attach to the
        # entry whose filename matches the first token.
        if current_name is not None and line.startswith("STAT "):
            parts = line.split(maxsplit=3)
            if len(parts) == 4 and parts[1] == current_name:
                current_paths.append((parts[2], parts[3] == "present"))
            continue
        if current_name is not None and line.strip() != "":
            current_buf.append(line)
    # End of input: commit the last in-flight entry. A missing trailing
    # sentinel still recovers the entry rather than dropping it; the
    # STAT lines that happened to be emitted are already attached.
    if current_name is not None and current_buf:
        entries.append(
            parse_loader_entry(current_name, "\n".join(current_buf), current_paths)
        )
    return entries


def _parse_ostree_karg_path(options: str) -> tuple[str, int] | None:
    """Extract `(stateroot, deploy_serial)` from the BLS `options` line.

    ostree writes an `ostree=/ostree/boot.<N>/<stateroot>/<bootcsum>/<serial>`
    kernel argument into every BLS entry it generates
    (src/libostree/ostree-sysroot-deploy.c: `g_strdup_printf ("ostree=/ostree/boot.%d/%s/%s/%d",
    bootversion, osname, bootcsum, deployserial)`). The `<bootcsum>` segment
    is a hash of the kernel+initramfs layout (`ostree_deployment_get_bootcsum`),
    NOT the commit checksum the deployment object exposes as `ostree_checksum`
    -- matching on it never succeeds and the harness check would always fail
    at baseline. The deployment-unique pair is `(stateroot, deploy_serial)`,
    which bootc's `BootEntryOstree` JSON also exposes.

    Returns `None` if no `ostree=` karg is present or the trailing
    `<serial>` is not an integer.
    """
    # The kargs may be space-separated; isolate the ostree= token. The value
    # may be quoted if it contains a space (it never does in practice), so
    # tokenising on whitespace is enough.
    for token in options.split():
        if not token.startswith("ostree="):
            continue
        path = token[len("ostree="):]
        # Strip an optional surrounding pair of quotes (defensive: ostree
        # never quotes the path, but other boot managers might).
        if len(path) >= 2 and path[0] == path[-1] and path[0] in ("'", '"'):
            path = path[1:-1]
        segments = [s for s in path.split("/") if s]
        # segments are [ostree, boot.<N>, stateroot, bootcsum, serial]
        # (the leading '/' is dropped by the filter). Need at least
        # four: boot.N, stateroot, bootcsum, serial. The deployment-
        # unique pair is stateroot + serial; the bootcsum in the
        # middle is irrelevant for matching (and is not exposed by
        # bootc's JSON status, so we cannot compare it anyway).
        if len(segments) < 4:
            return None
        try:
            serial = int(segments[-1])
        except ValueError:
            return None
        return segments[-3], serial
    return None


def validate_bootmgr_entries(
    status_data: str | dict[str, Any],
    listing_text: str,
    expected_slots: tuple[str, ...] = ("booted", "staged", "rollback"),
) -> tuple[bool, str, dict[str, Any]]:
    """Validate systemd-boot entries against bootc status.

    Match the deployment's BLS entry by the `(stateroot, deploy_serial)`
    tuple ostree writes into every BLS entry it generates
    (`ostree=/ostree/boot.N/<stateroot>/<bootcsum>/<serial>` karg;
    see _parse_ostree_karg_path). The `<bootcsum>` segment is the
    kernel+initramfs layout hash and is intentionally NOT used as a match
    key -- bootc's `BootEntryOstree` JSON does not expose it.

    ostree allocates `deployserial` per (osname, commit), so two
    deployments with distinct commits but no prior deployment at that
    commit both receive serial 0. Matching purely by `(stateroot,
    deploy_serial)` would let one BLS entry satisfy two deployments and
    silently miss a missing-entry regression. The validator therefore
    groups both the expected deployments and the captured BLS entries by
    their `(stateroot, deploy_serial)` tuple and requires the count of
    entries in each group to be at least the count of deployments; each
    deployment then claims a unique entry from its group (greedy in slot
    order). Entries that cannot be parsed (no `ostree=` karg) are surfaced
    as malformed regardless of how many well-formed entries exist, since
    any unparseable BLS file on the ESP is a defect.

    Each claimed entry is also checked to carry `linux` (kernel) and at
    least one of `initrd` or `options` (the loader will silently skip an
    entry missing those). A missing `linux` line is fatal because the
    loader will ignore the entry on next reboot, which is the regression
    the lifecycle suite exists to catch.

    The check is per-deployment so a missing entry for one slot (e.g. no
    `rollback` deployment after rollback is the desired state) is reported
    as PASS for that slot, while a missing entry for a slot the harness
    expects (e.g. `staged` after `bootc switch`) fails the phase with the
    offending slot named in the message.
    """
    deployments = parse_bootc_status(status_data)
    entries = parse_loader_listing(listing_text)

    diag: dict[str, Any] = {
        "entries": [e.to_dict() for e in entries],
        "matches": {},
        "missing": [],
        "malformed": [],
        "unparseable": [],
    }

    failures: list[str] = []

    # Index entries by the (stateroot, deploy_serial) tuple ostree
    # writes into each BLS entry's `options` line.
    entry_index: dict[tuple[str, int], list[LoaderEntry]] = {}
    for entry in entries:
        parsed = _parse_ostree_karg_path(entry.fields.get("options", ""))
        if parsed is None:
            diag["unparseable"].append(entry.to_dict())
            continue
        entry_index.setdefault(parsed, []).append(entry)

    if diag["unparseable"]:
        names = ", ".join(e["filename"] for e in diag["unparseable"])
        failures.append(
            f"{len(diag['unparseable'])} BLS entries have no parseable "
            f"ostree= karg and cannot be matched: {names}"
        )

    # Group expected deployments by (stateroot, deploy_serial). Slots
    # without a deployment (e.g. empty rollback after rollback) are PASS
    # without consuming an entry, matching the per-slot semantics the
    # harness expects.
    expected_by_key: dict[tuple[str, int], list[tuple[str, DeploymentInfo]]] = {}
    for slot in expected_slots:
        diag["matches"][slot] = None
        dep = deployments.get(slot)
        if dep is None:
            continue
        if dep.stateroot is None or dep.deploy_serial is None:
            failures.append(
                f"{slot} deployment is missing stateroot or deploy_serial "
                f"and cannot be anchored to a BLS entry "
                f"(digest {dep.digest or 'unknown'})"
            )
            continue
        expected_by_key.setdefault(
            (dep.stateroot, dep.deploy_serial), []
        ).append((slot, dep))

    # Validate counts per group, then greedily assign each expected
    # deployment a unique entry from its group.
    for key, deps_in_key in expected_by_key.items():
        stateroot, serial = key
        have_entries = entry_index.get(key, [])
        need = len(deps_in_key)
        have = len(have_entries)
        if have < need:
            for slot, dep in deps_in_key:
                failures.append(
                    f"No BLS entry found for {slot} deployment "
                    f"(stateroot={stateroot}, deploy_serial={serial}, "
                    f"image digest {dep.digest or 'unknown'}; "
                    f"have {have} BLS entries, need {need})"
                )
                diag["missing"].append(
                    {
                        "slot": slot,
                        "stateroot": stateroot,
                        "deploy_serial": serial,
                        "digest": dep.digest,
                    }
                )
            continue
        # have >= need: assign each expected slot a unique entry in slot
        # order. The chosen entry's bootcsum on disk belongs to one of the
        # commits in the group; we cannot distinguish them without
        # ostree's bootcsum which BootEntryOstree does not expose, so the
        # greedy assignment is for diagnostics only -- the count check
        # above is what proves no entry went missing.
        claimed: set[int] = set()
        for slot, dep in deps_in_key:
            chosen_idx = next(
                (i for i in range(len(have_entries)) if i not in claimed),
                None,
            )
            if chosen_idx is None:
                failures.append(
                    f"Could not assign a unique BLS entry to {slot} "
                    f"deployment (stateroot={stateroot}, "
                    f"deploy_serial={serial})"
                )
                continue
            claimed.add(chosen_idx)
            chosen = have_entries[chosen_idx]
            diag["matches"][slot] = chosen.to_dict()
            linux = chosen.fields.get("linux", "")
            if not linux:
                failures.append(
                    f"BLS entry '{chosen.filename}' for {slot} "
                    f"deployment has no 'linux' line"
                )
                diag["malformed"].append(
                    {"slot": slot, "filename": chosen.filename, "reason": "missing linux"}
                )
                continue
            initrd = chosen.fields.get("initrd", "")
            options = chosen.fields.get("options", "")
            if not initrd and not options:
                failures.append(
                    f"BLS entry '{chosen.filename}' for {slot} deployment "
                    f"has neither 'initrd' nor 'options'"
                )
                diag["malformed"].append(
                    {"slot": slot, "filename": chosen.filename, "reason": "missing initrd/options"}
                )
                continue
            # Filesystem check: the linux/initrd/image paths the harness
            # captured must resolve on disk. Without this, a BLS entry
            # that points at a pruned kernel passes the syntactic check
            # above and silently breaks next boot. The harness emits
            # STAT lines for every referenced path; if no STAT was
            # captured (older listing, harness change), we trust the
            # syntax check alone and skip the filesystem gate.
            if chosen.paths:
                missing = [p for p, present in chosen.paths if not present]
                if missing:
                    failures.append(
                        f"BLS entry '{chosen.filename}' for {slot} "
                        f"deployment points at missing files: "
                        f"{missing}"
                    )
                    diag["malformed"].append(
                        {
                            "slot": slot,
                            "filename": chosen.filename,
                            "reason": "missing on disk",
                            "paths": missing,
                        }
                    )
                    continue

    if failures:
        return False, "; ".join(failures), diag
    return True, f"All {len(expected_slots)} expected deployments have a valid BLS entry", diag


def validate_phase_transition(
    phase: str,
    status_data: str | dict[str, Any],
    baseline_digest: str | None = None,
    candidate_digest: str | None = None,
    candidate_image: str | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Validate lifecycle phase invariants against current bootc status."""
    deployments = parse_bootc_status(status_data)
    booted = deployments.get("booted")
    staged = deployments.get("staged")
    rollback = deployments.get("rollback")

    diag: dict[str, Any] = {
        "phase": phase,
        "booted": booted.to_dict() if booted else None,
        "staged": staged.to_dict() if staged else None,
        "rollback": rollback.to_dict() if rollback else None,
    }

    if phase == "baseline":
        if not booted:
            return False, "No booted deployment found in bootc status", diag
        if not booted.digest:
            return False, "Booted deployment is missing an image digest", diag
        return True, f"Baseline deployment active with digest {booted.digest}", diag

    elif phase == "staged":
        if not booted:
            return False, "No booted deployment active during staging", diag
        if baseline_digest and booted.digest != baseline_digest:
            return (
                False,
                f"Atomic guarantee violated: booted digest changed before reboot (expected {baseline_digest}, got {booted.digest})",
                diag,
            )
        if not staged:
            return False, "No staged deployment found after upgrade command", diag
        # The staged slot is what is under test, so the expectation must come
        # from outside it: the image reference the harness asked bootc to stage.
        if not candidate_image:
            return False, "Candidate image is required for staged phase validation", diag
        candidate_repo = image_repository(candidate_image)
        staged_repo = image_repository(staged.image)
        if staged_repo != candidate_repo:
            return (
                False,
                f"Staged image '{staged.image}' is not from candidate repository '{candidate_repo}'",
                diag,
            )
        # A digest-pinned candidate names exactly one image, so the staged
        # digest must be it; a tag is resolved by the registry at staging time.
        _, pinned, pinned_digest = candidate_image.strip().partition("@")
        if pinned and staged.digest != pinned_digest:
            return (
                False,
                f"Staged digest '{staged.digest}' does not match candidate digest '{pinned_digest}'",
                diag,
            )
        # bootc switch only stops early when the ref is unchanged; a different
        # ref that resolves to the booted digest is still staged, and every
        # later phase would then pass without anything having been upgraded.
        if baseline_digest and staged.digest == baseline_digest:
            return (
                False,
                f"Staged digest {staged.digest} is the baseline digest; nothing was upgraded",
                diag,
            )
        return True, f"Upgrade staged successfully with digest {staged.digest}", diag

    elif phase == "upgraded":
        if not booted:
            return False, "No booted deployment active after rebooting upgraded image", diag
        if not candidate_digest:
            return False, "Candidate digest is required for upgraded phase validation", diag
        if booted.digest != candidate_digest:
            return (
                False,
                f"Upgraded boot failed: booted digest '{booted.digest}' does not match candidate '{candidate_digest}'",
                diag,
            )
        if not rollback:
            return False, "Rollback deployment missing after upgrade", diag
        if baseline_digest and rollback.digest != baseline_digest:
            return (
                False,
                f"Rollback deployment digest '{rollback.digest}' does not point to baseline '{baseline_digest}'",
                diag,
            )
        return True, f"Upgraded deployment active with digest {booted.digest}", diag

    elif phase == "rollback":
        if not booted:
            return False, "No booted deployment active after rollback reboot", diag
        if baseline_digest and booted.digest != baseline_digest:
            return (
                False,
                f"Rollback verification failed: booted digest '{booted.digest}' does not match baseline '{baseline_digest}'",
                diag,
            )
        return True, f"Rollback successfully restored baseline deployment {booted.digest}", diag

    else:
        return False, f"Unknown lifecycle phase: {phase}", diag


def record_phase_diagnostics(
    output_dir: Path,
    phase: str,
    deployment_type: str,
    digest: str,
    status: str,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> Path:
    """Write structured diagnostics for a phase to output_dir."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    record = {
        "timestamp": timestamp,
        "phase": phase,
        "deployment_type": deployment_type,
        "digest": digest,
        "status": status,
        "reason": reason,
        "details": details or {},
    }

    record_path = output_dir / f"lifecycle-{phase}.json"
    record_path.write_text(json.dumps(record, indent=2) + "\n")
    return record_path


def format_failure_summary(
    phase: str,
    active_deployment: str,
    active_digest: str,
    expected_digest: str | None,
    reason: str,
) -> str:
    """Format human-readable failure diagnostics identifying deployment and digest."""
    lines = [
        "======================================================================",
        "                    BOOTC LIFECYCLE TEST FAILURE                      ",
        "======================================================================",
        f"Phase:             {phase}",
        f"Active Deployment: {active_deployment}",
        f"Active Digest:     {active_digest or '(unknown)'}",
    ]
    if expected_digest:
        lines.append(f"Expected Digest:   {expected_digest}")
    lines.extend([
        f"Failure Reason:    {reason}",
        "======================================================================",
    ])
    return "\n".join(lines)


def generate_lifecycle_summary(evidence_dir: Path) -> dict[str, Any]:
    """Aggregate phase records into a final lifecycle summary."""
    summary: dict[str, Any] = {
        "status": "PASS",
        "phases": {},
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    for phase in ("baseline", "staged", "upgraded", "rollback"):
        record_file = evidence_dir / f"lifecycle-{phase}.json"
        if record_file.is_file():
            data = json.loads(record_file.read_text())
            summary["phases"][phase] = data
            if data.get("status") != "PASS":
                summary["status"] = "FAIL"

    summary_file = evidence_dir / "lifecycle-summary.json"
    summary_file.write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # extract-digest
    p_extract = subparsers.add_parser("extract-digest", help="Extract digest for a given slot")
    p_extract.add_argument("--status", required=True, help="Path to status JSON file or '-' for stdin")
    p_extract.add_argument("--slot", default="booted", choices=["booted", "staged", "rollback"])

    # extract-image
    p_image = subparsers.add_parser("extract-image", help="Extract image reference for a given slot")
    p_image.add_argument("--status", required=True, help="Path to status JSON file or '-' for stdin")
    p_image.add_argument("--slot", default="booted", choices=["booted", "staged", "rollback"])
    p_image.add_argument(
        "--repository",
        action="store_true",
        help="Print only the repository portion, without tag or digest",
    )

    # image-repository
    p_repo = subparsers.add_parser(
        "image-repository", help="Normalize an image reference to its repository"
    )
    p_repo.add_argument("--ref", required=True, help="Image reference to normalize")

    # validate-phase
    p_val = subparsers.add_parser("validate-phase", help="Validate invariants for a lifecycle phase")
    p_val.add_argument("phase", choices=["baseline", "staged", "upgraded", "rollback"])
    p_val.add_argument("--status", required=True, help="Path to status JSON file or '-' for stdin")
    p_val.add_argument("--baseline-digest", help="Expected baseline image digest")
    p_val.add_argument("--candidate-digest", help="Expected candidate image digest")
    p_val.add_argument("--candidate-image", help="Candidate image reference that was staged")

    # validate-bootmgr
    p_bootmgr = subparsers.add_parser(
        "validate-bootmgr",
        help="Validate systemd-boot BLS entries against bootc status deployments",
    )
    p_bootmgr.add_argument(
        "--status",
        required=True,
        help="Path to bootc status JSON or '-' for stdin",
    )
    p_bootmgr.add_argument(
        "--listing",
        required=True,
        help="Path to a loader-entry listing (or '-' for stdin); the harness produces this by concatenating '=== ENTRY <path> ===' headers with the entry content and a trailing '=== END ===' sentinel",
    )
    p_bootmgr.add_argument(
        "--slots",
        default="booted,staged,rollback",
        help="Comma-separated slots to require (default: booted,staged,rollback)",
    )
    p_bootmgr.add_argument(
        "--output",
        type=Path,
        help="Optional path to write structured diagnostics JSON",
    )

    # record-diagnostics
    p_diag = subparsers.add_parser("record-diagnostics", help="Record structured diagnostics")
    p_diag.add_argument("--output-dir", required=True, type=Path)
    p_diag.add_argument("--phase", required=True)
    p_diag.add_argument("--deployment", required=True)
    p_diag.add_argument("--digest", required=True)
    p_diag.add_argument("--status", required=True, choices=["PASS", "FAIL", "IN_PROGRESS"])
    p_diag.add_argument("--reason")

    # summary
    p_sum = subparsers.add_parser("summary", help="Generate final lifecycle summary")
    p_sum.add_argument("--evidence-dir", required=True, type=Path)

    # failure-report
    p_fail = subparsers.add_parser("failure-report", help="Print formatted failure diagnostics")
    p_fail.add_argument("--phase", required=True)
    p_fail.add_argument("--deployment", required=True)
    p_fail.add_argument("--digest", required=True)
    p_fail.add_argument("--expected-digest")
    p_fail.add_argument("--reason", required=True)

    args = parser.parse_args(argv)

    if args.subcommand == "extract-digest":
        raw = sys.stdin.read() if args.status == "-" else Path(args.status).read_text()
        deployments = parse_bootc_status(raw)
        dep = deployments.get(args.slot)
        if not dep or not dep.digest:
            print(f"No digest found for slot {args.slot}", file=sys.stderr)
            return 1
        print(dep.digest)
        return 0

    elif args.subcommand == "extract-image":
        raw = sys.stdin.read() if args.status == "-" else Path(args.status).read_text()
        deployments = parse_bootc_status(raw)
        dep = deployments.get(args.slot)
        if not dep or not dep.image:
            print(f"No image reference found for slot {args.slot}", file=sys.stderr)
            return 1
        print(image_repository(dep.image) if args.repository else dep.image)
        return 0

    elif args.subcommand == "image-repository":
        repo = image_repository(args.ref)
        if not repo:
            print("Empty image reference", file=sys.stderr)
            return 1
        print(repo)
        return 0

    elif args.subcommand == "validate-phase":
        raw = sys.stdin.read() if args.status == "-" else Path(args.status).read_text()
        ok, msg, _diag = validate_phase_transition(
            phase=args.phase,
            status_data=raw,
            baseline_digest=args.baseline_digest,
            candidate_digest=args.candidate_digest,
            candidate_image=args.candidate_image,
        )
        if ok:
            print(f"PASS: {msg}")
            return 0
        else:
            print(f"FAIL: {msg}", file=sys.stderr)
            return 1

    elif args.subcommand == "validate-bootmgr":
        if args.status == "-" and args.listing == "-":
            print("--status and --listing cannot both read from stdin ('-')", file=sys.stderr)
            return 2
        status_raw = sys.stdin.read() if args.status == "-" else Path(args.status).read_text()
        listing_raw = sys.stdin.read() if args.listing == "-" else Path(args.listing).read_text()
        slots = tuple(s.strip() for s in args.slots.split(",") if s.strip())
        if not slots:
            print("At least one slot is required for validate-bootmgr", file=sys.stderr)
            return 2
        ok, msg, diag = validate_bootmgr_entries(status_raw, listing_raw, expected_slots=slots)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(diag, indent=2) + "\n")
        if ok:
            print(f"PASS: {msg}")
            return 0
        print(f"FAIL: {msg}", file=sys.stderr)
        return 1

    elif args.subcommand == "record-diagnostics":
        record_phase_diagnostics(
            output_dir=args.output_dir,
            phase=args.phase,
            deployment_type=args.deployment,
            digest=args.digest,
            status=args.status,
            reason=args.reason,
        )
        return 0

    elif args.subcommand == "summary":
        summary = generate_lifecycle_summary(args.evidence_dir)
        print(f"Lifecycle status: {summary.get('status')}")
        return 0 if summary.get("status") == "PASS" else 1

    elif args.subcommand == "failure-report":
        print(
            format_failure_summary(
                phase=args.phase,
                active_deployment=args.deployment,
                active_digest=args.digest,
                expected_digest=args.expected_digest,
                reason=args.reason,
            ),
            file=sys.stderr,
        )
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
