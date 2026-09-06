#!/usr/bin/env python3
"""The image flavors Utah builds, in the shapes each workflow needs.

There are four places that have to agree about this: the build matrix, the
promote matrix, the release matrix, and whether the kernel cache image is worth
building at all. They were four separate literals, so narrowing the set in one
would have left the others promoting images that no longer exist.

config/flavors.json is the single source. "retired" is not commentary -- it is
what a flavor moves to when it is switched off, so the reason it is off stays
next to the list rather than in a commit message.

    flavors.py list       ["main"]
    flavors.py images     [{"image": "utah"}, ...]         promote
    flavors.py releases   [{"image": "utah", "source_tag": ...}, ...]  release
    flavors.py needs-kernel   true / false
    flavors.py list-main      ["main"]            flavors that build on the pristine base
    flavors.py list-kernel    ["nvidia", ...]     flavors that build on the kernel cache
    flavors.py count          4                   how many images a green build produces
    flavors.py image FLAVOR   utah-nvidia         the published image name for one flavor
"""
import json
import sys
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "config" / "flavors.json"
IMAGE = {
    "main": "utah",
    "nvidia": "utah-nvidia",
    "gaming": "utah-gaming",
    "nvidia-gaming": "utah-nvidia-gaming",
}

flavors = json.loads(CONFIG.read_text())["flavors"]
unknown = [f for f in flavors if f not in IMAGE]
if unknown:
    raise SystemExit(f"unknown flavor(s) in {CONFIG.name}: {', '.join(unknown)}")

what = sys.argv[1] if len(sys.argv) > 1 else "list"
if what == "list":
    print(json.dumps(flavors))
elif what == "images":
    print(json.dumps([{"image": IMAGE[f]} for f in flavors]))
elif what == "releases":
    print(json.dumps([{"image": IMAGE[f], "source_tag": "testing", "target_tag": "stable"}
                      for f in flavors]))
elif what == "needs-kernel":
    # Only the OGC and NVIDIA flavors consume the kernel cache image. With
    # neither in the set, building it is 45 minutes spent on nothing.
    print("true" if any(f != "main" for f in flavors) else "false")
elif what == "list-main":
    # main is built on the pristine Hummingbird base and never waits for the
    # kernel cache. The build workflow submits it separately so a cache miss
    # (about twenty minutes of kernel compile) delays only the flavors that
    # actually consume the result.
    print(json.dumps([f for f in flavors if f == "main"]))
elif what == "list-kernel":
    print(json.dumps([f for f in flavors if f != "main"]))
elif what == "count":
    # The number of images a green build publishes. post-testing-e2e refuses to
    # advance :testing unless it collected exactly this many digests -- a real
    # gate, but it used to be the literal 4, so narrowing or widening the flavor
    # set here stalled the promotion of every image with nothing to look at
    # except a build that had already gone green.
    print(len(flavors))
elif what == "image":
    if len(sys.argv) < 3:
        raise SystemExit("usage: flavors.py image FLAVOR")
    flavor = sys.argv[2]
    if flavor not in flavors:
        raise SystemExit(f"unknown Utah image flavor: {flavor}")
    print(IMAGE[flavor])
else:
    raise SystemExit(f"unknown query: {what}")
