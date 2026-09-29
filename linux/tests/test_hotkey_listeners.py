"""Tests for recording-key press/release semantics."""

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
