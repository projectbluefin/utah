#!/usr/bin/env python3
"""Drive a GTK application through AT-SPI, from inside the guest session.

The raw-image E2E uses this to operate the real OS-Installer and
gnome-initial-setup windows: find widgets by their accessible name, press
buttons through their accessible action, and move keyboard focus to entries so
the host can type into them with QEMU `sendkey` (Wayland gives AT-SPI no way to
synthesize keystrokes). Run it as the session's user with
DBUS_SESSION_BUS_ADDRESS pointing at that session's bus.

  atspi_drive.py snapshot APP              JSON list of showing nodes
  atspi_drive.py act APP NAME [ROLE]       press/activate a named widget
  atspi_drive.py focus APP NAME [ROLE]     move keyboard focus to it
  atspi_drive.py row APP TEXT              activate the list row containing TEXT

APP is a case-insensitive substring of the AT-SPI application name. NAME
matches the accessible name with mnemonic underscores removed, exactly
(case-insensitive) unless it starts with "~", which makes it a substring match.
`act` and `row` print {"how": "action:<name>"} when an accessible action ran, or
{"how": "focus"} when only focus could be moved -- then the caller presses
Enter/Space. Exit status 2 means no match.
"""

import json
import sys

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

MAX_NODES = 4000


def norm(text):
    return (text or "").replace("_", "").strip().lower()


def apps(substr):
    desktop = Atspi.get_desktop(0)
    found = []
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is not None and substr.lower() in (app.get_name() or "").lower():
            found.append(app)
    return found


def states(node):
    try:
        ss = node.get_state_set()
    except Exception:
        return set()
    names = set()
    for st in ("SHOWING", "VISIBLE", "SENSITIVE", "FOCUSED", "CHECKED", "EDITABLE", "SELECTED", "ENABLED"):
        if ss.contains(getattr(Atspi.StateType, st)):
            names.add(st.lower())
    return names


def text_of(node):
    try:
        iface = node.get_text_iface()
        if iface is not None:
            return Atspi.Text.get_text(node, 0, -1)
    except Exception:
        pass
    return ""


def actions_of(node):
    try:
        iface = node.get_action_iface()
        if iface is None:
            return []
        return [Atspi.Action.get_action_name(node, i) for i in range(Atspi.Action.get_n_actions(node))]
    except Exception:
        return []


def walk(node, path="0", depth=0, out=None):
    out = [] if out is None else out
    if node is None or len(out) >= MAX_NODES or depth > 60:
        return out
    st = states(node)
    out.append((node, path, st))
    if depth > 0 and "showing" not in st and "visible" not in st:
        return out
    try:
        n = node.get_child_count()
    except Exception:
        n = 0
    for i in range(n):
        try:
            child = node.get_child_at_index(i)
        except Exception:
            continue
        walk(child, f"{path}.{i}", depth + 1, out)
    return out


def showing_nodes(app_substr):
    found = apps(app_substr)
    if not found:
        print(json.dumps({"error": f"no application matching {app_substr!r}",
                          "apps": [a.get_name() for a in apps("")]}))
        sys.exit(3)
    nodes = []
    for app in found:
        nodes += [(n, p, s) for n, p, s in walk(app) if "showing" in s]
    return nodes


def describe(node, path, st):
    return {
        "path": path,
        "role": node.get_role_name(),
        "name": node.get_name() or "",
        "text": text_of(node)[:200],
        "states": sorted(st),
        "actions": actions_of(node),
    }


def matches(node, name, role):
    want = norm(name)
    got = norm(node.get_name())
    ok = want[1:] in got if want.startswith("~") else got == want
    return ok and (role is None or node.get_role_name() == role)


def press(node):
    acts = actions_of(node)
    for preferred in ("click", "press", "activate", "toggle", "jump"):
        if preferred in acts:
            Atspi.Action.do_action(node, acts.index(preferred))
            return f"action:{preferred}"
    if acts:
        Atspi.Action.do_action(node, 0)
        return f"action:{acts[0]}"
    Atspi.Component.grab_focus(node)
    return "focus"


def find(app, name, role):
    for node, path, st in showing_nodes(app):
        if matches(node, name, role) and "sensitive" in st:
            return node, path, st
    return None


def main(argv):
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 1
    cmd, app = argv[1], argv[2]
    if cmd == "snapshot":
        print(json.dumps([describe(*n) for n in showing_nodes(app)], indent=1))
        return 0
    if cmd in ("act", "focus"):
        role = argv[4] if len(argv) > 4 else None
        hit = find(app, argv[3], role)
        if hit is None:
            print(json.dumps({"error": f"no sensitive showing node named {argv[3]!r}"}))
            return 2
        node, path, _ = hit
        if cmd == "act":
            how = press(node)
        else:
            Atspi.Component.grab_focus(node)
            how = "focus"
        print(json.dumps({"how": how, "path": path, "role": node.get_role_name(), "name": node.get_name()}))
        return 0
    if cmd == "focus-path":
        for node, path, _ in showing_nodes(app):
            if path == argv[3]:
                Atspi.Component.grab_focus(node)
                print(json.dumps({"how": "focus", "path": path, "role": node.get_role_name()}))
                return 0
        print(json.dumps({"error": f"no showing node at {argv[3]}"}))
        return 2
    if cmd == "row":
        want = argv[3].lower()
        nodes = showing_nodes(app)
        by_path = {p: (n, s) for n, p, s in nodes}
        for node, path, st in nodes:
            label = f"{node.get_name() or ''} {text_of(node)}".lower()
            if want not in label:
                continue
            # Walk up to the nearest list row and activate that.
            parts = path.split(".")
            while parts:
                cand = by_path.get(".".join(parts))
                if cand and cand[0].get_role_name() in ("list item", "table row", "row", "list box item"):
                    how = press(cand[0])
                    print(json.dumps({"how": how, "path": ".".join(parts), "role": cand[0].get_role_name()}))
                    return 0
                parts.pop()
        print(json.dumps({"error": f"no row containing {argv[3]!r}"}))
        return 2
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
