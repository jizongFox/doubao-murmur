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
import time

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
            except OSError:
                continue
        return False

    def start(self) -> bool:
        """Start listening. Returns False if no accessible devices."""
        if self.is_running:
            return True
        devices = self._find_keyboard_devices()
        if not devices:
            logger.debug("No accessible evdev input devices found")
            return False

        self._right_alt_down = False
        self._other_key_pressed = False
        self._running = True
        self._thread = threading.Thread(
            target=self._listen_loop, args=(devices,), daemon=True
        )
        self._thread.start()
        return True

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

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
            except OSError:
                continue
        return accessible

    @staticmethod
    def _open_devices(fds: dict[int, str], devices: list[str]) -> None:
        for path in devices:
            if path in fds.values():
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                fds[fd] = path
            except OSError as e:
                logger.debug("Cannot open %s: %s", path, e)

    def _listen_loop(self, devices: list[str]) -> None:
        """Read events and periodically add devices that became accessible."""
        fds: dict[int, str] = {}
        self._open_devices(fds, devices)
        next_scan = time.monotonic() + 2.0

        buf_size = EVENT_SIZE * 16

        try:
            while self._running:
                now = time.monotonic()
                if now >= next_scan:
                    self._open_devices(fds, sorted(glob.glob("/dev/input/event*")))
                    next_scan = now + 2.0
                # An empty fd list still waits; missing devices must not spin.
                readable, _, _ = select.select(list(fds.keys()), [], [], 0.5)
                for fd in readable:
                    try:
                        data = os.read(fd, buf_size)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError as e:
                        logger.warning("Cannot read %s: %s", fds[fd], e)
                        data = b""
                    if not data:
                        # A disconnected device remains ready in select();
                        # retaining it would turn this into a busy loop.
                        path = fds.pop(fd)
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        logger.info("Stopped listening to input device %s", path)
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
            self._running = False
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
