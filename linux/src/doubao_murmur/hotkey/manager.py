"""Hotkey manager: coordinates input methods for triggering recording.

Two backends, retried as the desktop session becomes ready:
1. X11/XRecord (x11_listener) — preferred, and the only one that sees
   Steam Input's XTEST-injected controller keys.
2. evdev /dev/input (evdev_listener) — needs the `input` group, plus
   --device=input when sandboxed. The only one that works on a Wayland
   session, where keys sent to native Wayland windows never reach
   XWayland and are therefore invisible to XRecord.
"""

from __future__ import annotations

import logging
import time

from gi.repository import GLib

from doubao_murmur.desktop import is_x11_session

from doubao_murmur.config import (
    DEBOUNCE_INTERVAL,
    RECORDING_MODES,
    RECORDING_MODE_HOLD,
    RECORDING_MODE_TOGGLE,
)

logger = logging.getLogger(__name__)


class HotkeyManager:
    """Own hotkey decisions on GTK; backends only enqueue timestamped input.

    start/stop and settings setters must run on the GTK thread. The trigger_*
    entrypoints may run on either backend thread and never mutate hotkey state.
    """

    def __init__(self, recording_mode: str = RECORDING_MODE_TOGGLE) -> None:
        if recording_mode not in RECORDING_MODES:
            raise ValueError(f"Unknown recording mode: {recording_mode}")
        self.on_toggle = None  # () -> None
        self.on_start = None  # () -> None
        self.on_stop = None  # () -> None
        self.on_cancel = None  # () -> None
        self.on_keyboard = None  # () -> None (toggle on-screen keyboard)
        self._evdev_listener = None
        self._x11_listener = None
        self._cancel_enabled = False
        self._last_toggle_time = 0.0
        self._last_keyboard_time = 0.0
        self._recording_mode = recording_mode
        self._hold_active = False
        self._last_hold_release_time = 0.0
        self._input_epoch: object | None = object()
        self._retry_source: int | None = None

    def start(self, evdev_listener=None, x11_listener=None) -> None:
        """Own backend startup and recovery, including login-time failures."""
        self.stop()
        self._input_epoch = object()
        self._evdev_listener = evdev_listener
        self._x11_listener = x11_listener
        self._ensure_listeners()
        if evdev_listener or x11_listener:
            self._retry_source = GLib.timeout_add_seconds(5, self._ensure_listeners)
            if not self.has_global_hotkey:
                logger.warning("Hotkeys not ready; retrying every 5 seconds")

    def _ensure_listeners(self) -> bool:
        native_x11 = is_x11_session()
        for listener in (self._x11_listener, self._evdev_listener):
            # Keep a healthy X11 backend, including an evdev fallback: switching
            # listeners mid-press can lose the matching release. Wayland needs
            # both XRecord and evdev to cover all focused applications.
            if native_x11 and self.has_global_hotkey:
                break
            if listener is not None and not listener.is_running:
                if listener.start():
                    logger.info("%s active", type(listener).__name__)
        return GLib.SOURCE_CONTINUE

    def stop(self) -> None:
        """Stop all input backends."""
        self._input_epoch = None
        self._hold_active = False
        if self._retry_source is not None:
            GLib.source_remove(self._retry_source)
            self._retry_source = None
        for listener in (self._x11_listener, self._evdev_listener):
            if listener is not None and listener.is_running:
                listener.stop()
        self._x11_listener = None
        self._evdev_listener = None

    @property
    def has_global_hotkey(self) -> bool:
        """True when a global hotkey backend is active."""
        return any(
            listener is not None and listener.is_running
            for listener in (self._evdev_listener, self._x11_listener)
        )

    def trigger_toggle(self) -> None:
        self._queue_input(self._handle_toggle)

    def trigger_record_press(self) -> None:
        self._queue_input(self._handle_record_press)

    def trigger_record_release(self, valid: bool) -> None:
        """Queue release; ``valid`` is false when AltGr formed a chord."""
        self._queue_input(self._handle_record_release, valid)

    def trigger_cancel(self) -> None:
        self._queue_input(self._handle_cancel)

    def trigger_keyboard(self) -> None:
        self._queue_input(self._handle_keyboard)

    def _queue_input(self, handler, *args) -> None:
        # Snapshot event time, not dispatch time: a busy GTK loop must not make
        # two separate key presses look like simultaneous duplicates.
        epoch = self._input_epoch
        if epoch is not None:
            GLib.idle_add(self._dispatch_input, epoch, handler, time.monotonic(), *args)

    def _dispatch_input(self, epoch, handler, timestamp, *args) -> bool:
        # Shutdown and mode changes invalidate work queued under the old rules.
        if epoch is self._input_epoch:
            handler(timestamp, *args)
        return GLib.SOURCE_REMOVE

    def _handle_toggle(self, now: float) -> None:
        if now - self._last_toggle_time < DEBOUNCE_INTERVAL:
            return
        self._last_toggle_time = now
        if self.on_toggle:
            self.on_toggle()

    def _handle_record_press(self, now: float) -> None:
        if self._recording_mode != RECORDING_MODE_HOLD:
            return
        if self._hold_active:
            return
        # X11 and evdev can report the same physical key. A second backend may
        # deliver its press just after the first backend delivered the release.
        if now - self._last_hold_release_time < DEBOUNCE_INTERVAL:
            return
        self._hold_active = True
        if self.on_start:
            self.on_start()

    def _handle_record_release(self, now: float, valid: bool) -> None:
        if self._recording_mode == RECORDING_MODE_TOGGLE:
            if valid:
                self._handle_toggle(now)
            return
        if not self._hold_active:
            return
        self._hold_active = False
        self._last_hold_release_time = now
        if valid:
            if self.on_stop:
                self.on_stop()
        else:
            if self.on_cancel:
                self.on_cancel()

    def _handle_cancel(self, now: float) -> None:
        if self._hold_active:
            self._hold_active = False
            self._last_hold_release_time = now
        elif not self._cancel_enabled:
            return
        if self.on_cancel:
            self.on_cancel()

    def _handle_keyboard(self, now: float) -> None:
        if now - self._last_keyboard_time < DEBOUNCE_INTERVAL:
            return
        self._last_keyboard_time = now
        if self.on_keyboard:
            self.on_keyboard()

    def set_cancel_enabled(self, enabled: bool) -> None:
        self._cancel_enabled = enabled

    def set_recording_mode(self, mode: str) -> None:
        """Apply a mode change immediately, cancelling an active hold."""
        if mode not in RECORDING_MODES:
            raise ValueError(f"Unknown recording mode: {mode}")
        if mode == self._recording_mode:
            return
        if self._input_epoch is not None:
            self._input_epoch = object()
        self._recording_mode = mode
        if self._hold_active:
            self._hold_active = False
            self._last_hold_release_time = time.monotonic()
            if self.on_cancel:
                self.on_cancel()
