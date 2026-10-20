# h150i-lcd

Push text, images, a live CPU dial, or an analog clock to the round LCD on a
**Corsair H150i ELITE LCD XT** liquid cooler — from Linux, with no Corsair iCUE
software required.

It's a single Python script. Each run opens the LCD's USB HID interface, sends
one frame (or a brightness/rotation command), and exits. The screen holds the
last frame until something changes it; `--watch` modes keep re-pushing so the
pump firmware can't reclaim the screen with its own default.

The LCD only accepts JPEG frames, so even "display text" works by rendering the
text to a 480×480 image and uploading that.

## Features

- **`text`** — render a message, auto-fit and word-wrapped to the round screen
- **`image`** — show any image file (cover-fit and center-cropped)
- **`cores`** — live per-core CPU load as a radial "reactor dial" with an average hub
- **`clock`** — an analog clock with ticking (or sweeping) hands
- **`usage`** — your Claude Code token usage, displayed with (comical) flair — pick a
  style: burning money, a thirsty robot, a fuel gauge, or a doomsday counter
- **`power`** — live CPU + GPU power draw, styled as an old electricity meter with a
  spinning disc and a rolling Wh register
- **`brightness`** / **`rotate`** — adjust the panel

## Requirements

- A Corsair H150i ELITE LCD XT (or a sibling with the same LCD module — see
  [Compatibility](#compatibility))
- Python 3.8+
- System library `libhidapi-hidraw0`
- Python packages: `hid`, `Pillow`, and `psutil` (only needed for `cores`)

```bash
sudo apt install libhidapi-hidraw0 python3-pil
pip install -r requirements.txt
```

## Permissions

HID writes to the LCD need root, **or** a udev rule so your user can write to the
device without `sudo`. Create the rule, then re-plug the cooler or reboot:

```
# /etc/udev/rules.d/99-corsair-lcd.rules
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1b1c", ATTRS{idProduct}=="0c39", MODE="0660", TAG+="uaccess"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1b1c", ATTRS{idProduct}=="0c33", MODE="0660", TAG+="uaccess"
```

## Usage

```bash
./h150i-lcd.py detect                 # find the LCD and print its HID path/id

./h150i-lcd.py text "Hello 42C"
./h150i-lcd.py text "CPU OK" --fg 00e070 --bg 101015 --size 96
./h150i-lcd.py text "Line one\nLine two"

./h150i-lcd.py image photo.png

./h150i-lcd.py cores --watch          # live per-core CPU dial (needs psutil)
./h150i-lcd.py clock --watch          # ticking analog clock
./h150i-lcd.py clock --watch --smooth # sweeping second hand

./h150i-lcd.py usage --watch          # doomsday token counter (default style)
./h150i-lcd.py usage money  --watch   # estimated $ spent, as cash on fire
./h150i-lcd.py usage robot  --watch   # a thirsty robot chugging tokens
./h150i-lcd.py usage gauge  --watch --cap 5000000   # token "fuel" gauge
./h150i-lcd.py usage counter --window all           # all-time damage report

sudo ./h150i-lcd.py power --watch     # CPU + GPU watts as an electricity meter

./h150i-lcd.py brightness 80          # 0-100
./h150i-lcd.py rotate 0               # 0,1,2,3 = up / right / down / left
```

Colors are hex like `00e070`. `--watch` re-pushes the frame on `--interval`
seconds so the firmware can't take the screen back; press Ctrl-C to stop.

### `usage` — how much have you asked Claude today?

`usage` reads Claude Code's own session transcripts under `~/.claude/projects/`
and tallies the tokens. No login or API key — it just totals what's already on
disk. Styles: `counter` (default), `money`, `robot`, `gauge`. Scope it with
`--window today` (default), `24h`, or `all`.

Because HID writes usually need root, this typically runs under `sudo` — where
`~` would resolve to `/root` and find nothing. It handles that automatically by
falling back to the `sudo`-invoking user's home. If it still shows `0`, point it
at your config dir explicitly with `--claude-dir /home/you/.claude` (or set
`CLAUDE_CONFIG_DIR`). A run that finds no transcripts prints a hint to stderr.

The dollar figure in the `money` and `counter` styles is a **tongue-in-cheek
estimate** from public per-token rates — on a subscription plan it is not your
real bill.

### `power` — what is the machine drawing?

`power` adds up the two sensors Linux exposes without extra tools: CPU package
power from the RAPL energy counters (`/sys/class/powercap/intel-rapl:*`, which
also covers AMD Zen) and GPU power from `amdgpu` hwmon or `nvidia-smi`. The
headline is **CPU + GPU**, not wall power — the motherboard, RAM, drives, fans
and PSU losses have no sensor.

The face reads like an electricity meter. The aluminium disc in the window turns
once per 2 Wh, so it spins faster as the load goes up. The register below
counts the Wh used since the display started, and its red tenths drum rolls
between digits.

Below the register are the cost so far and the current cost per hour. Both
include the standing charge, spread over the elapsed time. Set your tariff with
`--rate` (pence/kWh, default 24.49) and `--standing` (pence/day, default 42.21).
Use `--standing 0` to see the PC's running cost alone.

RAPL counters are root-only, so run it with `sudo` (or as the systemd service).
Without root it prints a warning and shows the GPU only.

Run `./h150i-lcd.py --help` (or `<command> --help`) for all options.

## Run at boot (systemd)

A sample unit, [`h150i-lcd.service`](h150i-lcd.service), runs a `--watch` command
as root (so no udev rule is needed). Edit the absolute path and the command, then:

```bash
sudo cp h150i-lcd.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now h150i-lcd.service
```

## Compatibility

The LCD on the ELITE LCD XT is a **separate HID device** from the pump/fan
controller — which is why tools like `liquidctl list` don't show it. This script
looks for Corsair's vendor id `0x1B1C` with product id `0x0C39` or `0x0C33`.

If `detect` finds nothing, list your Corsair devices with `lsusb -d 1b1c:`. If
your LCD reports a different product id, add it to `LCD_PRODUCT_IDS` near the top
of `h150i-lcd.py`.

## Credits

The frame-upload, brightness, and rotation protocol is reproduced from the
[OpenLinkHub](https://github.com/jurkovic-nikola/OpenLinkHub) Commander Core
driver.

## License

[GPL-3.0](LICENSE)
