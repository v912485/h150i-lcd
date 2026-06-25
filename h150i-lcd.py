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
    ./h150i-lcd.py brightness 80          # 0-100
    ./h150i-lcd.py rotate 0               # 0,1,2,3  (up, right, down, left)

NOTE: the product ids and frame format match the ELITE LCD XT family, but
Corsair occasionally varies ids. Run `detect` first; if it finds nothing, run
`lsusb -d 1b1c:` and tell me the LCD's id so this can be adjusted.
"""

import argparse
import io
import math
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


def load_font(size):
    for p in FONT_CANDIDATES:
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
    finally:
        h.close()


if __name__ == "__main__":
    main()
