"""Opt-in real GTK/WebKit/XRecord check; run only in an isolated Xvfb server."""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("DOUBAO_X11_SMOKE") != "1",
    reason="requires explicit opt-in under xvfb-run",
)


def test_x11_components_start(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from doubao_murmur.app_state import AppState
    from doubao_murmur.hotkey.x11_listener import X11KeyListener
    from doubao_murmur.ui.login_window import LoginWindow
    from doubao_murmur.ui.overlay import Overlay

    Gtk.init()
    state = AppState()
    assert LoginWindow.is_available(), "GTK4 WebKitGTK 6 is required"
    login = LoginWindow(state)
    listener = X11KeyListener(lambda: None, lambda valid: None, lambda: None)
    try:
        login._setup()  # Create the real WebView, but never load doubao.com.
        assert login.is_active
        overlay = Overlay(state)
        overlay.show()
        overlay.hide()
        assert listener.is_available(), "X server must expose RECORD"
        assert listener.start()
    finally:
        listener.stop()
        login.destroy()
