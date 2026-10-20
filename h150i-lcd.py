#!/usr/bin/env python3
"""
h150i-lcd.py — push text or an image to a Corsair H150i ELITE LCD XT screen.

No background service. Each run opens the LCD's USB HID interface, sends one
frame (or a brightness/rotation command), and exits. The screen holds the last
frame until something changes it.

The LCD only accepts JPEG image frames, so "display text" works by rendering
the text to a 480x480 image and uploading that.

Protocol (frame upload, brightness, rotation) is reproduced from the
OpenLinkHub Commander Core driver. The LCD on the ELITE LCD XT is a *separate*
HID device from the pump/fan controller (which is why `liquidctl list` doesn't
show it) — its product id is 0x0C39 or 0x0C33 on Corsair's vendor id 0x1B1C.

Requirements:
    sudo apt install libhidapi-hidraw0 python3-pil
    pip install hid            # the 'hid' / hidapi binding

Permissions: hidraw access needs root, OR a udev rule so your user can write to
the device without sudo. Example rule (then re-plug or reboot):

    # /etc/udev/rules.d/99-corsair-lcd.rules
    SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1b1c", ATTRS{idProduct}=="0c39", MODE="0660", TAG+="uaccess"
    SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1b1c", ATTRS{idProduct}=="0c33", MODE="0660", TAG+="uaccess"

Usage:
    ./h150i-lcd.py detect
    ./h150i-lcd.py text "Hello 42C"
    ./h150i-lcd.py text "CPU OK" --fg 00e070 --bg 101015 --size 96
    ./h150i-lcd.py image photo.png
    sudo ./h150i-lcd.py power --watch     # CPU + GPU watts (CPU sensor is root-only)
    ./h150i-lcd.py brightness 80          # 0-100
    ./h150i-lcd.py rotate 0               # 0,1,2,3  (up, right, down, left)

NOTE: the product ids and frame format match the ELITE LCD XT family, but
Corsair occasionally varies ids. Run `detect` first; if it finds nothing, run
`lsusb -d 1b1c:` and tell me the LCD's id so this can be adjusted.
"""

import argparse
import glob
import io
import json
import math
import os
import struct
import sys
import time
from datetime import datetime

try:
    import hid
except ImportError:
    sys.exit("Missing 'hid'. Install with: pip install hid  (and apt install libhidapi-hidraw0)")

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Missing Pillow. Install with: pip install Pillow  (or apt install python3-pil)")

VENDOR_ID = 0x1B1C
LCD_PRODUCT_IDS = (0x0C39, 0x0C33)   # ELITE LCD XT screen module candidates
IMG_W = IMG_H = 480

# Frame transfer constants (Commander Core LCD protocol)
LCD_BUFFER_SIZE = 1024
LCD_HEADER_SIZE = 8
MAX_CHUNK = LCD_BUFFER_SIZE - LCD_HEADER_SIZE   # 1016

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def find_lcd_path():
    """Return the hidraw path of the LCD's interface 0, or None."""
    for pid in LCD_PRODUCT_IDS:
        candidates = hid.enumerate(VENDOR_ID, pid)
        # Prefer interface 0 (the screen control interface).
        for info in candidates:
            if info.get("interface_number", 0) == 0:
                return info["path"], pid
        if candidates:
            return candidates[0]["path"], pid
    return None, None


def open_lcd():
    path, pid = find_lcd_path()
    if path is None:
        sys.exit(
            "No LCD device found (looked for 0x1B1C:0x0C39 / 0x0C33).\n"
            "Run `lsusb -d 1b1c:` to list your Corsair devices. If the LCD shows\n"
            "a different product id, add it to LCD_PRODUCT_IDS in this script."
        )
    h = hid.device()
    try:
        h.open_path(path)
    except OSError as e:
        sys.exit(f"Could not open the LCD ({e}).\nTry running with sudo, or install the udev rule shown in the header.")
    return h, pid


def hex_color(s, default):
    if s is None:
        return default
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


# The panel is 480x480 but the visible area is a circle. Keep text inside a
# slightly-inset circle so the bezel doesn't clip it. Lower this if your bezel
# hides more of the panel; raise it toward 1.0 to use more of the glass.
SAFE_RADIUS_FRAC = 0.90


def load_font(size, candidates=FONT_CANDIDATES):
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _line_size(draw, text, font):
    bb = draw.textbbox((0, 0), text or " ", font=font)
    return (bb[2] - bb[0], bb[3] - bb[1], bb)


def _wrap(draw, message, font, max_w):
    """Greedy word-wrap; respects explicit \\n in the message."""
    lines = []
    for para in message.split("\\n"):
        words = para.split(" ")
        cur = words[0]
        for w in words[1:]:
            trial = cur + " " + w
            if _line_size(draw, trial, font)[0] <= max_w:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def render_text(message, fg, bg, size=None):
    """Render centered text that fits inside the round display.

    If size is None, auto-shrink (with wrapping) until the whole text block's
    corners sit inside the safe circle. If size is given, that size is forced.
    """
    img = Image.new("RGB", (IMG_W, IMG_H), bg)
    draw = ImageDraw.Draw(img)
    cx, cy = IMG_W / 2, IMG_H / 2
    r_eff = (IMG_W / 2) * SAFE_RADIUS_FRAC
    max_line_w = 2 * r_eff  # generous; the corner test below enforces the circle

    candidate_sizes = [size] if size else list(range(220, 22, -2))
    font = lines = dims = gap = block_h = None
    for s in candidate_sizes:
        f = load_font(s)
        ls = _wrap(draw, message, f, max_line_w)
        ds = [_line_size(draw, ln, f) for ln in ls]
        block_w = max(d[0] for d in ds)
        g = max(2, int(s * 0.22))
        h = sum(d[1] for d in ds) + g * (len(ls) - 1)
        font, lines, dims, gap, block_h = f, ls, ds, g, h  # remember latest
        # Fits when the block's corners are within the safe circle.
        if (block_w / 2) ** 2 + (h / 2) ** 2 <= r_eff ** 2:
            break

    y = cy - block_h / 2
    for ln, (w, h, bb) in zip(lines, dims):
        x = cx - w / 2 - bb[0]
        draw.text((x, y - bb[1]), ln, font=font, fill=fg)
        y += h + gap
    return img


def load_image(path):
    img = Image.open(path).convert("RGB")
    # Fit (cover) into 480x480, center-cropped.
    src_w, src_h = img.size
    scale = max(IMG_W / src_w, IMG_H / src_h)
    img = img.resize((int(src_w * scale), int(src_h * scale)), Image.LANCZOS)
    left = (img.width - IMG_W) // 2
    top = (img.height - IMG_H) // 2
    return img.crop((left, top, left + IMG_W, top + IMG_H))


