"""
ESP32 FireRed Shiny Hunter (webcam edition)

Ported from hackastan/pokemon-automation's shiny_hunter.py, which used a
Raspberry Pi Pico H (native USB HID, wired) and a capture card. This version:
  - Talks to a plain ESP32 flashed with UARTSwitchCon's PRO-UART0 firmware,
    which pairs to the Switch over Bluetooth as a Pro Controller (see
    ns_controller.py for the wire protocol and one-time flashing steps).
  - Reads frames from a webcam pointed at the TV instead of a capture card
    reading HDMI directly, so detection has to tolerate glare/moire/blur.

Requires: pip install opencv-python pyserial numpy obsws-python

Type STARTMACRO and press Enter to start the hunt.
Type STOPMACRO and press Enter to stop the macro (program keeps running).
Type EXIT to close the program.
Type TESTMACRO to run the shiny detection check on the current screen.
Type A, B, X, Y, UP, DOWN, LEFT, RIGHT, HOME, STOP, or ABXY for manual commands.
Ctrl+C also stops at any time.

OBS Setup (optional, only needed if you want replay clips saved):
- Tools -> WebSocket Server Settings -> Enable, port 4455, no auth
- Settings -> Output -> Replay Buffer -> Enable, set to 600 seconds
- Click Start Replay Buffer before running this script

Logs are saved to shiny_hunter_YYYY-MM-DD_HH-MM-SS.log in the same folder as
this script.
"""

import cv2
import time
import random
import numpy as np
import threading
import sys
import os
from datetime import datetime

import ns_controller

# Try to import OBS websocket -- gracefully handle if not available
try:
    import obsws_python as obs

    OBS_AVAILABLE = True
except ImportError:
    OBS_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────
COM_PORT = "COM3"  # the ESP32's serial port -- Windows: "COM5", Mac/Linux: "/dev/ttyUSB0" or "/dev/ttyACM0"

# Set to None to auto-detect the webcam, or set a specific number (0, 1, 2...) to force it
CAPTURE_INDEX = None

# Windows webcams often hang/return no frames on OpenCV's default MSMF backend.
# Force DirectShow. Set to None on Mac/Linux (cv2.CAP_DSHOW is Windows-only).
CAPTURE_BACKEND = cv2.CAP_DSHOW

# Detection region — calibrated against a real shiny (Squirtle) and a real
# non-shiny (Pidgey) summary screen on this exact webcam/TV/lighting setup.
# Re-run tools/star_test.py if you move the camera, change lighting, or the
# console shifts position, since these are fractional coords tied to framing.
# Fixed star location calibration
STAR_CENTER_X = 368
STAR_CENTER_Y = 225
STAR_BOX_SIZE = 40

# Star color thresholds — measured directly off the real shiny star pixels
# (hue 8-20, sat as low as ~25, val ~110-170 under this webcam's glare/white
# balance). Confirmed 0 false-positive pixels against a real non-shiny frame
# using the same region+thresholds.
# Recalibrated for YOUR webcam

STAR_HUE_LOW = 16
STAR_HUE_HIGH = 28

STAR_SAT_LOW = 80
STAR_SAT_HIGH = 170

STAR_VAL_LOW = 150
STAR_VAL_HIGH = 255

# Pixel counts observed during calibration: real shiny star ~240-337px in this
# crop, real non-shiny ~0px. THRESHOLD is the "definitely shiny" cutoff.
# BORDERLINE_LOW is a safety net -- a count between BORDERLINE_LOW and
# STAR_PIXEL_THRESHOLD pauses the macro for a manual look instead of silently
# treating it as "not shiny", in case lighting drifts from the calibration
# session and shrinks the measured star a bit.
STAR_PIXEL_THRESHOLD = 60
STAR_PIXEL_BORDERLINE_LOW = 20

# Random wait after ABXY soft reset (seconds)
RESET_WAIT_MIN = 5.00
RESET_WAIT_MAX = 5.50

# Seconds to wait after non-detection before second check
PRE_RESET_WAIT = 3

# OBS Websocket settings
OBS_HOST = "localhost"
OBS_PORT = 4455
OBS_PASSWORD = ""  # Leave empty if auth is disabled

# ──────────────────────────────────────────────────────────────────────────────

MANUAL_COMMANDS = ["A", "B", "X", "Y", "UP", "DOWN", "LEFT", "RIGHT", "HOME", "STOP", "ABXY"]

stop_flag = threading.Event()
exit_flag = threading.Event()  # set this to kill the program cleanly
pause_flag = threading.Event()  # set when a borderline read needs a manual look


