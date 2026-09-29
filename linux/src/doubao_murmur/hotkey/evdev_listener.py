"""evdev listener for global hotkeys via /dev/input.

OPTIONAL input method. Requires user to be in the 'input' group.
Listens for:
- Right Alt (KEY_RIGHTALT=100) press and release
- ESC (KEY_ESC=1) -> cancel
"""

from __future__ import annotations

import glob
import logging
import os
import select
import struct
import threading

logger = logging.getLogger(__name__)

# evdev constants
EV_KEY = 0x01
KEY_ESC = 1
KEY_RIGHTALT = 100

# sizeof(struct input_event) on 64-bit Linux
EVENT_SIZE = 24
EVENT_FORMAT = "llHHi"


class EvdevListener:
    """Reads /dev/input/event* devices for global hotkeys."""

    def __init__(self, on_record_press, on_record_release, on_escape) -> None:
        self.on_record_press = on_record_press
        self.on_record_release = on_record_release
        self.on_escape = on_escape
        self._thread: threading.Thread | None = None
        self._running = False
        self._right_alt_down = False
        self._other_key_pressed = False

    @staticmethod
    def is_available() -> bool:
        """Check if any evdev devices are accessible."""
        for path in sorted(glob.glob("/dev/input/event*")):
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                os.close(fd)
                return True
            except PermissionError:
                continue
        return False

    def start(self) -> bool:
        """Start listening. Returns False if no accessible devices."""
        devices = self._find_keyboard_devices()
        if not devices:
            logger.warning("No accessible evdev input devices found")
            return False

        self._running = True
        self._thread = threading.Thread(
            target=self._listen_loop, args=(devices,), daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def _find_keyboard_devices(self) -> list[str]:
        """Find /dev/input/event* files that are readable."""
        accessible = []
        for path in sorted(glob.glob("/dev/input/event*")):
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                os.close(fd)
                accessible.append(path)
            except PermissionError:
                continue
        return accessible

    def _listen_loop(self, devices: list[str]) -> None:
        """Main read loop using select() for multiplexing."""
        fds: dict[int, str] = {}
        for path in devices:
            try:
                fd = os.open(path, os.O_RDONLY)
                fds[fd] = path
            except Exception as e:
                logger.warning("Cannot open %s: %s", path, e)

        if not fds:
            return

        buf_size = EVENT_SIZE * 16

        try:
            while self._running:
                readable, _, _ = select.select(list(fds.keys()), [], [], 0.5)
                for fd in readable:
                    try:
                        data = os.read(fd, buf_size)
                    except OSError:
                        continue
                    for i in range(0, len(data), EVENT_SIZE):
                        if i + EVENT_SIZE > len(data):
                            break
                        event = struct.unpack(
                            EVENT_FORMAT, data[i : i + EVENT_SIZE]
                        )
                        ev_type = event[2]
                        ev_code = event[3]
                        ev_value = event[4]

                        if ev_type != EV_KEY:
                            continue

                        self._handle_key(ev_code, ev_value)
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _handle_key(self, keycode: int, value: int) -> None:
        if keycode == KEY_RIGHTALT:
            if value == 1 and not self._right_alt_down:  # press
                self._right_alt_down = True
                self._other_key_pressed = False
                self.on_record_press()
            elif value == 0 and self._right_alt_down:  # release
                self.on_record_release(not self._other_key_pressed)
                self._right_alt_down = False
        elif value == 1:
            if self._right_alt_down:
                self._other_key_pressed = True
            if keycode == KEY_ESC:
                self.on_escape()