def image_to_jpeg(img):
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


# --- Per-core "reactor dial" -------------------------------------------------

def _lerp(c0, c1, t):
    return tuple(int(round(c0[k] + (c1[k] - c0[k]) * t)) for k in range(3))


def load_color(pct):
    """Green (idle) -> amber (busy) -> red (maxed)."""
    p = max(0.0, min(100.0, pct)) / 100.0
    if p < 0.5:
        return _lerp((40, 200, 105), (240, 190, 30), p / 0.5)
    return _lerp((240, 190, 30), (235, 55, 45), (p - 0.5) / 0.5)


def _annular_sector(draw, cx, cy, r0, r1, a0, a1, color):
    """Filled ring segment between radii r0..r1 and angles a0..a1 (degrees)."""
    steps = max(2, int((a1 - a0) / 3))
    pts = []
    for k in range(steps + 1):  # outer arc, a0 -> a1
        a = math.radians(a0 + (a1 - a0) * k / steps)
        pts.append((cx + r1 * math.cos(a), cy + r1 * math.sin(a)))
    for k in range(steps + 1):  # inner arc, a1 -> a0
        a = math.radians(a1 - (a1 - a0) * k / steps)
        pts.append((cx + r0 * math.cos(a), cy + r0 * math.sin(a)))
    draw.polygon(pts, fill=color)


def render_cores(loads, bg=(12, 12, 16)):
    """Render per-core loads as a ring of radial bars with an average hub.
    Supersampled 2x then downscaled for smooth edges."""
    n = max(1, len(loads))
    ss = 2
    W = IMG_W * ss
    cx = cy = W / 2.0
    img = Image.new("RGB", (W, W), bg)
    d = ImageDraw.Draw(img)

    r_outer = (W / 2.0) * SAFE_RADIUS_FRAC
    r_inner = W * 0.165
    track = (34, 34, 44)
    seg = 360.0 / n
    gap = 2.0 if n <= 12 else 1.3
    start = -90.0  # core 0 at top, going clockwise

    for i, load in enumerate(loads):
        a0 = start + i * seg + gap / 2.0
        a1 = start + (i + 1) * seg - gap / 2.0
        _annular_sector(d, cx, cy, r_inner, r_outer, a0, a1, track)
        vr = r_inner + (r_outer - r_inner) * (max(0.0, min(100.0, load)) / 100.0)
        if vr > r_inner + 0.5:
            _annular_sector(d, cx, cy, r_inner, vr, a0, a1, load_color(load))

    # Central hub with the average load.
    hub_r = r_inner * 0.80
    d.ellipse([cx - hub_r, cy - hub_r, cx + hub_r, cy + hub_r], fill=(20, 20, 27))
    d.ellipse([cx - hub_r, cy - hub_r, cx + hub_r, cy + hub_r], outline=(45, 45, 58), width=ss * 2)

    avg = sum(loads) / n
    big = load_font(int(W * 0.135))
    small = load_font(int(W * 0.052))
    avg_txt = f"{avg:.0f}%"
    bb = d.textbbox((0, 0), avg_txt, font=big)
    d.text((cx - (bb[2] - bb[0]) / 2 - bb[0], cy - (bb[3] - bb[1]) / 2 - bb[1] - W * 0.018),
           avg_txt, font=big, fill=load_color(avg))
    lb = d.textbbox((0, 0), "CPU", font=small)
    d.text((cx - (lb[2] - lb[0]) / 2 - lb[0], cy + W * 0.075),
           "CPU", font=small, fill=(150, 150, 165))

    return img.resize((IMG_W, IMG_H), Image.LANCZOS)


def make_cores_renderer(bg):
    """Return a zero-arg callable producing a fresh JPEG of current core loads.
    psutil measures load between successive calls, so this suits the watch loop."""
    try:
        import psutil
    except ImportError:
        sys.exit("Per-core load needs psutil. Install with: pip install psutil")
    psutil.cpu_percent(percpu=True)  # prime; first real reading comes next tick

    def render():
        return image_to_jpeg(render_cores(psutil.cpu_percent(percpu=True), bg))
    return render


# --- Analog clock ------------------------------------------------------------

def _hand(draw, cx, cy, frac, length, width, color, tail=0.0):
    """Draw a clock hand. frac is position around the dial (0..1, 0 = 12 o'clock)."""
    a = math.radians(frac * 360.0 - 90.0)
    ex, ey = cx + length * math.cos(a), cy + length * math.sin(a)
    tx, ty = cx - tail * math.cos(a), cy - tail * math.sin(a)
    draw.line([(tx, ty), (ex, ey)], fill=color, width=int(width))


def render_clock(now, bg=(12, 12, 16), accent=(240, 90, 75), smooth=False):
    """Render an analog clock. Supersampled 2x then downscaled for smooth edges.
    smooth=False snaps the second hand to whole seconds (lands on each tick);
    smooth=True uses sub-second motion for a sweeping second hand."""
    ss = 2
    W = IMG_W * ss
    cx = cy = W / 2.0
    img = Image.new("RGB", (W, W), bg)
    d = ImageDraw.Draw(img)

    r_dial = (W / 2.0) * SAFE_RADIUS_FRAC
    face = (150, 150, 165)
    bright = (235, 235, 245)

    # Tick ring: 60 minute ticks, every 5th emphasized as an hour tick.
    for i in range(60):
        a = math.radians(i * 6.0 - 90.0)
        hour_tick = (i % 5 == 0)
        t_len = W * (0.052 if hour_tick else 0.024)
        w = ss * (3 if hour_tick else 1)
        col = bright if hour_tick else face
        r1 = r_dial
        r0 = r_dial - t_len
        d.line([(cx + r0 * math.cos(a), cy + r0 * math.sin(a)),
                (cx + r1 * math.cos(a), cy + r1 * math.sin(a))], fill=col, width=w)

    # Hour numerals.
    num_font = load_font(int(W * 0.082))
    r_num = r_dial - W * 0.10
    for h in range(1, 13):
        a = math.radians(h * 30.0 - 90.0)
        s = str(h)
        bb = d.textbbox((0, 0), s, font=num_font)
        nx = cx + r_num * math.cos(a) - (bb[2] - bb[0]) / 2 - bb[0]
        ny = cy + r_num * math.sin(a) - (bb[3] - bb[1]) / 2 - bb[1]
        d.text((nx, ny), s, font=num_font, fill=bright)

    # Date readout (weekday + day) low on the face.
    date_font = load_font(int(W * 0.045))
    ds = now.strftime("%a %d").upper()
    bb = d.textbbox((0, 0), ds, font=date_font)
    d.text((cx - (bb[2] - bb[0]) / 2 - bb[0], cy + r_dial * 0.42),
           ds, font=date_font, fill=(120, 120, 135))

    # Hands. In tick mode the second hand snaps to whole seconds so it lands
    # exactly on a tick; in smooth mode it includes sub-second motion to sweep.
    sec = now.second + (now.microsecond / 1e6 if smooth else 0)
    minute = now.minute + sec / 60.0
    hour = (now.hour % 12) + minute / 60.0
    _hand(d, cx, cy, hour / 12.0, r_dial * 0.50, ss * 7, bright, tail=r_dial * 0.08)
    _hand(d, cx, cy, minute / 60.0, r_dial * 0.72, ss * 5, bright, tail=r_dial * 0.10)
    _hand(d, cx, cy, sec / 60.0, r_dial * 0.82, ss * 2, accent, tail=r_dial * 0.16)

    # Center cap.
    cap = ss * 7
    d.ellipse([cx - cap, cy - cap, cx + cap, cy + cap], fill=accent)
    d.ellipse([cx - ss * 3, cy - ss * 3, cx + ss * 3, cy + ss * 3], fill=bg)

    return img.resize((IMG_W, IMG_H), Image.LANCZOS)


