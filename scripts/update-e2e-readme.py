#!/usr/bin/env python3
"""Embed a passing CI record without copying an old source README over main."""
import json
from pathlib import Path
import re


def update(text, provenance):
    sha, run = provenance["source_sha"], provenance["e2e_run"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or not re.fullmatch(r"[0-9]+", run):
        raise ValueError("invalid provenance")
    begin, end = "<!-- BEGIN E2E VERIFICATION -->", "<!-- END E2E VERIFICATION -->"
    block = (f"{begin}\n"
             "[![Verified ISO desktop](docs/verification/screenshots/installed-fastfetch.png)]"
             "(docs/verification/README.md)\n\n"
             f"*LUKS ISO test passed for commit `{sha[:12]}`. "
             f"[CI run](https://github.com/projectbluefin/utah/actions/runs/{run}); "
             "[screenshots and provenance](docs/verification/README.md).*\n"
             f"{end}")
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.S)
    if pattern.search(text):
        return pattern.sub(lambda _: block, text, count=1)
    title, separator, rest = text.partition("\n")
    return title + separator + "\n" + block + "\n" + rest


if __name__ == "__main__":
    provenance = json.loads(Path("docs/verification/provenance.json").read_text())
    if not Path("docs/verification/screenshots/installed-fastfetch.png").is_file():
        raise SystemExit("missing verified screenshot")
    path = Path("README.md")
    path.write_text(update(path.read_text(), provenance))
