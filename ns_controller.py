"""
ns_controller.py

UARTSwitchCon PRO-UART0 driver
ESP32 -> Nintendo Switch Pro Controller

Uses the button mapping discovered from testing:
- Byte 0: system buttons
- Byte 1: face + shoulder buttons
"""

import threading
import time
import serial


STICK_CENTER = 128


# Discovered from button testing
_BYTE0_BITS = {
    "MINUS": 0,
    "PLUS": 1,
    "LCLICK": 2,
    "RCLICK": 3,
    "HOME": 4,
}


_BYTE1_BITS = {
    "Y": 0,
    "B": 1,
    "A": 2,
    "X": 3,
    "R": 5,
    "ZL": 6,
    "ZR": 7,
}


# D-pad values
DPAD_UP = 0x00
DPAD_UP_RIGHT = 0x01
DPAD_RIGHT = 0x02
DPAD_DOWN_RIGHT = 0x03
DPAD_DOWN = 0x04
DPAD_DOWN_LEFT = 0x05
DPAD_LEFT = 0x06
DPAD_UP_LEFT = 0x07
DPAD_CENTER = 0x08


_DPAD_BITS = {
    "UP": DPAD_UP,
    "DOWN": DPAD_DOWN,
    "LEFT": DPAD_LEFT,
    "RIGHT": DPAD_RIGHT,
}


def crc8(data):

    crc = 0x00

    for byte in data:

        crc ^= byte

        for _ in range(8):

            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xff
            else:
                crc = (crc << 1) & 0xff

    return crc


class NSController:


    def __init__(self, port, baud=19200):

        self.ser = serial.Serial(
            port,
            baud,
            timeout=2
        )

        time.sleep(0.5)

        self.lock = threading.Lock()

        self._handshake()

        time.sleep(1)

        self._reset_state()

        self._send_report()



    def _read_byte(self, name):

        data = self.ser.read(1)

        if len(data) != 1:
            raise IOError(
                f"Handshake failed waiting for {name}"
            )

        return data



    def _handshake(self):

        self.ser.reset_input_buffer()

        # Chocolate handshake
        self.ser.write(bytes([0xFF]))
        self._read_byte("FF")

        self.ser.write(bytes([0x44]))
        self._read_byte("44")

        self.ser.write(bytes([0xEE]))

        controller_type = self._read_byte(
            "controller type"
        )

        print(
            "Controller type:",
            controller_type.hex()
        )



    def _reset_state(self):

        self._buttons0 = 0
        self._buttons1 = 0

        self._dpad = DPAD_CENTER

        self._lx = STICK_CENTER
        self._ly = STICK_CENTER

        self._rx = STICK_CENTER
        self._ry = STICK_CENTER



    def _send_report(self):

        payload = bytes([
            self._buttons0,
            self._buttons1,
            self._dpad,
            self._lx,
            self._ly,
            self._rx,
            self._ry,
            0x00
        ])

        packet = payload + bytes([
            crc8(payload)
        ])

        self.ser.write(packet)

        ack = self.ser.read(1)

        if ack != b"\x90":
            raise IOError(
                f"Expected ACK 90, got {ack.hex()}"
            )



    def _set_button(self, name, pressed):

        name = name.upper()


        if name in _BYTE0_BITS:

            bit = _BYTE0_BITS[name]

            if pressed:
                self._buttons0 |= (1 << bit)
            else:
                self._buttons0 &= ~(1 << bit)


        elif name in _BYTE1_BITS:

            bit = _BYTE1_BITS[name]

            if pressed:
                self._buttons1 |= (1 << bit)
            else:
                self._buttons1 &= ~(1 << bit)


        elif name in _DPAD_BITS:

            if pressed:
                self._dpad = _DPAD_BITS[name]
            else:
                self._dpad = DPAD_CENTER


        else:

            raise ValueError(
                f"Unknown button: {name}"
            )



    def press(self, buttons, hold=0.05):

        if isinstance(buttons, str):
            buttons = [buttons]


        with self.lock:

            for button in buttons:
                self._set_button(button, True)

            self._send_report()


        time.sleep(hold)


        with self.lock:

            for button in buttons:
                self._set_button(button, False)

            self._send_report()



    def set_stick(
        self,
        lx=None,
        ly=None,
        rx=None,
        ry=None
    ):

        if lx is not None:
            self._lx = lx

        if ly is not None:
            self._ly = ly

        if rx is not None:
            self._rx = rx

        if ry is not None:
            self._ry = ry


        with self.lock:
            self._send_report()



    def release_all(self):

        with self.lock:

            self._reset_state()

            self._send_report()



    def send(self, cmd, delay=0):

        cmd = cmd.upper().strip()


        if cmd == "STOP":

            self.release_all()


        elif cmd == "ABXY":

            self.press(
                ["A", "B", "X", "Y"],
                hold=0.1
            )


        else:

            self.press(cmd)



        if delay:
            time.sleep(delay)



    def close(self):

        try:
            self.release_all()

        finally:
            self.ser.close()