# --- Claude usage, displayed with flair -------------------------------------
#
# Claude Code writes a JSONL transcript per session under ~/.claude/projects/.
# Each assistant line carries a `message.usage` object (input/output/cache
# tokens) plus a `message.model` and a top-level ISO `timestamp`. We aggregate
# those to show how many tokens have been vaporized. The dollar figure is a
# tongue-in-cheek ESTIMATE from public per-token rates — on a subscription plan
# it is emphatically not your real bill.

# Approx public API pricing, USD per 1M tokens: (input, output, cache_write, cache_read).
USAGE_RATES = {
    "opus":   (15.00, 75.00, 18.75, 1.50),
    "sonnet": (3.00, 15.00, 3.75, 0.30),
    "haiku":  (0.80, 4.00, 1.00, 0.08),
}
DEFAULT_RATE = USAGE_RATES["sonnet"]   # unknown model → assume mid-tier


def _rate_for(model):
    m = (model or "").lower()
    for key, rate in USAGE_RATES.items():
        if key in m:
            return rate
    return DEFAULT_RATE


def _claude_base_dir():
    """Locate the Claude Code config dir (~/.claude), even when run under sudo.

    HID writes usually need root, so this tends to run via sudo — where ~ resolves
    to /root and the transcripts (owned by the real user) are invisible. Prefer an
    explicit CLAUDE_CONFIG_DIR, then the sudo-invoking user's home, then ~."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return env
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        try:
            import pwd
            return os.path.join(pwd.getpwnam(sudo_user).pw_dir, ".claude")
        except (KeyError, ImportError):
            pass
    return os.path.expanduser("~/.claude")


# Optional explicit override set from the CLI (--claude-dir); wins over everything.
_CLAUDE_DIR_OVERRIDE = None


def _claude_projects_dir():
    base = _CLAUDE_DIR_OVERRIDE or _claude_base_dir()
    return os.path.join(base, "projects")


def _parse_ts(s):
    """ISO-8601 (with trailing Z) → epoch seconds, or None if unparseable."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _window_start(window):
    """Epoch cutoff for the window, or None to include everything ('all')."""
    now = time.time()
    if window == "all":
        return None
    if window == "24h":
        return now - 24 * 3600
    lt = time.localtime(now)  # 'today' = since local midnight
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def collect_usage(window="today"):
    """Aggregate token usage across all Claude Code transcripts.

    Returns {tokens: {input,output,cache_creation,cache_read,total}, cost, messages, window}.
    Lines are deduped by (message id, requestId) so streamed/retried copies of the
    same reply aren't counted twice. Cost is an estimate (see module notes)."""
    cutoff = _window_start(window)
    tokens = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
    cost = 0.0
    messages = 0
    seen = set()

    for path in glob.glob(os.path.join(_claude_projects_dir(), "**", "*.jsonl"), recursive=True):
        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                # Cheap pre-filter before the JSON parse.
                if '"output_tokens"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                msg = d.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                key = (msg.get("id"), d.get("requestId"))
                if key != (None, None):
                    if key in seen:
                        continue
                    seen.add(key)
                if cutoff is not None:
                    ts = _parse_ts(d.get("timestamp"))
                    if ts is not None and ts < cutoff:
                        continue
                inp = int(usage.get("input_tokens") or 0)
                out = int(usage.get("output_tokens") or 0)
                cc = int(usage.get("cache_creation_input_tokens") or 0)
                cr = int(usage.get("cache_read_input_tokens") or 0)
                tokens["input"] += inp
                tokens["output"] += out
                tokens["cache_creation"] += cc
                tokens["cache_read"] += cr
                messages += 1
                ri, ro, rw, rr = _rate_for(msg.get("model", ""))
                cost += (inp * ri + out * ro + cc * rw + cr * rr) / 1e6

    tokens["total"] = tokens["input"] + tokens["output"] + tokens["cache_creation"] + tokens["cache_read"]
    return {"tokens": tokens, "cost": cost, "messages": messages, "window": window}


def _human_count(n):
    """1290419 -> '1.29M'."""
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= div:
            return f"{n / div:.2f}{unit}"
    return str(int(n))


def _usage_canvas(bg):
    """Fresh 2x-supersampled canvas + draw handle; downscale on the way out."""
    ss = 2
    W = IMG_W * ss
    img = Image.new("RGB", (W, W), bg)
    return img, ImageDraw.Draw(img), W, ss


def _centered(d, text, font, cx, cy, fill):
    bb = d.textbbox((0, 0), text, font=font)
    d.text((cx - (bb[2] - bb[0]) / 2 - bb[0], cy - (bb[3] - bb[1]) / 2 - bb[1]), text, font=font, fill=fill)


def _flame(d, cx, base_y, half_w, height, color):
    """A tapered flame: narrow tip, a bulge partway down, narrowing again to the base."""
    tip_y = base_y - height
    d.polygon([
        (cx, tip_y),
        (cx + half_w * 0.5, base_y - height * 0.55),
        (cx + half_w, base_y - height * 0.18),
        (cx + half_w * 0.5, base_y),
        (cx - half_w * 0.5, base_y),
        (cx - half_w, base_y - height * 0.18),
        (cx - half_w * 0.5, base_y - height * 0.55),
    ], fill=color)


