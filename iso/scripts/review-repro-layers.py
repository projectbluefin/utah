#!/usr/bin/env python3
"""Compare metadata and file bytes in the first differing native overlay layer."""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys


def snapshot(directory):
    result = {}
    for root, directories, files in os.walk(directory):
        for name in sorted(directories + files):
            path = Path(root) / name
            info = path.lstat()
            record = {"mode": info.st_mode, "uid": info.st_uid, "gid": info.st_gid,
                      "mtime_ns": info.st_mtime_ns, "size": info.st_size}
            if stat.S_ISLNK(info.st_mode):
                record["target"] = os.readlink(path)
            elif stat.S_ISREG(info.st_mode):
                with path.open("rb") as stream:
                    record["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
            record["xattrs"] = {key: os.getxattr(path, key, follow_symlinks=False).hex()
                               for key in sorted(os.listxattr(path, follow_symlinks=False))}
            result[str(path.relative_to(directory))] = record
    return result


def main():
    graph = Path(sys.argv[1])
    images = [json.loads(Path(name).read_text())[0] for name in sys.argv[2:4]]
    layers = [item["RootFS"]["Layers"] for item in images]
    assert len(layers[0]) == len(layers[1]), "different layer counts"
    index = next((i for i, pair in enumerate(zip(*layers)) if pair[0] != pair[1]), None)
    if index is None:
        print(json.dumps({"equal": True}))
        return
    records = json.loads((graph / "overlay-layers/layers.json").read_text())
    directories = []
    for digest in (layers[0][index], layers[1][index]):
        layer = next(row for row in records if row.get("diff-digest") == digest)
        directories.append(graph / "overlay" / layer["id"] / "diff")
    left, right = map(snapshot, directories)
    changed = {name: {"a": left.get(name), "b": right.get(name)}
               for name in sorted(left.keys() | right.keys()) if left.get(name) != right.get(name)}
    print(json.dumps({"first_different_layer": index + 1, "differences": changed}, indent=2))


if __name__ == "__main__":
    main()
