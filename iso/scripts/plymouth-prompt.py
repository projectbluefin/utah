#!/usr/bin/env python3
"""Prove the LUKS passphrase prompt is Plymouth's branded graphical splash.

luks-unlock.py only has to find *a* prompt to type at. That is not the same as
the prompt a user sees: an installed Utah once booted every machine with a
serial port into Plymouth's text "details" fallback (#461), and a splash with
no watermark.png draws, unlocks and boots exactly as well as a branded one. So
this gate looks at the screen, the way a person at the machine would:

  - Plymouth's prompt text is on screen (tesseract finds "passphrase"), and
  - the theme's watermark is drawn where the theme puts it: the PNG the image
    ships, composited over the black splash background at the position the
    theme's WatermarkHorizontalAlignment/WatermarkVerticalAlignment give.

The text fallback fails fast: it is the only mode in which the prompt reaches
the serial console, so a "Please enter passphrase" line there is the verdict.

Usage:
  plymouth-prompt.py wait  <monitor-sock> <serial-log> <theme-file> <watermark.png> <evidence.ppm> [timeout-s]
  plymouth-prompt.py check <frame.ppm|.png> <theme-file> <watermark.png>

Exit codes: 0 branded graphical prompt; 1 none within the timeout (or bad
input); 3 text fallback; 4 graphical prompt without the expected watermark.
"""

import os
import re
import socket
import struct
import subprocess
import sys
import time
import zlib

POLL_INTERVAL = 3
SEARCH_RADIUS = 6         # pixels around the theme's computed watermark origin
MATCH_RATIO = 0.25        # best difference must be under a quarter of the
                          # watermark's own contrast against black
UNBRANDED_POLLS = 2       # a prompt without a watermark this many polls running fails
TEXT_FALLBACK = "Please enter passphrase for disk"


def read_ppm(data: bytes):
    fields, i = [], 2
    while len(fields) < 3:
        while data[i:i + 1].isspace():
            i += 1
        if data[i:i + 1] == b"#":
            i = data.index(b"\n", i)
            continue
        start = i
        while not data[i:i + 1].isspace():
            i += 1
        fields.append(int(data[start:i]))
    w, h, maxval = fields
    if maxval != 255:
        raise ValueError(f"unsupported PPM maxval {maxval}")
    pixels = data[i + 1:i + 1 + w * h * 3]
    if len(pixels) != w * h * 3:
        raise ValueError("truncated PPM")
    return w, h, 3, pixels


def read_png(data: bytes):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, header = 8, bytearray(), None
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
        pos += 12 + length
    w, h, depth, color, _, _, interlace = header
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color)
    if depth != 8 or interlace or channels is None:
        raise ValueError(f"unsupported PNG: depth {depth}, colour type {color}, interlace {interlace}")
    raw, stride = zlib.decompress(bytes(idat)), w * channels
    out, prev = bytearray(), bytearray(stride)
    for y in range(h):
        kind = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for x in range(stride):
            a = line[x - channels] if x >= channels else 0
            b = prev[x]
            c = prev[x - channels] if x >= channels else 0
            if kind == 1:
                line[x] = (line[x] + a) & 255
            elif kind == 2:
                line[x] = (line[x] + b) & 255
            elif kind == 3:
                line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif kind == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
            elif kind:
                raise ValueError(f"invalid PNG filter {kind}")
        out += line
        prev = line
    return w, h, channels, bytes(out)


def read_image(path: str):
    with open(path, "rb") as handle:
        data = handle.read()
    return read_ppm(data) if data[:2] == b"P6" else read_png(data)


def rgb(image):
    """(w, h, [(r, g, b) per pixel]) from any decoded image."""
    w, h, channels, pixels = image
    if channels >= 3:
        return w, h, [tuple(pixels[i:i + 3]) for i in range(0, len(pixels), channels)]
    return w, h, [(v, v, v) for v in pixels[::channels]]


def alignment(theme_text: str):
    def value(key):
        match = re.search(rf"^{key}\s*=\s*([0-9.]+)\s*$", theme_text, re.M)
        if not match:
            raise ValueError(f"theme declares no {key}")
        return float(match.group(1))
    return value("WatermarkHorizontalAlignment"), value("WatermarkVerticalAlignment")