def _teardrop(d, x, y, rr, color):
    """A little sweat/water drop centered near (x, y)."""
    d.ellipse([x - rr, y - rr * 0.4, x + rr, y + rr * 1.4], fill=color)
    d.polygon([(x - rr * 0.72, y - rr * 0.05), (x + rr * 0.72, y - rr * 0.05), (x, y - rr * 1.15)], fill=color)


def render_usage_money(u, bg=(10, 10, 14), accent=(80, 200, 120)):
    """Estimated spend as cash on fire; taller flames = more money burned."""
    img, d, W, ss = _usage_canvas(bg)
    cx = cy = W / 2.0
    r = (W / 2.0) * SAFE_RADIUS_FRAC
    cost = u["cost"]

    # Headline dollar figure up top.
    _centered(d, f"${cost:,.2f}", load_font(int(W * 0.155)), cx, cy - r * 0.60, (245, 225, 90))

    # Banknote, with flames rising from its top edge. Flame height grows with spend.
    note_top = cy + r * 0.14
    bw, bh = r * 1.05, r * 0.46
    bx0 = cx - bw / 2
    intensity = min(1.0, cost / 20.0)                 # ~$20 = full inferno
    fh = r * 0.22 + r * 0.50 * intensity
    # A bouquet of flames — tallest in the center, tapering to the sides — layered
    # back (red) to front (yellow) for depth.
    tongues = ((-0.40, 0.55), (-0.20, 0.80), (0.0, 1.0), (0.22, 0.80), (0.42, 0.56))
    for col, scale in (((215, 45, 30), 1.0), ((240, 140, 25), 0.66), ((250, 220, 95), 0.38)):
        for off, hmul in tongues:
            _flame(d, cx + bw * off, note_top, r * 0.16 * scale + r * 0.05, fh * scale * hmul, col)
    d.rounded_rectangle([bx0, note_top, bx0 + bw, note_top + bh], radius=bh * 0.14,
                        fill=(38, 105, 66), outline=(120, 200, 150), width=ss * 2)
    _centered(d, "$", load_font(int(bh * 0.72)), cx, note_top + bh / 2, (200, 240, 210))

    _centered(d, f"BURNED · {u['window'].upper()}", load_font(int(W * 0.052)),
              cx, cy + r * 0.75, (170, 150, 120))
    return img.resize((IMG_W, IMG_H), Image.LANCZOS)


def render_usage_robot(u, bg=(10, 10, 14), accent=(90, 200, 235)):
    """A little bot chugging tokens — sweatier and wider-eyed as the count climbs."""
    img, d, W, ss = _usage_canvas(bg)
    cx = cy = W / 2.0
    r = (W / 2.0) * SAFE_RADIUS_FRAC
    total = u["tokens"]["total"]
    panic = min(1.0, total / 5_000_000.0)             # ~5M tokens = meltdown

    hw, hh = r * 0.98, r * 0.80
    hx0, hy0 = cx - hw / 2, cy - hh / 2 - r * 0.08
    # Antenna with a status bulb that reddens under load.
    d.line([(cx, hy0), (cx, hy0 - r * 0.15)], fill=(90, 100, 120), width=ss * 3)
    d.ellipse([cx - ss * 7, hy0 - r * 0.15 - ss * 7, cx + ss * 7, hy0 - r * 0.15 + ss * 7],
              fill=load_color(panic * 100))
    d.rounded_rectangle([hx0, hy0, hx0 + hw, hy0 + hh], radius=r * 0.14,
                        fill=(45, 50, 62), outline=(95, 105, 125), width=ss * 3)

    eye_y = hy0 + hh * 0.40
    eye_r = r * (0.09 + 0.05 * panic)
    eye_col = _lerp((120, 220, 255), (240, 80, 70), panic)
    for sx in (-1, 1):
        ex = cx + sx * hw * 0.22
        d.ellipse([ex - eye_r, eye_y - eye_r, ex + eye_r, eye_y + eye_r], fill=(240, 245, 250))
        pr = eye_r * 0.5
        d.ellipse([ex - pr, eye_y - pr, ex + pr, eye_y + pr], fill=eye_col)

    my = hy0 + hh * 0.74
    mw = hw * 0.42
    if panic < 0.5:                                    # calm: flat mouth
        d.line([(cx - mw / 2, my), (cx + mw / 2, my)], fill=(200, 210, 225), width=ss * 4)
    else:                                              # panicking: open gulp
        d.ellipse([cx - mw * 0.36, my - mw * 0.22, cx + mw * 0.36, my + mw * 0.36],
                  fill=(18, 18, 24), outline=(200, 210, 225), width=ss * 3)

    for i in range(int(round(panic * 4))):             # 0..4 sweat drops
        _teardrop(d, hx0 + hw * (0.14 + 0.2 * i), hy0 + hh * 0.18 + (i % 2) * r * 0.07,
                  r * 0.05, accent)

    _centered(d, f"{_human_count(total)} tok", load_font(int(W * 0.115)), cx, cy + r * 0.56, (235, 240, 250))
    _centered(d, "so thirsty..." if panic >= 0.5 else "glug glug", load_font(int(W * 0.05)),
              cx, cy + r * 0.76, (150, 160, 175))
    return img.resize((IMG_W, IMG_H), Image.LANCZOS)


def render_usage_gauge(u, bg=(10, 10, 14), accent=(235, 55, 45), cap=10_000_000):
    """A car-style fuel needle for tokens, pinned toward 'F' (send help)."""
    img, d, W, ss = _usage_canvas(bg)
    cx = cy = W / 2.0
    r = (W / 2.0) * SAFE_RADIUS_FRAC
    total = u["tokens"]["total"]
    frac = min(1.0, total / float(max(1, cap)))

    gcx, gcy = cx, cy + r * 0.30                        # pivot sits a bit low
    r1 = r * 0.95
    r0 = r1 * 0.80
    _annular_sector(d, gcx, gcy, r0, r1, 180, 360, (40, 40, 50))       # track (top arc)
    _annular_sector(d, gcx, gcy, r0, r1, 315, 360, (120, 35, 30))      # red zone (last 25%)
    _annular_sector(d, gcx, gcy, r0, r1, 180, 180 + 180 * frac, load_color(frac * 100))

    end_font = load_font(int(W * 0.075))
    _centered(d, "E", end_font, gcx - r1 * 0.92, gcy - r * 0.02, (150, 150, 165))
    _centered(d, "F", end_font, gcx + r1 * 0.92, gcy - r * 0.02, (235, 90, 80))

    a = math.radians(180 + 180 * frac)                  # needle
    d.line([(gcx, gcy), (gcx + r1 * 0.9 * math.cos(a), gcy + r1 * 0.9 * math.sin(a))],
           fill=accent, width=ss * 5)
    d.ellipse([gcx - ss * 8, gcy - ss * 8, gcx + ss * 8, gcy + ss * 8], fill=accent)

    _centered(d, _human_count(total), load_font(int(W * 0.11)), gcx, gcy - r * 0.16, (235, 240, 250))
    caption = ("sipping" if frac < 0.5 else "guzzling" if frac < 0.85 else
               "THIRSTY" if frac < 1.0 else "SEND HELP")
    _centered(d, caption, load_font(int(W * 0.055)), gcx, gcy + r * 0.03, (170, 175, 190))
    return img.resize((IMG_W, IMG_H), Image.LANCZOS)


