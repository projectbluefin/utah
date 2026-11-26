#!/usr/bin/env python3
"""A minimal VTE terminal window for E2E screenshots.

Utah's terminal (Ghostty) is a Flatpak that the raw image does not carry
offline, while VTE for GTK 4 is in the image. This opens one terminal window
running COMMAND in bash and keeps a shell open afterwards, so a screendump
shows the command's real output in a real terminal. That shell skips the
rc files: Bluefin's /etc/profile.d/uwelcome.sh banner, which /etc/bashrc
prints in every interactive shell, otherwise scrolls the top of COMMAND's
output out of the window (fastfetch's kernel, desktop and shell lines, the
ones fastfetch-ocr-match.sh reads; Post-Testing E2E run 45858480299).

  vte_term.py TITLE COMMAND
"""

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Vte", "3.91")
from gi.repository import GLib, Gtk, Pango, Vte  # noqa: E402

title, command = sys.argv[1], sys.argv[2]
loop = GLib.MainLoop()
window = Gtk.Window(title=title, default_width=1280, default_height=760)
term = Vte.Terminal()
term.set_font(Pango.FontDescription.from_string("Monospace 12"))
term.set_scrollback_lines(2000)
window.set_child(term)
window.connect("close-request", lambda *_: (loop.quit(), False)[1])
term.spawn_async(Vte.PtyFlags.DEFAULT, GLib.get_home_dir(),
                 ["/bin/bash", "-c", f"{command}; echo; echo UTAH-E2E-TERMINAL-DONE; exec bash --norc"],
                 None, GLib.SpawnFlags.DEFAULT, None, None, -1, None, None)
window.present()
loop.run()
