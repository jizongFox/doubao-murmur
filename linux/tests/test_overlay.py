"""Tests for overlay position recovery."""

from types import SimpleNamespace

import pytest

pytest.importorskip("gi")

from doubao_murmur.ui import overlay as overlay_module
from doubao_murmur.ui.overlay import Overlay


class _Monitors:
    def __init__(self, geometries):
        self._items = [
            SimpleNamespace(get_geometry=lambda geo=geo: geo)
            for geo in geometries
        ]

    def get_n_items(self):
        return len(self._items)

    def get_item(self, index):
        return self._items[index]


def _overlay_at(x, y):
    overlay = Overlay.__new__(Overlay)
    overlay._saved_x = x
    overlay._saved_y = y
    overlay._window = None
    return overlay


def test_saved_position_visibility(monkeypatch):
    geometries = [SimpleNamespace(x=0, y=0, width=1920, height=1080)]
    display = SimpleNamespace(get_monitors=lambda: _Monitors(geometries))
    fake_gdk = SimpleNamespace(
        Display=SimpleNamespace(get_default=lambda: display)
    )
    monkeypatch.setattr(overlay_module, "Gdk", fake_gdk)

    assert _overlay_at(500, 500)._saved_position_is_visible()
    assert not _overlay_at(5000, 5000)._saved_position_is_visible()


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
