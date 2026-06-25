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

./h150i-lcd.py brightness 80          # 0-100
./h150i-lcd.py rotate 0               # 0,1,2,3 = up / right / down / left
```

Colors are hex like `00e070`. `--watch` re-pushes the frame on `--interval`
seconds so the firmware can't take the screen back; press Ctrl-C to stop.

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