def render_usage_counter(u, bg=(10, 10, 14), accent=(235, 55, 45)):
    """A dramatic odometer of tokens vaporized, under a DAMAGE REPORT banner."""
    img, d, W, ss = _usage_canvas(bg)
    cx = cy = W / 2.0
    r = (W / 2.0) * SAFE_RADIUS_FRAC
    total = u["tokens"]["total"]

    _centered(d, "DAMAGE REPORT", load_font(int(W * 0.062)), cx, cy - r * 0.60, accent)

    s = str(total)
    n = len(s)
    gap = W * 0.010
    box_w = min(W * 0.115, (2 * r * 0.94 - gap * (n - 1)) / n)
    box_h = box_w * 1.5
    digit_font = load_font(int(box_h * 0.72))
    x = cx - (box_w * n + gap * (n - 1)) / 2
    y = cy - box_h / 2
    for ch in s:
        d.rounded_rectangle([x, y, x + box_w, y + box_h], radius=box_w * 0.16,
                            fill=(24, 24, 30), outline=(70, 70, 84), width=ss * 2)
        _centered(d, ch, digit_font, x + box_w / 2, y + box_h / 2, (240, 240, 250))
        x += box_w + gap

    _centered(d, f"TOKENS VAPORIZED · {u['window'].upper()}", load_font(int(W * 0.05)),
              cx, cy + r * 0.58, (150, 150, 165))
    _centered(d, f"{u['messages']} msgs · ~${u['cost']:,.2f}", load_font(int(W * 0.045)),
              cx, cy + r * 0.74, (110, 110, 125))
    return img.resize((IMG_W, IMG_H), Image.LANCZOS)


USAGE_STYLES = ("money", "robot", "gauge", "counter")


def make_usage_renderer(style, window, bg, accent, cap):
    """Return a zero-arg callable producing a fresh JPEG of current Claude usage
    in the chosen comedic style, suitable for the watch loop."""
    def render():
        u = collect_usage(window)
        if style == "money":
            img = render_usage_money(u, bg, accent)
        elif style == "robot":
            img = render_usage_robot(u, bg, accent)
        elif style == "gauge":
            img = render_usage_gauge(u, bg, accent, cap)
        else:
            img = render_usage_counter(u, bg, accent)
        return image_to_jpeg(img)
    return render


# --- Power draw, as an electricity meter ------------------------------------
#
# Linux exposes power for the two big consumers without extra tools:
#   * CPU package: RAPL energy counters under /sys/class/powercap (the
#     intel-rapl driver also serves AMD Zen). They're cumulative microjoules, so
#     power is the delta between two reads — and they're root-only (CVE-2020-8694).
#   * GPU: amdgpu hwmon power1_average/power1_input (microwatts), or nvidia-smi.
# Board, RAM, drives, fans and PSU losses have no sensor, so the readout is
# CPU + GPU and says so — it is not wall power.
#
# The face is styled after a Ferraris electricity meter: an aluminium disc seen
# edge-on that turns once per POWER_WH_PER_REV (so its speed *is* the power),
# and a cyclometer register counting energy used since the display started.

POWER_WH_PER_REV = 2.0

_NOTO = "/usr/share/fonts/truetype/noto/"
POWER_DISPLAY_FONTS = [_NOTO + "NotoSansDisplay-CondensedSemiBold.ttf",
                       _NOTO + "NotoSans-CondensedSemiBold.ttf"] + FONT_CANDIDATES
POWER_LABEL_FONTS = [_NOTO + "NotoSans-SemiCondensedMedium.ttf"] + FONT_CANDIDATES
POWER_DIGIT_FONTS = [_NOTO + "NotoSans-CondensedBold.ttf"] + FONT_CANDIDATES

BAKELITE = (20, 16, 14)
SMOKE = (46, 39, 35)
IVORY = (236, 228, 214)
COPPER = (212, 137, 90)
VERDIGRIS = (95, 163, 143)
SIGNAL = (214, 52, 40)
PLATE = (128, 116, 106)          # nameplate/label text
ALU_HI, ALU_LO = (214, 218, 223), (92, 98, 106)


