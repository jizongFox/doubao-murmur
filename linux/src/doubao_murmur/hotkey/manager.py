"""Hotkey manager: coordinates input methods for triggering recording.

Two backends, both started when available:
1. X11/XRecord (x11_listener) — preferred, and the only one that sees
   Steam Input's XTEST-injected controller keys.
2. evdev /dev/input (evdev_listener) — needs the `input` group, plus
   --device=input when sandboxed. The only one that works on a Wayland
   session, where keys sent to native Wayland windows never reach
   XWayland and are therefore invisible to XRecord.

`overlay_button` is vestigial: the on-screen push-to-talk button was
removed in 6555ffa and app.py passes None. Reviving it would give Wayland
users a trigger that needs no /dev/input access at all — see
hotkey/overlay_button.py.
"""

from __future__ import annotations

import logging
import time

from gi.repository import GLib

from doubao_murmur.config import (
    DEBOUNCE_INTERVAL,
    RECORDING_MODES,
    RECORDING_MODE_HOLD,
    RECORDING_MODE_TOGGLE,
)

logger = logging.getLogger(__name__)


class HotkeyManager:
    """Coordinates input methods for triggering recording."""

    def __init__(self, recording_mode: str = RECORDING_MODE_TOGGLE) -> None:
        if recording_mode not in RECORDING_MODES:
            raise ValueError(f"Unknown recording mode: {recording_mode}")
        self.on_toggle = None  # () -> None
        self.on_start = None  # () -> None
        self.on_stop = None  # () -> None
        self.on_cancel = None  # () -> None
        self.on_keyboard = None  # () -> None (toggle on-screen keyboard)
        self._overlay_button = None
        self._evdev_listener = None
        self._x11_listener = None
        self._cancel_enabled = False
        self._last_toggle_time = 0.0
        self._last_keyboard_time = 0.0
        self._recording_mode = recording_mode
        self._hold_active = False
        self._last_hold_release_time = 0.0

    def start(
        self, overlay_button=None, evdev_listener=None, x11_listener=None
    ) -> None:
        """Initialize input backends."""
        self._overlay_button = overlay_button
        self._evdev_listener = evdev_listener
        self._x11_listener = x11_listener

        if self._x11_listener:
            if self._x11_listener.start():
                logger.info("X11 key listener active")
            else:
                logger.warning("X11 key listener failed to start")
                self._x11_listener = None

        if self._evdev_listener:
            if self._evdev_listener.start():
                logger.info("evdev listener active")
            else:
                logger.warning("evdev not available (need input group?)")
                self._evdev_listener = None

    def stop(self) -> None:
        """Stop all input backends."""
        if self._x11_listener:
            self._x11_listener.stop()
        if self._evdev_listener:
            self._evdev_listener.stop()

    @property
    def has_global_hotkey(self) -> bool:
        """True when a global hotkey backend is active."""
        return (
            self._evdev_listener is not None
            or self._x11_listener is not None
        )

    def trigger_toggle(self) -> None:
        """Called by input backends, possibly from non-GTK threads.

        Debounced, then marshalled to the GTK main thread — GTK4 is not
        thread-safe and the evdev listener runs on its own thread.
        """
        now = time.monotonic()
        if now - self._last_toggle_time < DEBOUNCE_INTERVAL:
            return
        self._last_toggle_time = now
        GLib.idle_add(self._dispatch_toggle)

    def trigger_record_press(self) -> None:
        """Handle the first press of the recording hotkey."""
        if self._recording_mode != RECORDING_MODE_HOLD:
            return
        now = time.monotonic()
        if self._hold_active:
            return
        # X11 and evdev can report the same physical key. A second backend may
        # deliver its press just after the first backend delivered the release.
        if now - self._last_hold_release_time < DEBOUNCE_INTERVAL:
            return
        self._hold_active = True
        GLib.idle_add(self._dispatch_start)

    def trigger_record_release(self, valid: bool) -> None:
        """Handle release; ``valid`` is false when AltGr formed a chord."""
        if self._recording_mode == RECORDING_MODE_TOGGLE:
            if valid:
                self.trigger_toggle()
            return
        if not self._hold_active:
            return
        self._hold_active = False
        self._last_hold_release_time = time.monotonic()
        if valid:
            GLib.idle_add(self._dispatch_stop)
        else:
            GLib.idle_add(self._dispatch_cancel)

    def trigger_cancel(self) -> None:
        """Called by input backends for cancel (ESC)."""
        if self._hold_active:
            self._hold_active = False
            self._last_hold_release_time = time.monotonic()
            GLib.idle_add(self._dispatch_cancel)
            return
        if self._cancel_enabled:
            GLib.idle_add(self._dispatch_cancel)

    def trigger_keyboard(self) -> None:
        """Called by input backends for the on-screen-keyboard hotkey.

        Debounced so key auto-repeat (holding the chord) toggles once.
        """
        now = time.monotonic()
        if now - self._last_keyboard_time < DEBOUNCE_INTERVAL:
            return
        self._last_keyboard_time = now
        GLib.idle_add(self._dispatch_keyboard)

    def _dispatch_keyboard(self) -> bool:
        if self.on_keyboard:
            self.on_keyboard()
        return GLib.SOURCE_REMOVE

    def _dispatch_toggle(self) -> bool:
        if self.on_toggle:
            self.on_toggle()
        return GLib.SOURCE_REMOVE

    def _dispatch_start(self) -> bool:
        if self.on_start:
            self.on_start()
        return GLib.SOURCE_REMOVE

    def _dispatch_stop(self) -> bool:
        if self.on_stop:
            self.on_stop()
        return GLib.SOURCE_REMOVE

    def _dispatch_cancel(self) -> bool:
        if self.on_cancel:
            self.on_cancel()
        return GLib.SOURCE_REMOVE

    def set_cancel_enabled(self, enabled: bool) -> None:
        self._cancel_enabled = enabled

    def set_recording_mode(self, mode: str) -> None:
        """Apply a mode change immediately, cancelling an active hold."""
        if mode not in RECORDING_MODES:
            raise ValueError(f"Unknown recording mode: {mode}")
        if self._hold_active:
            self._hold_active = False
            self._last_hold_release_time = time.monotonic()
            GLib.idle_add(self._dispatch_cancel)
        self._recording_mode = mode
