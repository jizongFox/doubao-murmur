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
    login = LoginWindow()
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


def test_delivery_gtk_clipboard_fallback(monkeypatch):
    """Exercise real GTK clipboard ownership without tools or key injection."""
    from unittest.mock import Mock

    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, GLib, Gtk

    from doubao_murmur.paste.delivery import DeliveryOutcome, TextDelivery
    from doubao_murmur.paste.paste_helper import PasteHelper

    Gtk.init()
    monkeypatch.setattr(PasteHelper, "copy_to_clipboard", lambda *_: False)
    paste = Mock(return_value=True)
    monkeypatch.setattr(PasteHelper, "simulate_paste", paste)
    loop = GLib.MainLoop()
    outcomes, clipboard_text = [], []
    timed_out = False

    def read_finished(clipboard, result):
        try:
            clipboard_text.append(clipboard.read_text_finish(result))
        finally:
            loop.quit()

    def on_result(outcome):
        outcomes.append(outcome)
        Gdk.Display.get_default().get_clipboard().read_text_async(None, read_finished)

    def timeout():
        nonlocal timed_out
        timed_out = True
        loop.quit()
        return GLib.SOURCE_REMOVE

    service = TextDelivery(on_result=on_result)
    timer = GLib.timeout_add_seconds(5, timeout)
    try:
        service.submit("isolated GTK delivery test")
        loop.run()
    finally:
        service.close()
        service._worker.join(5)
        if not timed_out:
            GLib.source_remove(timer)

    assert not timed_out
    assert not service._worker.is_alive()
    assert outcomes == [DeliveryOutcome.PASTE_REQUESTED]
    assert clipboard_text == ["isolated GTK delivery test"]
    paste.assert_called_once()


def test_overlay_diagnostics_with_real_gtk(tmp_path, monkeypatch):
    """Build/measure real labels without presenting a window or reading keys."""
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from doubao_murmur.app_state import AppState, RecordingState
    from doubao_murmur.session_status import ConnectionPhase, SessionFault, SessionStatus
    from doubao_murmur.ui.overlay import Overlay

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    Gtk.init()
    state = AppState()
    overlay = Overlay(state)
    overlay._create_window()
    try:
        state.recording_state = RecordingState.RECORDING
        state.session_status = SessionStatus(phase=ConnectionPhase.CONNECTED)
        assert "已连接" in overlay._diagnostics.get_text()
        state.transcription_text = "检查真实 GTK 标签更新"
        state.session_status = SessionStatus(
            phase=ConnectionPhase.CLOSED, fault=SessionFault.DISCONNECTED
        )
        assert overlay._label.get_text() == "检查真实 GTK 标签更新"
        assert "连接已中断" in overlay._diagnostics.get_text()
        assert overlay._diagnostics.has_css_class("error")
        assert overlay._window.measure(Gtk.Orientation.VERTICAL, 760)[0] > 0
        assert not overlay._window.get_visible()
    finally:
        overlay._window.destroy()
