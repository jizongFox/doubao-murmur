"""Session policy must not confuse an XWayland window with an X11 desktop."""

import pytest

from doubao_murmur.desktop import is_x11_session


@pytest.mark.parametrize("session,display,wayland,expected", [
    ("x11", ":0", "", True),
    ("wayland", ":0", "wayland-0", False),
    ("wayland", ":0", "", False),
    ("", ":0", "", True),
    ("", ":0", "wayland-0", False),
    ("", "", "", False),
])
def test_session_selection(monkeypatch, session, display, wayland, expected):
    monkeypatch.setenv("XDG_SESSION_TYPE", session)
    monkeypatch.setenv("DISPLAY", display)
    monkeypatch.setenv("WAYLAND_DISPLAY", wayland)
    assert is_x11_session() is expected
