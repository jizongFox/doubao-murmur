"""Tests for recording-key press/release semantics."""

from unittest.mock import Mock

from doubao_murmur.hotkey.evdev_listener import (
    KEY_RIGHTALT,
    EvdevListener,
)
from doubao_murmur.hotkey.x11_listener import X11KeyListener


def test_evdev_reports_one_press_and_release_with_validity():
    events = []
    listener = EvdevListener(
        lambda: events.append("press"),
        lambda valid: events.append(("release", valid)),
        lambda: events.append("escape"),
    )

    listener._handle_key(KEY_RIGHTALT, 1)
    listener._handle_key(KEY_RIGHTALT, 2)  # key repeat
    listener._handle_key(KEY_RIGHTALT, 0)
    listener._handle_key(KEY_RIGHTALT, 1)
    listener._handle_key(30, 1)  # another key makes this an AltGr chord
    listener._handle_key(KEY_RIGHTALT, 0)

    assert events == [
        "press",
        ("release", True),
        "press",
        ("release", False),
    ]


def test_x11_reports_one_press_and_release_with_validity():
    events = []
    listener = X11KeyListener(
        lambda: events.append("press"),
        lambda valid: events.append(("release", valid)),
        lambda: events.append("escape"),
    )
    listener._kc_toggle = frozenset({108})

    listener._handle_key(108, pressed=True)
    listener._handle_key(108, pressed=True)  # key repeat
    listener._handle_key(108, pressed=False)
    listener._handle_key(108, pressed=True)
    listener._handle_key(38, pressed=True)
    listener._handle_key(108, pressed=False)

    assert events == [
        "press",
        ("release", True),
        "press",
        ("release", False),
    ]


def test_x11_empty_login_keymap_is_retryable(monkeypatch):
    listener = X11KeyListener(Mock(), Mock(), Mock())
    displays = [Mock(), Mock()]
    monkeypatch.setenv("DISPLAY", ":test-only")
    monkeypatch.setattr("Xlib.display.Display", Mock(side_effect=displays))
    monkeypatch.setattr(listener, "_resolve_toggle_keycodes", lambda _: frozenset())

    assert not listener.start()
    assert not listener.is_running
    for display in displays:
        display.close.assert_called_once()


def test_x11_worker_failure_releases_connections_for_recovery():
    listener = X11KeyListener(Mock(), Mock(), Mock())
    record_display, control_display = Mock(), Mock()
    record_display.record_enable_context.side_effect = OSError("X server closed")
    listener._record_dpy = record_display
    listener._ctrl_dpy = control_display
    listener._context = 1
    listener._running = True

    listener._listen_loop()

    assert not listener._running
    assert listener._record_dpy is None and listener._ctrl_dpy is None
    record_display.close.assert_called_once()
    control_display.close.assert_called_once()