# ── Logging setup ─────────────────────────────────────────────────────────────
class Logger:
    def __init__(self, log_path):
        self.terminal = sys.stdout
        self.log_file = open(log_path, "w", encoding="utf-8", buffering=1)
        self.log_path = log_path

    def write(self, message):
        self.terminal.write(message)
        self.log_file.write(message)

    def flush(self):
        self.terminal.flush()
        self.log_file.flush()

    def close(self):
        self.log_file.close()


def setup_logging():
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(script_dir, f"shiny_hunter_{timestamp}.log")
    logger = Logger(log_path)
    sys.stdout = logger
    print(f"Logging to: {log_path}")
    return logger


# ──────────────────────────────────────────────────────────────────────────────


def open_capture(index):
    if CAPTURE_BACKEND is not None:
        return cv2.VideoCapture(index, CAPTURE_BACKEND)
    return cv2.VideoCapture(index)


def find_capture_index():
    print("Auto-detecting webcam index...")
    for i in range(6):
        cap = open_capture(i)
        if cap.isOpened():
            for _ in range(10):
                ret, frame = cap.read()
                if ret and frame is not None:
                    h, w = frame.shape[:2]
                    print(f"  Index {i}: {w}x{h}")
                    cap.release()
                    return i
                time.sleep(0.2)
            print(f"  Index {i}: opened but no frame.")
            cap.release()
        else:
            print(f"  Index {i}: no device.")
    return None


def save_obs_replay():
    if not OBS_AVAILABLE:
        print("  [OBS replay save skipped — obsws-python not installed]")
        return
    try:
        print("  [Saving OBS replay buffer...]")
        cl = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=3)
        cl.save_replay_buffer()
        print("  [OBS replay buffer saved!]")
    except Exception as e:
        print(f"  [OBS replay save failed: {e}]")
        print("  [Make sure OBS is open, WebSocket is enabled, and Replay Buffer is running]")


def send(ctrl, cmd, delay=0):
    if stop_flag.is_set():
        return
    print(f"  >> PRESS {cmd}")
    ctrl.send(cmd)
    if delay > 0:
        print(f"  waiting {delay}s...")
        end = time.time() + delay
        while time.time() < end:
            if stop_flag.is_set():
                return
            time.sleep(0.1)


def interruptible_sleep(seconds):
    end = time.time() + seconds
    while time.time() < end:
        if stop_flag.is_set():
            return
        time.sleep(0.1)


def handle_command(ctrl, cmd):
    if cmd in MANUAL_COMMANDS:
        print(f"  >> PRESS {cmd}")
        ctrl.send(cmd)
    else:
        print(f"  [unknown command: {cmd}]")


def detect_shiny_star(frame):
    """Returns (is_shiny, is_borderline, pixel_count, region, mask)."""

    x1 = STAR_CENTER_X - STAR_BOX_SIZE // 2
    y1 = STAR_CENTER_Y - STAR_BOX_SIZE // 2

    x2 = STAR_CENTER_X + STAR_BOX_SIZE // 2
    y2 = STAR_CENTER_Y + STAR_BOX_SIZE // 2

    region = frame[y1:y2, x1:x2]

    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)

    lower = np.array([
        STAR_HUE_LOW,
        STAR_SAT_LOW,
        STAR_VAL_LOW
    ])

    upper = np.array([
        STAR_HUE_HIGH,
        STAR_SAT_HIGH,
        STAR_VAL_HIGH
    ])

    mask = cv2.inRange(hsv, lower, upper)

    yellow_pixels = cv2.countNonZero(mask)

    is_shiny = yellow_pixels >= STAR_PIXEL_THRESHOLD
    is_borderline = (
        not is_shiny
        and yellow_pixels >= STAR_PIXEL_BORDERLINE_LOW
    )

    return (
        is_shiny,
        is_borderline,
        yellow_pixels,
        region,
        mask
    ) 


def grab_frame(cap, retries=20):
    for i in range(retries):
        ret, frame = cap.read()
        if ret and frame is not None:
            return frame
        time.sleep(0.3)
    print(f"  [frame grab failed after {retries} retries]")
    return None


def show_debug(region, mask, window_title="Detection Region | Mask"):
    debug_mask = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
    combined = np.hstack([region, debug_mask])
    cv2.imshow(window_title, combined)
    cv2.waitKey(1)


