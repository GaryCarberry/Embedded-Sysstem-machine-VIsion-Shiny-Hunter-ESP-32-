# ESP32 + Webcam port of hackastan/pokemon-automation's shiny_hunter.py

## What changed and why

| Piece | Original | This port | Why |
|---|---|---|---|
| Controller | Raspberry Pi Pico H, native USB HID, wired straight into the Switch | Plain ESP32 (ESP-WROOM-32, no native USB) flashed with `UARTSwitchCon`'s `PRO-UART0.bin`, pairs to the Switch **over Bluetooth** as a Pro Controller | A plain ESP32 has no USB device hardware, so it can't emulate a wired USB gamepad like the Pico can. It *does* have classic Bluetooth, and Nintendo's own Pro Controller/Joy-Con Bluetooth HID protocol has already been reverse-engineered and packaged as ready-to-flash firmware — no reason to write that from scratch. |
| PC ↔ controller link | USB serial to Pico, plain text commands (`A\n`, `STOP\n`) | USB serial to ESP32 (19200 baud), binary 9-byte packets with a CRC8 and a short handshake — see `ns_controller.py` | This is the wire format `PRO-UART0.bin` actually expects. `ns_controller.py` wraps it behind the same `send(ctrl, "A", delay=0.5)` style calls the original script used, so the game-logic parts of `shiny_hunter.py` barely had to change. |
| Video in | USB capture card, HDMI straight from the Switch, forced to 1920x1080 | Any webcam, native resolution, pointed at the TV | You asked for webcam instead of capture card. This also means real-world glare, moiré, and focus blur enter the picture — see the calibration note below. |
| Cabling | Pico H's own USB port (HID) + a Raspberry Pi Debug Probe on a second USB port (serial commands) | One USB-C↔USB-C cable, ESP32 → dock's USB-A port (via a USB-C-to-A adapter, or USB-A cable if your ESP32 board has micro-USB) | Bluetooth replaces the wired HID link, so you don't need a second cable/debug probe at all — the ESP32's single USB port just carries serial commands from the PC. |

## One-time setup

1. **Get the firmware.** Either build it from [nullstalgia/UARTSwitchCon](https://github.com/nullstalgia/UARTSwitchCon) or grab the prebuilt `PRO-UART0.bin` (mirrored in [shinyypig/pynscontroller/bin](https://github.com/shinyypig/pynscontroller/tree/main/bin)).
2. **Flash it:**
   ```
   pip install esptool
   python -m esptool --port /dev/ttyUSB0 --baud 230400 write_flash 0x0 PRO-UART0.bin
   ```
   (Windows: port will be `COMx`. This baud rate — 230400 — is only for flashing; the runtime serial link in `ns_controller.py` uses 19200, per the protocol doc.)
3. **Wire it up:** plug the ESP32 into your PC over USB (for serial commands), and separately pair it to the Switch over Bluetooth: Switch → **Change Grip/Order** → it should show up as a controller. Do this pairing once; it should reconnect automatically after that.
4. **Set `COM_PORT`** at the top of `esp32_shiny_hunter.py` to the ESP32's serial port.
5. **Point your webcam at the TV**, run `tools/find_devices.py`-equivalent (or just try index 0, 1, 2... — `esp32_shiny_hunter.py` auto-detects if you leave `CAPTURE_INDEX = None`).

## Before you actually hunt: recalibrate detection

The original detection region (`STAR_REGION_X1..Y2`) and HSV thresholds (`STAR_HUE_LOW..STAR_VAL_HIGH`) were tuned for a clean 1920x1080 HDMI capture. A webcam pointed at a TV screen will differ in:
- **Framing/aspect** — your webcam's crop of the TV won't match the capture card's exact frame, so the fractional region coordinates need to be re-found.
- **Color accuracy** — webcam auto white-balance and glare will shift the yellow star's hue/saturation, usually requiring a *wider* hue range and *lower* saturation floor than the original thresholds.
- **Focus/moiré** — screen pixels re-photographed by a webcam produce a subtly different look than direct capture; a slightly higher pixel-count threshold can help avoid false positives from moiré patterns.

Use `TESTMACRO` in the running script (prints yellow-pixel counts live) to dial these in against your actual setup before trusting the automated loop.

## Files in this port
- `ns_controller.py` — the ESP32 serial driver (handshake, packet framing, CRC8, button/dpad state)
- `esp32_shiny_hunter.py` — the ported shiny-hunter script (webcam capture + same detection/reset logic as the original, talking to `ns_controller.py` instead of raw Pico serial)

## Dependencies
```
pip install opencv-python numpy pyserial obsws-python
```
