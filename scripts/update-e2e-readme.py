#!/usr/bin/env python3
"""Embed a passing CI record without copying an old source README over main."""
import json
from pathlib import Path
import re


def update(text, provenance, record_commit="verification"):
    sha, run = provenance["source_sha"], provenance["e2e_run"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or not re.fullmatch(r"[0-9]+", run):
        raise ValueError("invalid provenance")
    begin, end = "<!-- BEGIN E2E VERIFICATION -->", "<!-- END E2E VERIFICATION -->"
    # Pin every evidence URL to the record's own commit. `verification` is a
    # single-commit orphan force-pushed every run, so `raw/verification/...`
    # always resolves to the latest screenshot while the commit and CI run
    # below are frozen from provenance.json - that pairs a fixed claim with the
    # next run's evidence until the link PR merges. The block's links already
    # resolve to the record, so read its identity from that record instead.
    block = (f"{begin}\n"
             f"[![Verified ISO desktop](https://github.com/projectbluefin/utah/raw/{record_commit}/screenshots/installed-fastfetch.png)]"
             f"(https://github.com/projectbluefin/utah/blob/{record_commit}/README.md)\n\n"
             f"*LUKS ISO test passed for commit `{sha[:12]}`. "
             f"[CI run](https://github.com/projectbluefin/utah/actions/runs/{run}) "
             "also runs independent desktop tests that do not gate this record; "
             "the LUKS and production ISO jobs passed; "
             f"[screenshots and provenance](https://github.com/projectbluefin/utah/tree/{record_commit}).*\n"
             f"{end}")
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.S)
    if pattern.search(text):
        return pattern.sub(lambda _: block, text, count=1)
    title, separator, rest = text.partition("\n")
    return title + separator + "\n" + block + "\n" + rest


if __name__ == "__main__":
    import os
    import sys
    record = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/verification")
    provenance = json.loads((record / "provenance.json").read_text())
    if not (record / "screenshots/installed-fastfetch.png").is_file():
        raise SystemExit("missing verified screenshot")
    record_commit = provenance.get("record_commit") or os.environ.get(
        "RECORD_COMMIT", "verification")
    path = Path("README.md")
    path.write_text(update(path.read_text(), provenance, record_commit))
