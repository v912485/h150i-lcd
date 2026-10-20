# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Python CLI (`h150i-lcd.py`) that pushes frames to the round 480×480
LCD on a **Corsair H150i ELITE LCD XT** cooler. There is no daemon: each invocation
opens the LCD's USB HID interface, sends one frame (or a brightness/rotation command),
and exits. The screen holds the last frame until something changes it; `--watch` modes
re-push on an interval so the pump firmware can't reclaim the screen.

## Run it

```bash
./h150i-lcd.py detect                 # find the LCD, print its HID path/id
./h150i-lcd.py text "Hello 42C"
./h150i-lcd.py text "CPU OK" --fg 00e070 --bg 101015 --size 96
./h150i-lcd.py image photo.png
./h150i-lcd.py cores --watch          # per-core CPU load as a radial dial (needs psutil)
./h150i-lcd.py clock --watch          # analog clock; --smooth for a sweeping hand
./h150i-lcd.py usage counter --watch  # Claude Code token usage; styles: counter/money/robot/gauge
sudo ./h150i-lcd.py power --watch     # CPU (RAPL, root-only) + GPU (amdgpu/nvidia-smi) watts
./h150i-lcd.py brightness 80          # 0-100
./h150i-lcd.py rotate 0               # 0,1,2,3 = up/right/down/left
```

Dependencies (`requirements.txt`): `hid` (hidapi binding, plus system `libhidapi-hidraw0`),
`Pillow`, and `psutil` for `cores`. Install with `pip install -r requirements.txt`
(plus `sudo apt install libhidapi-hidraw0`). HID writes need root or a udev rule granting
`uaccess` on the device (rule template is in the script's module docstring and README).

There are no tests, build step, or linter in this repo.

## Architecture

The flow is the same for every visual command: **render a 480×480 RGB `PIL.Image` →
JPEG-encode → chunk into HID packets → write**. The LCD only accepts JPEG frames, so
even text is rasterized to an image.

- **Renderers** each return a `PIL.Image`: `render_text` (auto-fits/word-wraps text
  inside the circular safe area), `render_cores` (per-core load ring + average hub),
  `render_clock`, `load_image` (cover-fit + center-crop a file), and the four
  `render_usage_*` styles (`money`/`robot`/`gauge`/`counter`). `render_cores`,
  `render_clock`, and the usage renderers supersample 2× then downscale for smooth
  edges. `psutil` measures CPU load *between* successive calls, so `cores --watch`
  primes once and reads on each tick, while the one-shot `cores` path takes a blocking
  0.4s measurement instead.
- **`usage`** is the only renderer whose data isn't the local machine's hardware:
  `collect_usage(window)` scans Claude Code's own JSONL transcripts under
  `~/.claude/projects/` (or `$CLAUDE_CONFIG_DIR`), deduping lines by
  `(message.id, requestId)` so streamed/retried replies aren't double-counted, and
  sums the `message.usage` token fields. Cost is a rough estimate from a hardcoded
  per-model rate table (`USAGE_RATES`) — labeled as such, not a real subscription bill.
  It's pure/testable: no HID device needed to render a frame. `make_usage_renderer`
  re-scans on every watch tick, so live usage updates without a daemon.
- **`power`**: `PowerSampler` reads CPU package power as a delta of the RAPL
  `energy_uj` counters (so its first read only primes the counters, and wraps are
  handled via `max_energy_range_uj`), plus GPU power from `amdgpu` hwmon or
  `nvidia-smi`. `make_power_renderer` integrates watts over time into Wh, which
  drives the meter disc's angle (`POWER_WH_PER_REV`) and the register.
  `render_power` is pure, so you can preview it without the device. It prefers
  Noto condensed fonts and falls back to `FONT_CANDIDATES`.
- **Transport** is `build_packets` → `write_packets`. Frames are split into 1016-byte
  chunks inside 1024-byte HID reports (report id `0x02`, `0x05`); the last chunk sets
  byte 3 to `0x01` to trigger the render. `build_packets` is built once and reused
  across re-pushes in watch loops.
- **Watch loops**: `watch_frame` re-pushes a fixed JPEG; `watch_render` calls a zero-arg
  renderer each tick (for live `cores`/`clock`) and aligns sleeps to wall-clock
  interval boundaries so a clock lands on each second. `clock`'s default interval varies
  by mode (1.0s tick / 0.2s `--smooth`).
- **Device control** (`set_brightness`, `set_rotation`) uses HID feature reports
  (`0x03, 0x0b` / `0x03, 0x0c`), not frame writes.

## Hardware gotchas

- The LCD is a **separate HID device** from the pump/fan controller, which is why
  `liquidctl list` doesn't show it. Vendor `0x1B1C`, candidate product ids `0x0C39`
  / `0x0C33` (`LCD_PRODUCT_IDS`). If `detect` finds nothing, check `lsusb -d 1b1c:`
  and add the real product id to that tuple.
- The protocol (frame upload / brightness / rotation) is reproduced from the
  **OpenLinkHub Commander Core** driver.
- The panel is square but only a circle is visible. `SAFE_RADIUS_FRAC` (0.90) keeps
  content inside the bezel; renderers fit their layout to that inscribed circle.

## Deployment

`h150i-lcd.service` is a systemd unit that runs a `--watch` command as root (so no udev
rule is needed). Its `ExecStart` hardcodes the absolute path to `h150i-lcd.py` and the
message/command — edit both when relocating the repo or changing what's displayed.