def save_borderline_frame(frame, pixel_count, tag="borderline"):
    """Save the full frame + timestamp so a borderline read can be reviewed later."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(script_dir, f"{tag}_{ts}_{pixel_count}px.png")
    cv2.imwrite(path, frame)
    print(f"  [saved review frame: {path}]")


def run_test_macro(cap):
    """Run the full detection sequence on the current screen and report results."""
    print("\n── TESTMACRO ──────────────────────────────────────")
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] Running detection test on current screen...")
    print("  [CHECK 1 — grabbing frame]")
    frame = grab_frame(cap)
    if frame is None:
        print("  [CHECK 1 FAILED — could not grab frame]")
        print("── TESTMACRO END ──────────────────────────────────\n")
        return

    is_shiny, is_borderline, pixel_count, region, mask = detect_shiny_star(frame)
    status = "SHINY" if is_shiny else "NOT SHINY"
    print(f"  [CHECK 1 — yellow pixels: {pixel_count} | threshold: {STAR_PIXEL_THRESHOLD}] -> {status}")
    if is_borderline:
        print(f"  [note: close to threshold — {pixel_count}px vs {STAR_PIXEL_THRESHOLD} needed]")
    show_debug(region, mask)

    if is_shiny:
        print("  [TESTMACRO RESULT: SHINY]")
        print("── TESTMACRO END ──────────────────────────────────\n")
        return

    print(f"  [waiting {PRE_RESET_WAIT}s before second check...]")
    for i in range(PRE_RESET_WAIT, 0, -1):
        print(f"  [{i}]")
        time.sleep(1)

    print("  [CHECK 2 — grabbing frame]")
    frame2 = grab_frame(cap)
    if frame2 is None:
        print("  [CHECK 2 FAILED — could not grab frame]")
        print("── TESTMACRO END ──────────────────────────────────\n")
        return

    is_shiny2, is_borderline2, pixel_count2, region2, mask2 = detect_shiny_star(frame2)
    status2 = "SHINY" if is_shiny2 else "NOT SHINY"
    print(f"  [CHECK 2 — yellow pixels: {pixel_count2} | threshold: {STAR_PIXEL_THRESHOLD}] -> {status2}")
    if is_borderline2:
        print(f"  [note: close to threshold — {pixel_count2}px vs {STAR_PIXEL_THRESHOLD} needed]")
    show_debug(region2, mask2)

    print(f"  [TESTMACRO RESULT: {status2}]")
    print("── TESTMACRO END ──────────────────────────────────\n")


def run_reset_sequence(ctrl, first_run=False):
    print("Starting reset sequence...")
    if stop_flag.is_set():
        return
    if first_run:
        print("  [initial soft reset]")
        ctrl.send("ABXY")
    wait = round(random.uniform(RESET_WAIT_MIN, RESET_WAIT_MAX), 2)
    print(f"  [waiting {wait}s after reset...]")
    interruptible_sleep(wait)
    if stop_flag.is_set():
        return

    steps = [
        ("A", 0.5),
        ("A", 0.5),
        ("A", 3.5),
        ("A", 1.5),
        ("B", 2.5),
        ("A", 1.5),
        ("B", 1.5),
        ("A", 1.5),
        ("B", 5),
        ("B", 3.0),
        ("B", 5),
        ("X", 0.5),
        ("A", 1),
        ("A", 1),
        ("A", 2),
    ]
    for btn, delay in steps:
        if stop_flag.is_set():
            return
        send(ctrl, btn, delay=delay)
    print("  [sequence complete — checking for shiny]")


def run_shiny_sequence(ctrl):
    print("SHINY DETECTED — saving replay buffer then running save sequence...")
    save_obs_replay()
    interruptible_sleep(2)
    send(ctrl, "B", delay=2)
    send(ctrl, "B", delay=2)
    send(ctrl, "B", delay=2)
    send(ctrl, "DOWN", delay=2)
    send(ctrl, "DOWN", delay=2)
    send(ctrl, "DOWN", delay=2)
    send(ctrl, "A", delay=2)
    send(ctrl, "A", delay=2)
    send(ctrl, "A", delay=2)
    send(ctrl, "STOP")
    print("Done. Shiny saved!")


def soft_reset(ctrl):
    if stop_flag.is_set():
        return
    print("  >> SOFT RESET (ABXY)")
    ctrl.send("ABXY")
    print("  [waiting for reset to complete...]")
    interruptible_sleep(3.0)


def pre_reset_countdown():
    print(f"  [not shiny — second check in {PRE_RESET_WAIT}s... Ctrl+C to abort]")
    for i in range(PRE_RESET_WAIT, 0, -1):
        if stop_flag.is_set():
            return True
        print(f"  [{i}]")
        time.sleep(1)
    return stop_flag.is_set()


def handle_borderline(frame, pixel_count):
    """A borderline read pauses the macro instead of silently resetting past it.
    The person running the hunt has to explicitly say whether it's shiny or not."""
    save_borderline_frame(frame, pixel_count, tag="borderline")
    print("\n" + "!" * 60)
    print(f"  BORDERLINE READ — {pixel_count} pixels (threshold is {STAR_PIXEL_THRESHOLD},")
    print(f"  borderline floor is {STAR_PIXEL_BORDERLINE_LOW}). This is NOT auto-treated")
    print("  as 'not shiny' -- the macro is paused so you can look at the screen.")
    print("  A review frame was saved next to this script.")
    print("  Type YES if it's actually shiny, NO to continue the hunt, Ctrl+C to stop.")
    print("!" * 60)
    while True:
        try:
            ans = input("  Is it shiny? [YES/NO] > ").strip().upper()
        except Exception:
            return False
        if ans == "YES":
            return True
        if ans == "NO":
            return False
        print("  Please type YES or NO.")


