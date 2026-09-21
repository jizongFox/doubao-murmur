"""Tests for overlay position persistence and reset."""

import pytest

pytest.importorskip("gi")

from doubao_murmur.ui import overlay as overlay_module
from doubao_murmur.ui.overlay import Overlay


def _overlay_at(x, y):
    overlay = Overlay.__new__(Overlay)
    overlay._saved_x = x
    overlay._saved_y = y
    overlay._window = None
    return overlay


def test_show_keeps_saved_x11_position(monkeypatch):
    overlay = _overlay_at(4711, 2950)
    overlay._window = object()
    overlay._anim_timer = 1
    overlay._update_content = lambda: None
    presented = []

    monkeypatch.setattr(overlay_module, "using_layer_shell", lambda: False)
    monkeypatch.setattr(
        overlay_module,
        "present_overlay",
        lambda _window, _role, x=None, y=None: presented.append((x, y)),
    )

    overlay.show()

    assert presented == [(4711, 2950)]


def test_reset_position_removes_saved_file(tmp_path, monkeypatch):
    position_file = tmp_path / "overlay.json"
    position_file.write_text('{"x": 5000, "y": 5000}')
    monkeypatch.setattr(
        overlay_module, "get_overlay_config_path", lambda: position_file
    )
    overlay = _overlay_at(5000, 5000)

    overlay.reset_position()

    assert overlay._saved_x is None
    assert overlay._saved_y is None
    assert not position_file.exists()