def _read_sysfs(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _read_int(path):
    try:
        return int(_read_sysfs(path))
    except (TypeError, ValueError):
        return None


class PowerSampler:
    """Reads CPU (RAPL) and GPU power. CPU power is an energy delta, so the
    first cpu_watts() call only primes the counters and returns None."""

    def __init__(self):
        self.rapl = []
        # Top-level zones only (intel-rapl:N); subzones like intel-rapl:N:0 are
        # already included in their package's count.
        for zone in sorted(glob.glob("/sys/class/powercap/intel-rapl:*")):
            if zone.count(":") == 1:
                self.rapl.append({"path": os.path.join(zone, "energy_uj"),
                                  "range": _read_int(os.path.join(zone, "max_energy_range_uj")) or 2 ** 32,
                                  "last": None, "t": None})
        self.gpu_files = []
        for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            if _read_sysfs(os.path.join(hw, "name")) == "amdgpu":
                for f in ("power1_average", "power1_input"):
                    if os.path.exists(os.path.join(hw, f)):
                        self.gpu_files.append(os.path.join(hw, f))
                        break
        import shutil
        self.nvidia_smi = shutil.which("nvidia-smi")

    def cpu_readable(self):
        return any(_read_int(z["path"]) is not None for z in self.rapl)

    def cpu_watts(self):
        total = None
        now = time.monotonic()
        for z in self.rapl:
            uj = _read_int(z["path"])
            if uj is None:
                continue
            if z["last"] is not None and now > z["t"]:
                delta = uj - z["last"]
                if delta < 0:                      # counter wrapped
                    delta += z["range"]
                total = (total or 0.0) + delta / 1e6 / (now - z["t"])
            z["last"], z["t"] = uj, now
        return total

    def gpu_watts(self):
        readings = [_read_int(f) for f in self.gpu_files]
        watts = [uw / 1e6 for uw in readings if uw is not None]
        if self.nvidia_smi:
            import subprocess
            try:
                out = subprocess.run([self.nvidia_smi, "--query-gpu=power.draw",
                                      "--format=csv,noheader,nounits"],
                                     capture_output=True, text=True, timeout=2).stdout
                watts += [float(v) for v in out.split() if v.replace(".", "", 1).isdigit()]
            except (OSError, subprocess.SubprocessError):
                pass
        return sum(watts) if watts else None


def _tracked(d, text, font, cx, cy, fill, track):
    """Letter-spaced text centered on (cx, cy) — Pillow has no tracking control."""
    widths = [d.textlength(ch, font=font) for ch in text]
    x = cx - (sum(widths) + track * (len(text) - 1)) / 2
    for ch, w in zip(text, widths):
        d.text((x, cy), ch, font=font, fill=fill, anchor="lm")
        x += w + track


def _gradient(size, values, vertical):
    """An L-mode strip from per-pixel values, stretched across `size`."""
    strip = Image.new("L", (1, len(values)) if vertical else (len(values), 1))
    strip.putdata([max(0, min(255, int(v))) for v in values])
    return strip.resize(size, Image.NEAREST)


def _meter_disc(w, h, phase, ss):
    """The meter disc seen edge-on through its window: brushed aluminium,
    foreshortened graduations, and the red index mark. phase is in degrees."""
    from PIL import ImageChops, ImageOps
    # Cylinder shading: a highlight just above center, falling off toward the
    # window ends where the disc curves away.
    vert = _gradient((w, h), [255 * (0.55 + 0.45 * math.cos((y / h - 0.38) * math.pi * 1.1))
                              for y in range(h)], vertical=True)
    horiz = _gradient((w, h), [255 * math.cos(math.asin(max(-1.0, min(1.0, (x - w / 2) / (w / 2))))) ** 0.55
                               for x in range(w)], vertical=False)
    shade = ImageChops.multiply(vert, horiz)
    disc = ImageOps.colorize(shade, black=(10, 10, 12), mid=ALU_LO, white=ALU_HI)
    d = ImageDraw.Draw(disc)

    half = w / 2
    for k in range(72):                          # a mark every 5 degrees of disc
        theta = math.radians(phase + k * 5.0)
        depth = math.cos(theta)                  # 1 = facing us, 0 = edge-on
        if depth <= 0.05:
            continue
        x = half + half * math.sin(theta)
        if k == 0:                               # the red index mark
            mw = half * 0.10 * depth
            d.rectangle([x - mw / 2, 0, x + mw / 2, h], fill=_lerp((40, 10, 8), SIGNAL, depth ** 0.5))
            continue
        y1 = h * (0.62 if k % 6 == 0 else 0.38)
        d.line([(x, h * 0.12), (x, y1)], fill=_lerp((150, 154, 160), (36, 38, 44), depth ** 0.6),
               width=max(1, int(ss * 1.6 * depth)))
    return disc


def _register_wheel(w, h, value, ss, face, ink):
    """One cyclometer drum. value may be fractional: 3.4 shows 3 rolling to 4."""
    from PIL import ImageChops
    font = load_font(int(h * 0.62), POWER_DIGIT_FONTS)
    wheel = Image.new("RGB", (w, h), face)
    d = ImageDraw.Draw(wheel)
    digit, frac = int(value) % 10, value - int(value)
    pitch = h * 0.92                             # distance between digits on the drum
    for k in (-1, 0, 1):
        d.text((w / 2, h / 2 + (k - frac) * pitch), str((digit + k) % 10),
               font=font, fill=ink, anchor="mm")
    # Darken top and bottom so it reads as a drum, not a flat box.
    shade = _gradient((w, h), [255 * (0.30 + 0.70 * math.sin(math.pi * (y + 0.5) / h) ** 0.8)
                               for y in range(h)], vertical=True)
    return ImageChops.multiply(wheel, Image.merge("RGB", (shade, shade, shade)))


def render_power(cpu, gpu, wh, since, bg=BAKELITE, accent=COPPER):
    """Electricity-meter face: CPU + GPU watts, a disc turning with the energy
    used, and a register of Wh consumed since `since`. cpu/gpu may be None
    (sensor missing or unreadable). Supersampled 2x."""
    ss = 2
    W = IMG_W * ss
    cx = cy = W / 2.0
    r = (W / 2.0) * SAFE_RADIUS_FRAC
    img = Image.new("RGB", (W, W), bg)
    d = ImageDraw.Draw(img)

    # Bezel, with a faint glass glint top-left.
    rb = r * 1.03
    d.ellipse([cx - rb, cy - rb, cx + rb, cy + rb], outline=SMOKE, width=ss * 2)
    d.arc([cx - rb, cy - rb, cx + rb, cy + rb], 200, 245, fill=_lerp(SMOKE, IVORY, 0.18), width=ss * 2)

    parts = [p for p in (cpu, gpu) if p is not None]
    total = sum(parts) if parts else None

    # Component readout.
    label_font = load_font(int(W * 0.036), POWER_LABEL_FONTS)
    for name, val, col, side in (("CPU", cpu, accent, -1), ("GPU", gpu, VERDIGRIS, 1)):
        txt = f"{name}  {val:.0f} W" if val is not None else f"{name}  —"
        _tracked(d, txt, label_font, cx + side * r * 0.31, cy - r * 0.64,
                 col if val is not None else PLATE, ss * 2)

    # Headline wattage, unit set on the same baseline.
    num_font = load_font(int(W * 0.235), POWER_DISPLAY_FONTS)
    unit_font = load_font(int(W * 0.085), POWER_DISPLAY_FONTS)
    num = f"{total:.0f}" if total is not None else "—"
    nw, uw, gap = d.textlength(num, font=num_font), d.textlength("W", font=unit_font), W * 0.012
    x0 = cx - (nw + gap + uw) / 2
    base = cy - r * 0.12
    d.text((x0, base), num, font=num_font, fill=IVORY, anchor="ls")
    d.text((x0 + nw + gap, base), "W", font=unit_font, fill=accent, anchor="ls")

    # CPU/GPU split bar.
    bw, bh, by = r * 0.92, ss * 4, cy - r * 0.03
    bx = cx - bw / 2
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh / 2, fill=SMOKE)
    if total:
        split = bx + bw * (cpu or 0.0) / total
        if cpu:
            d.rounded_rectangle([bx, by, split - ss * 2, by + bh], radius=bh / 2, fill=accent)
        if gpu:
            d.rounded_rectangle([split + ss * 2, by, bx + bw, by + bh], radius=bh / 2, fill=VERDIGRIS)

    # Disc window.
    ww, wh_ = int(r * 1.30), int(r * 0.13)
    wx, wy = int(cx - ww / 2), int(cy + r * 0.18 - wh_ / 2)
    pad = ss * 4
    d.rounded_rectangle([wx - pad, wy - pad, wx + ww + pad, wy + wh_ + pad], radius=wh_ * 0.35,
                        fill=(8, 7, 6), outline=SMOKE, width=ss * 2)
    mask = Image.new("L", (ww, wh_), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, ww - 1, wh_ - 1], radius=wh_ * 0.28, fill=255)
    img.paste(_meter_disc(ww, wh_, wh / POWER_WH_PER_REV * 360.0, ss), (wx, wy), mask)

    # Cyclometer register: five integer drums and a red tenths drum that rolls.
    n, rw, rh, rgap = 6, int(r * 0.15), int(r * 0.23), int(r * 0.022)
    rx = int(cx - (n * rw + (n - 1) * rgap) / 2)
    ry = int(cy + r * 0.47 - rh / 2)
    d.rounded_rectangle([rx - pad * 2, ry - pad * 2, rx + n * rw + (n - 1) * rgap + pad * 2, ry + rh + pad * 2],
                        radius=pad * 3, fill=(8, 7, 6), outline=SMOKE, width=ss * 2)
    reading = (wh * 10) % 1_000_000                # in tenths of a Wh; wraps like a real meter
    wmask = Image.new("L", (rw, rh), 0)
    ImageDraw.Draw(wmask).rounded_rectangle([0, 0, rw - 1, rh - 1], radius=ss * 3, fill=255)
    for i in range(n):
        place = 10 ** (n - 1 - i)
        if i == n - 1:
            wheel = _register_wheel(rw, rh, reading % 10, ss, SIGNAL, IVORY)
        else:
            wheel = _register_wheel(rw, rh, int(reading // place) % 10, ss, (26, 23, 21), IVORY)
        img.paste(wheel, (rx + i * (rw + rgap), ry), wmask)

    plate_font = load_font(int(W * 0.032), POWER_LABEL_FONTS)
    _tracked(d, f"Wh SINCE {since.strftime('%H:%M')}", plate_font, cx, cy + r * 0.69, PLATE, ss * 3)
    _tracked(d, f"1 REV = {POWER_WH_PER_REV:g} Wh", plate_font, cx, cy + r * 0.81,
             _lerp(bg, PLATE, 0.6), ss * 3)
    return img.resize((IMG_W, IMG_H), Image.LANCZOS)


def make_power_renderer(bg, accent):
    """Return a zero-arg callable producing a fresh JPEG of current power draw.
    It integrates watts over time between calls to drive the disc and register."""
    sampler = PowerSampler()
    if not sampler.rapl and not sampler.gpu_files and not sampler.nvidia_smi:
        sys.exit("No power sensors found (no RAPL zones under /sys/class/powercap, "
                 "no amdgpu hwmon, no nvidia-smi).")
    if sampler.rapl and not sampler.cpu_readable():
        print("Warning: CPU power needs root (RAPL energy counters are root-only); "
              "showing GPU only.", file=sys.stderr)
    sampler.cpu_watts()                          # prime the energy counters
    state = {"wh": 0.0, "t": time.monotonic(), "since": datetime.now()}

    def render():
        cpu, gpu = sampler.cpu_watts(), sampler.gpu_watts()
        now = time.monotonic()
        state["wh"] += sum(p for p in (cpu, gpu) if p is not None) * (now - state["t"]) / 3600.0
        state["t"] = now
        return image_to_jpeg(render_power(cpu, gpu, state["wh"], state["since"], bg, accent))
    return render


def build_packets(jpeg):
    """Turn a JPEG into the chunked 1024-byte LCD write packets (built once)."""
    chunks = [jpeg[i:i + MAX_CHUNK] for i in range(0, len(jpeg), MAX_CHUNK)] or [b""]
    if len(chunks) > 255:
        sys.exit("Image too large after JPEG encoding; reduce detail/quality.")
    packets = []
    for i, chunk in enumerate(chunks):
        pkt = bytearray(LCD_BUFFER_SIZE)
        pkt[0] = 0x02                       # report id
        pkt[1] = 0x05
        if i == len(chunks) - 1:            # final chunk triggers render
            pkt[3] = 0x01
        pkt[4] = i & 0xFF                    # chunk index
        struct.pack_into("<H", pkt, 6, len(chunk))
        pkt[8:8 + len(chunk)] = chunk
        packets.append(bytes(pkt))
    return packets


def write_packets(h, packets):
    for pkt in packets:
        if h.write(pkt) < 0:
            sys.exit("HID write failed (permissions? device busy?).")


def send_frame(h, jpeg):
    """Upload one JPEG frame, once."""
    write_packets(h, build_packets(jpeg))


def watch_frame(h, jpeg, interval):
    """Re-push a static frame forever so the pump firmware can't reclaim the
    screen with its default. Ctrl-C (or a service stop) ends it cleanly."""
    packets = build_packets(jpeg)
    print(f"Holding screen; re-pushing every {interval:g}s. Ctrl-C to stop.")
    try:
        while True:
            write_packets(h, packets)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped. The pump firmware will redraw its default screen shortly.")


def watch_render(h, make_jpeg, interval):
    """Like watch_frame, but re-renders a fresh frame each tick from a callable
    (for live content such as per-core load or a clock). Updates are aligned to
    wall-clock interval boundaries so a ticking clock lands on each second."""
    print(f"Live display; updating every {interval:g}s. Ctrl-C to stop.")
    try:
        while True:
            write_packets(h, build_packets(make_jpeg()))
            dt = interval - (time.time() % interval)
            if dt < 1e-3:
                dt += interval
            time.sleep(dt)
    except KeyboardInterrupt:
        print("\nStopped. The pump firmware will redraw its default screen shortly.")


def set_brightness(h, value):
    value = max(0, min(100, value))
    # feature report: [0x03, 0x0b, brightness, 0x01]
    h.send_feature_report(bytes([0x03, 0x0B, value, 0x01]))


def set_rotation(h, value):
    value = max(0, min(3, value))
    # feature report: [0x03, 0x0c, rotation, 0x01]
    h.send_feature_report(bytes([0x03, 0x0C, value, 0x01]))


def main():
    p = argparse.ArgumentParser(description="Control the H150i ELITE LCD XT screen.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("detect", help="find the LCD and print its id")

    pt = sub.add_parser("text", help="render text and show it")
    pt.add_argument("message", help=r"text to show (use \n for line breaks)")
    pt.add_argument("--fg", help="text color hex, e.g. 00e070")
    pt.add_argument("--bg", help="background color hex, e.g. 101015")
    pt.add_argument("--size", type=int, default=None,
                    help="force a font size; omit to auto-fit text to the round screen")
    pt.add_argument("--watch", action="store_true",
                    help="keep re-pushing so the firmware can't reclaim the screen")
    pt.add_argument("--interval", type=float, default=1.0,
                    help="seconds between re-pushes in --watch mode (default 1.0)")

    pi = sub.add_parser("image", help="show an image file")
    pi.add_argument("path", help="path to an image (png/jpg/etc.)")
    pi.add_argument("--watch", action="store_true",
                    help="keep re-pushing so the firmware can't reclaim the screen")
    pi.add_argument("--interval", type=float, default=1.0,
                    help="seconds between re-pushes in --watch mode (default 1.0)")

    pb = sub.add_parser("brightness", help="set brightness 0-100")
    pb.add_argument("value", type=int)

    pr = sub.add_parser("rotate", help="set rotation 0-3")
    pr.add_argument("value", type=int)

    pc = sub.add_parser("cores", help="per-core CPU load as a radial reactor dial")
    pc.add_argument("--bg", help="background color hex, e.g. 0c0c10")
    pc.add_argument("--watch", action="store_true",
                    help="keep updating live (recommended for this one)")
    pc.add_argument("--interval", type=float, default=1.0,
                    help="seconds between updates (default 1.0)")

    pk = sub.add_parser("clock", help="analog clock with ticking hands")
    pk.add_argument("--bg", help="background color hex, e.g. 0c0c10")
    pk.add_argument("--accent", help="second-hand/cap color hex, e.g. f0593f")
    pk.add_argument("--watch", action="store_true",
                    help="keep ticking live (recommended)")
    pk.add_argument("--smooth", action="store_true",
                    help="sweeping second hand instead of a once-per-second tick")
    pk.add_argument("--interval", type=float, default=None,
                    help="seconds between updates (default: 1.0 tick, 0.2 smooth)")

    pusg = sub.add_parser("usage", help="Claude Code token usage, displayed with flair")
    pusg.add_argument("style", nargs="?", default="counter", choices=USAGE_STYLES,
                      help="which comedic display (default: counter)")
    pusg.add_argument("--window", choices=["today", "24h", "all"], default="today",
                      help="how far back to tally (default: today = since local midnight)")
    pusg.add_argument("--cap", type=int, default=10_000_000,
                      help="full-tank token count for the 'gauge' style (default 10M)")
    pusg.add_argument("--claude-dir",
                      help="path to the .claude config dir (default: auto-detect, "
                           "resolving the real user's home even under sudo)")
    pusg.add_argument("--bg", help="background color hex, e.g. 0a0a0e")
    pusg.add_argument("--accent", help="accent color hex (needle/eyes/banner)")
    pusg.add_argument("--watch", action="store_true",
                      help="keep updating live (recommended)")
    pusg.add_argument("--interval", type=float, default=5.0,
                      help="seconds between updates (default 5.0; usage moves slowly)")

    ppw = sub.add_parser("power", help="CPU + GPU power draw as an electricity meter")
    ppw.add_argument("--bg", help="background color hex, e.g. 14100e")
    ppw.add_argument("--accent", help="unit/CPU color hex, e.g. d4895a")
    ppw.add_argument("--watch", action="store_true",
                     help="keep updating live (recommended; the disc and register need it)")
    ppw.add_argument("--interval", type=float, default=1.0,
                     help="seconds between updates (default 1.0)")

    args = p.parse_args()

    if args.cmd == "detect":
        path, pid = find_lcd_path()
        if path:
            print(f"Found LCD: vendor 0x{VENDOR_ID:04x} product 0x{pid:04x}")
            print(f"  hidraw path: {path.decode() if isinstance(path, bytes) else path}")
        else:
            print("No LCD found. Run: lsusb -d 1b1c:")
        return

    h, _ = open_lcd()
    try:
        if args.cmd == "text":
            fg = hex_color(args.fg, (0, 224, 112))
            bg = hex_color(args.bg, (16, 16, 21))
            jpeg = image_to_jpeg(render_text(args.message, fg, bg, args.size))
            watch_frame(h, jpeg, args.interval) if args.watch else send_frame(h, jpeg)
        elif args.cmd == "image":
            jpeg = image_to_jpeg(load_image(args.path))
            watch_frame(h, jpeg, args.interval) if args.watch else send_frame(h, jpeg)
        elif args.cmd == "brightness":
            set_brightness(h, args.value)
        elif args.cmd == "rotate":
            set_rotation(h, args.value)
        elif args.cmd == "cores":
            bg = hex_color(args.bg, (12, 12, 16))
            render = make_cores_renderer(bg)
            if args.watch:
                watch_render(h, render, args.interval)
            else:
                # one-shot: take a brief real measurement first
                import psutil
                send_frame(h, image_to_jpeg(render_cores(
                    psutil.cpu_percent(percpu=True, interval=0.4), bg)))
        elif args.cmd == "clock":
            bg = hex_color(args.bg, (12, 12, 16))
            accent = hex_color(args.accent, (240, 90, 75))
            interval = args.interval if args.interval is not None else (0.2 if args.smooth else 1.0)
            render = lambda: image_to_jpeg(render_clock(datetime.now(), bg, accent, args.smooth))
            watch_render(h, render, interval) if args.watch else send_frame(h, render())
        elif args.cmd == "usage":
            global _CLAUDE_DIR_OVERRIDE
            _CLAUDE_DIR_OVERRIDE = args.claude_dir
            proj = _claude_projects_dir()
            if not glob.glob(os.path.join(proj, "**", "*.jsonl"), recursive=True):
                print(f"Warning: no Claude Code transcripts found under {proj} — the "
                      "display will read 0.\n"
                      "  This reads local session files, not any API. If you ran with "
                      "sudo, it now tries\n"
                      "  the invoking user's home automatically; otherwise point it at "
                      "your config dir with\n"
                      "  --claude-dir /home/you/.claude  (or set CLAUDE_CONFIG_DIR).",
                      file=sys.stderr)
            bg = hex_color(args.bg, (10, 10, 14))
            accent = hex_color(args.accent, (235, 90, 60))
            render = make_usage_renderer(args.style, args.window, bg, accent, args.cap)
            watch_render(h, render, args.interval) if args.watch else send_frame(h, render())
        elif args.cmd == "power":
            render = make_power_renderer(hex_color(args.bg, BAKELITE), hex_color(args.accent, COPPER))
            if args.watch:
                watch_render(h, render, args.interval)
            else:
                time.sleep(0.5)                  # one-shot: give RAPL a real interval to measure
                send_frame(h, render())
    finally:
        h.close()


if __name__ == "__main__":
    main()