def listen_for_commands(ctrl, cap):
    while not stop_flag.is_set() and not exit_flag.is_set():
        try:
            cmd = input()
            cmd = cmd.strip().upper()
            if cmd == "STOPMACRO":
                print("\n[STOPMACRO received — stopping hunt, program still running]")
                stop_flag.set()
                break
            elif cmd == "EXIT":
                print("\n[EXIT received — shutting down]")
                stop_flag.set()
                exit_flag.set()
                break
            elif cmd == "TESTMACRO":
                threading.Thread(target=run_test_macro, args=(cap,), daemon=True).start()
            else:
                handle_command(ctrl, cmd)
        except Exception:
            break


def wait_for_startmacro(ctrl, cap):
    """Drop back to a prompt after STOPMACRO. Returns False if EXIT was typed."""
    print("\n[Stopped — type STARTMACRO to run again, EXIT to quit]")
    while True:
        try:
            cmd = input("> ").strip().upper()
        except Exception:
            return False
        if cmd == "STARTMACRO":
            print(f"Hunt restarted — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            return True
        elif cmd == "EXIT":
            print("[EXIT received — shutting down]")
            exit_flag.set()
            return False
        elif cmd == "TESTMACRO":
            threading.Thread(target=run_test_macro, args=(cap,), daemon=True).start()
        else:
            handle_command(ctrl, cmd)


def main():
    logger = setup_logging()
    print(f"Hunt started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    print(f"Opening ESP32 on {COM_PORT}...")
    ctrl = ns_controller.NSController(COM_PORT)
    print("ESP32 connected and handshake complete.")

    capture_index = CAPTURE_INDEX
    if capture_index is None:
        capture_index = find_capture_index()
        if capture_index is None:
            print("ERROR: Could not find any webcam.")
            ctrl.close()
            sys.stdout = logger.terminal
            logger.close()
            return
    else:
        print(f"Using configured webcam index: {capture_index}")

    print(f"Opening webcam at index {capture_index}...")
    cap = open_capture(capture_index)
    if not cap.isOpened():
        print(f"ERROR: Could not open webcam at index {capture_index}.")
        ctrl.close()
        sys.stdout = logger.terminal
        logger.close()
        return
    print("Webcam open.")

    print("Warming up capture feed...")
    for _ in range(10):
        cap.read()
        time.sleep(0.1)
    ret, test_frame = cap.read()
    if ret and test_frame is not None:
        print(f"Capture resolution: {test_frame.shape[1]}x{test_frame.shape[0]}")
    else:
        print("[WARNING] Could not read test frame — webcam may be unstable.")

    if OBS_AVAILABLE:
        try:
            cl = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=3)
            print("OBS WebSocket connected.")
        except Exception as e:
            print(f"[WARNING] Could not connect to OBS WebSocket: {e}")
            print("[WARNING] Replay buffer will NOT be saved on shiny detection.")
    else:
        print("[WARNING] obsws-python not installed. OBS replay save disabled.")

    print("\nCommands: A B X Y UP DOWN LEFT RIGHT HOME STOP ABXY")
    print("Type STARTMACRO to begin the hunt, STOPMACRO to stop, EXIT to quit.")
    print("Type TESTMACRO to test detection on the current screen.\n")

    while True:
        try:
            cmd = input("> ")
        except Exception:
            break
        cmd = cmd.strip().upper()
        if cmd == "STARTMACRO":
            print(f"Hunt started — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            break
        elif cmd == "EXIT":
            print("[EXIT received — shutting down]")
            exit_flag.set()
            ctrl.close()
            sys.stdout = logger.terminal
            logger.close()
            return
        elif cmd == "TESTMACRO":
            run_test_macro(cap)
        else:
            handle_command(ctrl, cmd)

    attempt = 0
    try:
        while not exit_flag.is_set():
            stop_flag.clear()
            stop_thread = threading.Thread(target=listen_for_commands, args=(ctrl, cap), daemon=True)
            stop_thread.start()

            while not stop_flag.is_set() and not exit_flag.is_set():
                attempt += 1
                loop_start = time.time()
                print(f"\n── Attempt {attempt} — {datetime.now().strftime('%H:%M:%S')} ──────────────────")
                run_reset_sequence(ctrl, first_run=(attempt == 1))
                if stop_flag.is_set() or exit_flag.is_set():
                    break
                interruptible_sleep(0.5)

                # ── First detection check ──────────────────────────────────
                print("  [CHECK 1 — grabbing frame]")
                frame = grab_frame(cap)
                if frame is None:
                    print("WARNING: Check 1 frame grab failed. Skipping to reset.")
                    soft_reset(ctrl)
                    elapsed = time.time() - loop_start
                    print(f"  [loop time: {elapsed:.1f}s]")
                    continue

                is_shiny, is_borderline, pixel_count, region, mask = detect_shiny_star(frame)
                status = "SHINY!" if is_shiny else ("BORDERLINE" if is_borderline else "not shiny")
                print(f"  [CHECK 1 — yellow pixels: {pixel_count} | threshold: {STAR_PIXEL_THRESHOLD}] -> {status}")
                show_debug(region, mask)

                if is_shiny:
                    elapsed = time.time() - loop_start
                    print(f"  [loop time: {elapsed:.1f}s]")
                    run_shiny_sequence(ctrl)
                    stop_flag.set()
                    break

                if is_borderline:
                    if handle_borderline(frame, pixel_count):
                        run_shiny_sequence(ctrl)
                        stop_flag.set()
                        break
                    # confirmed not shiny -- fall through to normal flow below

                # ── Countdown then second detection check ──────────────────
                aborted = pre_reset_countdown()
                if aborted:
                    elapsed = time.time() - loop_start
                    print(f"  [loop time: {elapsed:.1f}s]")
                    break

                print("  [CHECK 2 — grabbing frame]")
                frame2 = grab_frame(cap)
                if frame2 is None:
                    print("WARNING: Check 2 frame grab failed. Proceeding to reset.")
                    soft_reset(ctrl)
                    elapsed = time.time() - loop_start
                    print(f"  [loop time: {elapsed:.1f}s]")
                    continue

                is_shiny2, is_borderline2, pixel_count2, region2, mask2 = detect_shiny_star(frame2)
                status2 = "SHINY!" if is_shiny2 else ("BORDERLINE" if is_borderline2 else "not shiny")
                print(f"  [CHECK 2 — yellow pixels: {pixel_count2} | threshold: {STAR_PIXEL_THRESHOLD}] -> {status2}")
                show_debug(region2, mask2)

                if is_shiny2:
                    elapsed = time.time() - loop_start
                    print(f"  [loop time: {elapsed:.1f}s]")
                    run_shiny_sequence(ctrl)
                    stop_flag.set()
                    break

                if is_borderline2:
                    if handle_borderline(frame2, pixel_count2):
                        elapsed = time.time() - loop_start
                        print(f"  [loop time: {elapsed:.1f}s]")
                        run_shiny_sequence(ctrl)
                        stop_flag.set()
                        break
                    # confirmed not shiny -- reset and continue

                soft_reset(ctrl)
                elapsed = time.time() - loop_start
                print(f"  [loop time: {elapsed:.1f}s]")

            stop_thread.join(timeout=0.5)
            if exit_flag.is_set():
                break

            # STOPMACRO was hit — wait for STARTMACRO or EXIT
            should_continue = wait_for_startmacro(ctrl, cap)
            if not should_continue:
                break

    except KeyboardInterrupt:
        print("\nStopped by keyboard.")
    finally:
        try:
            ctrl.send("STOP")
        except Exception:
            pass
        cap.release()
        cv2.destroyAllWindows()
        ctrl.close()
        print(f"\nHunt ended after {attempt} attempts at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Log saved to: {logger.log_path}")
        sys.stdout = logger.terminal
        logger.close()


if __name__ == "__main__":
    main()