#!/usr/bin/env python3
"""Compare the image-based installer run with the ISO/fisherman path.

  compare_paths.py --repo OWNER/REPO --image utah --digest sha256:... \\
      --evidence E2E_EVIDENCE_DIR --raw-evidence RAW_EVIDENCE_DIR \\
      [--sizes medium-size.json ...] [--image-build-seconds N] \\
      --out comparison.json [--summary FILE]

The image-based side comes from this run: raw-evidence/medium.json and
shrink.json (build-raw-image.sh, shrink-raw-image.sh), the measure_medium.py
outputs, and e2e evidence/results.json and utah-install.log (raw_e2e.py).

The fisherman side is the latest Post-Testing E2E `luks` job for the same
image and digest, read through `gh api` (GH_TOKEN): the job's conclusion,
its `=== Phase N/7` markers (install time is phase 3 to phase 4), the `ISO
ready` line, the "Compose disposable debug ISO" step's duration and the
`production-iso-<image>` artifact size from the same run.

GNOME OS's published ISO sizes are read with HEAD requests, as a reference.

The comparison is written as JSON (--out) and as Markdown (stdout, and
appended to --summary, e.g. $GITHUB_STEP_SUMMARY).
"""

import argparse
import json
import re
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

E2E_WORKFLOW = "post-testing-e2e.yml"
LUKS_PHASES = {
    1: "live boot",
    2: "live desktop",
    3: "install",
    4: "boot installed disk",
    5: "Plymouth LUKS unlock",
    6: "graphical target, offline payload",
    7: "login, desktop, trailing checks",
}
ISO_STEP = "Compose disposable debug ISO"
GNOME_OS_ISOS = {
    "GNOME OS nightly": "https://os.gnome.org/download/latest/gnome_os_x86_64.iso",
    "GNOME OS stable": "https://os.gnome.org/download/stable/50/gnome_os_50.0-x86_64.iso",
}
PHASE_RE = re.compile(r"^(\S+)\s+=== Phase (\d)/7:")
ERROR_RE = re.compile(r"^(\S+)\s+##\[error\]Process completed with exit code (\d+)")
ISO_READY_RE = re.compile(r"ISO ready: \S+ \(([^)]+)\)")
INSTALLED_RE = re.compile(r"Utah installed to \S+ in (\d+)s")
BLOCK_COPY_RE = re.compile(r"Block copy finished in (\d+)s")


def ts(stamp):
    """Seconds since the epoch for a GitHub log or API timestamp."""
    stamp = stamp.rstrip("Z")
    if "." in stamp:
        head, frac = stamp.split(".", 1)
        stamp = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(stamp + "+00:00").timestamp()


def parse_luks_log(text):
    """Phase markers, the ISO size line and the failing exit code of a luks job log."""
    phases, iso_size, error = {}, None, None
    for line in text.splitlines():
        line = line.lstrip("\ufeff")
        if m := PHASE_RE.match(line):
            phases.setdefault(int(m.group(2)), ts(m.group(1)))
        elif m := ERROR_RE.match(line):
            error = error or (ts(m.group(1)), int(m.group(2)))
        if iso_size is None and (m := ISO_READY_RE.search(line)):
            iso_size = m.group(1)
    return {"phases": phases, "iso_size": iso_size, "error": error}


def luks_stages(parsed, conclusion):
    """Phases 1-7 as pass/fail/not run."""
    reached = max(parsed["phases"], default=0)
    out = []
    for n, name in LUKS_PHASES.items():
        if conclusion == "success":
            status = "pass"
        elif n < reached:
            status = "pass"
        elif n == reached:
            status = "fail"
        else:
            status = "not run"
        out.append({"stage": f"{n}. {name}", "status": status})
    return out


def luks_install_seconds(parsed):
    phases = parsed["phases"]
    if 3 in phases and 4 in phases:
        return round(phases[4] - phases[3]), "pass"
    if 3 in phases and parsed["error"]:
        return round(parsed["error"][0] - phases[3]), "fail"
    return None, "not run"


def step_seconds(job, name):
    for step in job.get("steps") or []:
        if step.get("name") == name and step.get("started_at") and step.get("completed_at"):
            return round(ts(step["completed_at"]) - ts(step["started_at"]))
    return None


def match_luks_job(jobs, image, digest):
    """The luks matrix job of image for digest ("luks (IMAGE, sha256:..., REF)")."""
    hexpart = digest.split(":", 1)[-1][:32]
    prefix = f"luks ({image}, sha256:"
    for job in jobs:
        name = job.get("name", "")
        if name.startswith(prefix) and hexpart and hexpart in name:
            return job
    return None