def watermark_match(frame, watermark, theme_text: str):
    """(matched, best_difference, contrast, (x, y)) for the watermark on the frame.

    The watermark is composited over black, Plymouth's splash background, and
    compared at every offset within SEARCH_RADIUS of where two-step places it:
    alignment * (screen - image) on each axis. contrast is the template's own
    difference from a black frame, so the ratio is independent of the art.
    """
    fw, fh, frame_px = rgb(frame)
    ww, wh, channels, raw = watermark
    template = []
    for y in range(wh):
        for x in range(ww):
            i = (y * ww + x) * channels
            alpha = raw[i + 3] if channels == 4 else 255
            px = raw[i:i + 3] if channels >= 3 else bytes([raw[i]] * 3)
            template.append((x, y, tuple(v * alpha // 255 for v in px)))
    contrast = sum(sum(px) for _, _, px in template) / (3 * len(template))
    if contrast < 8:
        raise ValueError("watermark is too dark to tell from a black frame")
    halign, valign = alignment(theme_text)
    ox, oy = round(halign * (fw - ww)), round(valign * (fh - wh))
    best, where = None, None
    for dy in range(-SEARCH_RADIUS, SEARCH_RADIUS + 1):
        for dx in range(-SEARCH_RADIUS, SEARCH_RADIUS + 1):
            x0, y0 = ox + dx, oy + dy
            if x0 < 0 or y0 < 0 or x0 + ww > fw or y0 + wh > fh:
                continue
            limit = best if best is not None else float("inf")
            total, cap = 0, limit * 3 * len(template)
            for x, y, want in template:
                got = frame_px[(y0 + y) * fw + x0 + x]
                total += abs(got[0] - want[0]) + abs(got[1] - want[1]) + abs(got[2] - want[2])
                if total > cap:
                    break
            score = total / (3 * len(template))
            if best is None or score < best:
                best, where = score, (x0, y0)
    if best is None:
        raise ValueError(f"a {ww}x{wh} watermark does not fit a {fw}x{fh} frame")
    return best < MATCH_RATIO * contrast, best, contrast, where


def ocr(frame_path: str) -> str:
    result = subprocess.run(["tesseract", frame_path, "stdout"],
                            capture_output=True, text=True, check=False)
    return result.stdout


def monitor(sock_path: str, command: str) -> None:
    """Run one HMP command and return once QEMU has prompted again.

    screendump writes its file synchronously before the next "(qemu)" prompt,
    so waiting for it means the frame is complete when this returns.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(30)
        client.connect(sock_path)
        seen = b""
        while b"(qemu)" not in seen:
            seen += client.recv(4096) or b"(qemu)"
        client.sendall(command.encode() + b"\n")
        seen = b""
        while b"(qemu)" not in seen:
            chunk = client.recv(4096)
            if not chunk:
                break
            seen += chunk


def serial_has_text_prompt(serial_log: str) -> bool:
    try:
        with open(serial_log, errors="replace") as handle:
            return TEXT_FALLBACK in handle.read()
    except OSError:
        return False


def describe(matched, best, contrast, where):
    verdict = "matched" if matched else "absent"
    return f"watermark {verdict} (difference {best:.1f} vs contrast {contrast:.1f} at {where})"


def wait(sock, serial_log, theme_path, watermark_path, evidence, timeout):
    theme_text = open(theme_path).read()
    watermark = read_image(watermark_path)
    # QEMU runs daemonized with "/" as its working directory, so a relative
    # screendump path would land somewhere else entirely.
    frame_path = os.path.abspath(evidence) + ".poll.ppm"
    deadline = time.monotonic() + timeout
    unbranded = 0
    while time.monotonic() < deadline:
        if serial_has_text_prompt(serial_log):
            print("[plymouth-prompt] FAIL: the passphrase prompt reached the serial console --"
                  " Plymouth fell back to its text details mode instead of the graphical splash",
                  file=sys.stderr)
            return 3
        try:
            os.unlink(frame_path)
        except FileNotFoundError:
            pass
        try:
            monitor(sock, f"screendump {frame_path}")
            frame = read_image(frame_path)
        except (OSError, ValueError):
            time.sleep(POLL_INTERVAL)
            continue
        if re.search(r"passphrase", ocr(frame_path), re.I):
            result = watermark_match(frame, watermark, theme_text)
            print(f"[plymouth-prompt] prompt on screen; {describe(*result)}", flush=True)
            os.replace(frame_path, evidence)
            if result[0]:
                return 0
            unbranded += 1
            if unbranded >= UNBRANDED_POLLS:
                print(f"[plymouth-prompt] FAIL: the graphical prompt has no {watermark_path}"
                      " watermark -- the splash is not Bluefin-branded", file=sys.stderr)
                return 4
        time.sleep(POLL_INTERVAL)
    if os.path.exists(frame_path):
        os.replace(frame_path, evidence)
    print(f"[plymouth-prompt] FAIL: no graphical passphrase prompt within {timeout}s;"
          f" last frame: {evidence}", file=sys.stderr)
    return 1


def check(frame_path, theme_path, watermark_path):
    result = watermark_match(read_image(frame_path), read_image(watermark_path),
                             open(theme_path).read())
    print(describe(*result))
    return 0 if result[0] else 4


def main(argv):
    if len(argv) >= 7 and argv[1] == "wait":
        timeout = int(argv[7]) if len(argv) > 7 else 300
        return wait(argv[2], argv[3], argv[4], argv[5], argv[6], timeout)
    if len(argv) == 5 and argv[1] == "check":
        return check(argv[2], argv[3], argv[4])
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
