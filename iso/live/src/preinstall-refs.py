#!/usr/bin/python3
"""Read effective preinstall refs using Flatpak's per-group merge order."""
import argparse
import configparser
from pathlib import Path


def refs(directories: list[Path], arch: str) -> list[str]:
    entries: dict[str, dict[str, str]] = {}
    for directory in directories:
        for path in sorted(directory.glob("*.preinstall")):
            if not path.is_file() or path.is_symlink():
                continue
            parser = configparser.ConfigParser(interpolation=None, strict=False,
                                               default_section="")
            parser.optionxform = str
            parser.read_string(path.read_text())
            for section in parser.sections():
                if section.startswith("Flatpak Preinstall "):
                    name = section.removeprefix("Flatpak Preinstall ")
                    entries.setdefault(name, {}).update(parser[section])
    result = []
    for name, values in entries.items():
        if not name or values.get("Install", "true").lower() in {"false", "0"}:
            continue
        kind = "runtime" if values.get("IsRuntime", "false").lower() in {"true", "1"} else "app"
        branch = values.get("Branch", "master") or "master"
        result.append(f"{kind}/{name}/{arch}/{branch}")
    return sorted(result)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--arch", required=True)
    cli.add_argument("directories", nargs="*", type=Path,
                     default=[Path("/usr/share/flatpak/preinstall.d"),
                              Path("/etc/flatpak/preinstall.d")])
    args = cli.parse_args()
    print("\n".join(refs(args.directories, args.arch)))