def fisherman_metrics(run, job, log_text, artifacts, image):
    parsed = parse_luks_log(log_text)
    install_s, install_status = luks_install_seconds(parsed)
    artifact = next((a for a in artifacts if a.get("name") == f"production-iso-{image}"), None)
    return {
        "run": run["id"], "run_url": run.get("html_url"), "job": job["id"], "job_url": job.get("html_url"),
        "conclusion": job.get("conclusion"),
        "iso_size": parsed["iso_size"],
        "production_iso_artifact_bytes": artifact.get("size_in_bytes") if artifact else None,
        "iso_build_seconds": step_seconds(job, ISO_STEP),
        "install_seconds": install_s, "install_status": install_status,
        "exit_code": parsed["error"][1] if parsed["error"] else None,
        "stages": luks_stages(parsed, job.get("conclusion")),
        "selinux": {"live": "Permissive (enforcing=0 on the ISO entry, #22)", "installed": "not recorded"},
        "failed_units": "not recorded (the luks job checks the contract's services instead)",
    }


def gh_api(path, raw=False):
    cmd = ["gh", "api", path] + (["--allow-escape-sequences"] if raw else [])
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return out if raw else json.loads(out)


def fetch_fisherman(repo, image, digest, max_runs=60, api=gh_api):
    runs = api(f"repos/{repo}/actions/workflows/{E2E_WORKFLOW}/runs?per_page={max_runs}")["workflow_runs"]
    for run in runs:
        if run.get("status") != "completed":
            continue
        jobs = api(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
        job = match_luks_job(jobs, image, digest)
        if job is None:
            continue
        log_text = api(f"repos/{repo}/actions/jobs/{job['id']}/logs", raw=True)
        artifacts = api(f"repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]
        return fisherman_metrics(run, job, log_text, artifacts, image)
    return None


def head_size(url, timeout=20):
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.headers.get("Content-Length") or 0) or None
    except (OSError, ValueError):
        return None


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def image_metrics(evidence, raw_evidence, sizes, image_build_seconds):
    results = read_json(Path(evidence) / "results.json") or {}
    medium = read_json(Path(raw_evidence) / "medium.json") or {}
    shrink = read_json(Path(raw_evidence) / "shrink.json") or {}
    flatpaks = read_json(Path(raw_evidence) / "flatpaks.json") or {}
    backend = read_json(Path(raw_evidence) / "backend.json") or results.get("backend")
    log_path = Path(evidence) / "utah-install.log"
    log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
    installed = INSTALLED_RE.search(log_text)
    copied = BLOCK_COPY_RE.search(log_text)
    timings = results.get("timings") or {}
    return {
        "result": results.get("result", "not run"),
        "error": results.get("error"),
        "install": results.get("install"),
        "backend": backend,
        "sizes": [s for s in (read_json(p) for p in sizes) if s],
        "shrink": shrink or None,
        "image_build_seconds": image_build_seconds,
        "bootc_install_seconds": medium.get("bootc_install_seconds"),
        "install_seconds": int(installed.group(1)) if installed else None,
        "block_copy_seconds": int(copied.group(1)) if copied else None,
        "gui_confirm_to_done_seconds": timings.get("gui-install-seconds-after-confirm"),
        "bootc_switch_seconds": timings.get("bootc-switch-seconds"),
        "stages": results.get("stages") or [],
        "boots": results.get("boots") or {},
        "flatpaks": {"baked": flatpaks or None, "checks": results.get("flatpaks") or {}},
    }


def gib(n):
    return "n/a" if not n else f"{n / 2**30:.2f} GiB"


def secs(n):
    if n is None:
        return "n/a"
    return f"{n // 60} min {n % 60:02d} s" if n >= 60 else f"{n} s"


def backend_cell(decision):
    if not decision:
        return "n/a"
    text = f"**{decision.get('backend')}**"
    if decision.get("requested"):
        text += f" (requested {decision['requested']}; kernel {decision.get('kernel')}: {decision.get('reason')})"
    return text


def flatpak_cell(flatpaks):
    baked, checks = flatpaks.get("baked"), flatpaks.get("checks") or {}
    if not baked:
        return "none baked"
    text = f"{baked['apps']} apps, {gib(baked['bytes'])} in the medium's /var (baked in {secs(baked['seconds'])})"
    if checks:
        problems = [p for c in checks.values() for p in c.get("problems", [])]
        persisted = ", a removed app stayed removed through switch and rollback" if "after bootc rollback" in checks else ""
        text += "; " + ("; ".join(problems) if problems else f"all present on {', '.join(checks)}{persisted}")
    return text


def render(comparison):
    img, fm = comparison["image_based"], comparison["fisherman"]
    lines = [f"## Image-based installer vs ISO/fisherman: `{comparison['image']}` @ `{comparison['digest'][:19]}`", ""]
    if fm:
        lines.append(f"Fisherman side: Post-Testing E2E run [{fm['run']}]({fm['run_url']}), "
                     f"[luks job]({fm['job_url']}) ({fm['conclusion']}).")
    else:
        lines.append("Fisherman side: no Post-Testing E2E `luks` job found for this digest.")
    lines += ["", "| Metric | Image-based (this run) | ISO/fisherman |", "|---|---|---|"]
    fm = fm or {}
    final = img["sizes"][-1] if img["sizes"] else {}
    zstd3 = (final.get("formats") or {}).get("zstd-3", {}).get("bytes")
    iso = fm.get("production_iso_artifact_bytes")
    iso_text = f"{fm.get('iso_size') or 'n/a'} (production ISO artifact {gib(iso)})"
    lines += [
        f"| Result | {img['result']} | {fm.get('conclusion') or 'n/a'} |",
        f"| bootc backend | {backend_cell(img.get('backend'))} | ostree (composefs is opt-in, #1129) |",
        f"| Medium, raw | {gib(final.get('apparent_bytes'))} (allocated {gib(final.get('allocated_bytes'))}) | {iso_text} |",
        f"| Medium, zstd -3 | {gib(zstd3)} | ISO is already compressed |",
        f"| Build time | image layer {secs(img['image_build_seconds'])} + bootc install {secs(img['bootc_install_seconds'])} | ISO compose {secs(fm.get('iso_build_seconds'))} |",
        f"| Install time | {secs(img['install_seconds'])} install.sh (block copy {secs(img['block_copy_seconds'])}; GUI confirm-to-done {secs(img['gui_confirm_to_done_seconds'])}) | {secs(fm.get('install_seconds'))} phase 3 to 4 ({fm.get('install_status', 'n/a')}) |",
    ]
    boots = img["boots"]
    selinux = ", ".join(f"{k}: {v['selinux']}" for k, v in boots.items()) or "n/a"
    failed = ", ".join(f"{k}: {', '.join(v['failed_units']) or 'none'}" for k, v in boots.items()) or "n/a"
    fm_selinux = fm.get("selinux") or {}
    lines += [
        f"| SELinux | {selinux} | live: {fm_selinux.get('live', 'n/a')}; installed: {fm_selinux.get('installed', 'n/a')} |",
        f"| Failed units | {failed} | {fm.get('failed_units', 'n/a')} |",
        f"| Offline Flatpaks | {flatpak_cell(img.get('flatpaks') or {})} | baked into the ISO's live squashfs; present offline on the installed disk (luks trailing check) |",
        "",
        "| Image-based stage | Result | Fisherman phase | Result |",
        "|---|---|---|---|",
    ]
    left = [(s["stage"], s["status"] + (f" ({secs(s['seconds'])})" if s.get("seconds") is not None else "")) for s in img["stages"]]
    right = [(s["stage"], s["status"]) for s in fm.get("stages") or []]
    for i in range(max(len(left), len(right))):
        a = left[i] if i < len(left) else ("", "")
        b = right[i] if i < len(right) else ("", "")
        lines.append(f"| {a[0]} | {a[1]} | {b[0]} | {b[1]} |")
    lines += ["", "### Medium size, step by step", "",
              "| Step | Raw (dd writes) | Allocated (sparse) | zstd -3 | zstd -19 --long | xz -9 |", "|---|---|---|---|---|---|"]
    shrink = img.get("shrink")
    if shrink:
        lines.append(f"| after bootc install (before shrinking) | {gib(shrink['before_apparent_bytes'])} | {gib(shrink['before_allocated_bytes'])} | | | |")
    for s in img["sizes"]:
        f = s.get("formats") or {}

        def cell(name):
            d = f.get(name)
            return f"{gib(d['bytes'])} (pack {secs(round(d['compress_seconds']))}, unpack {secs(round(d['decompress_seconds']))})" if d else ""
        lines.append(f"| {s.get('label') or 'medium'} | {gib(s['apparent_bytes'])} | {gib(s['allocated_bytes'])} | "
                     f"{cell('zstd-3')} | {cell('zstd-19-long')} | {cell('xz-9')} |")
    refs = comparison.get("references") or {}
    if refs:
        lines += ["", "References: " + "; ".join(f"{k} ISO {gib(v)}" for k, v in refs.items())]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--digest", required=True)
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--raw-evidence", required=True)
    ap.add_argument("--sizes", nargs="*", default=[])
    ap.add_argument("--image-build-seconds", type=int)
    ap.add_argument("--out", required=True)
    ap.add_argument("--summary")
    args = ap.parse_args()
    try:
        fisherman = fetch_fisherman(args.repo, args.image, args.digest)
    except (subprocess.CalledProcessError, KeyError, ValueError) as e:
        print(f"could not read the fisherman side: {e}")
        fisherman = None
    comparison = {
        "image": args.image, "digest": args.digest,
        "image_based": image_metrics(args.evidence, args.raw_evidence, args.sizes, args.image_build_seconds),
        "fisherman": fisherman,
        "references": {k: v for k, v in ((k, head_size(u)) for k, u in GNOME_OS_ISOS.items()) if v},
    }
    Path(args.out).write_text(json.dumps(comparison, indent=1) + "\n")
    md = render(comparison)
    print(md)
    if args.summary:
        with open(args.summary, "a") as f:
            f.write(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
