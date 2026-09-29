"""Tests for selectable toggle and hold-to-talk behavior."""

import importlib
import sys
import types

import pytest

from doubao_murmur.config import RECORDING_MODE_HOLD


class FakeGLib:
    SOURCE_REMOVE = False

    def __init__(self):
        self.pending = []

    def idle_add(self, callback, *args):
        self.pending.append((callback, args))
        return len(self.pending)

    def drain(self):
        while self.pending:
            callback, args = self.pending.pop(0)
            callback(*args)


@pytest.fixture
def manager_module(monkeypatch):
    fake_glib = FakeGLib()
    repository = types.ModuleType("gi.repository")
    repository.GLib = fake_glib
    gi = types.ModuleType("gi")
    gi.repository = repository
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    sys.modules.pop("doubao_murmur.hotkey.manager", None)
    module = importlib.import_module("doubao_murmur.hotkey.manager")
    yield module, fake_glib
    sys.modules.pop("doubao_murmur.hotkey.manager", None)


def _wire(manager):
    events = []
    manager.on_toggle = lambda: events.append("toggle")
    manager.on_start = lambda: events.append("start")
    manager.on_stop = lambda: events.append("stop")
    manager.on_cancel = lambda: events.append("cancel")
    return events


def test_toggle_mode_acts_on_valid_release(manager_module):
    module, glib = manager_module
    manager = module.HotkeyManager()
    events = _wire(manager)

    manager.trigger_record_press()
    manager.trigger_record_release(True)
    glib.drain()
    manager.trigger_record_release(False)
    glib.drain()

    assert events == ["toggle"]


def test_hold_mode_deduplicates_both_input_backends(manager_module, monkeypatch):
    module, glib = manager_module
    now = [1.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    manager = module.HotkeyManager(RECORDING_MODE_HOLD)
    events = _wire(manager)

    manager.trigger_record_press()
    manager.trigger_record_press()
    glib.drain()
    manager.trigger_record_release(True)
    manager.trigger_record_release(True)
    glib.drain()

    # Ignore a delayed duplicate press from the second input backend.
    now[0] = 1.1
    manager.trigger_record_press()
    manager.trigger_record_release(True)
    glib.drain()

    now[0] = 1.4
    manager.trigger_record_press()
    manager.trigger_record_release(True)
    glib.drain()

    assert events == ["start", "stop", "start", "stop"]


def test_hold_mode_cancels_an_altgr_chord(manager_module, monkeypatch):
    module, glib = manager_module
    monkeypatch.setattr(module.time, "monotonic", lambda: 1.0)
    manager = module.HotkeyManager(RECORDING_MODE_HOLD)
    events = _wire(manager)

    manager.trigger_record_press()
    glib.drain()
    manager.trigger_record_release(False)
    glib.drain()

    assert events == ["start", "cancel"]


def test_escape_cancels_even_before_start_is_dispatched(
    manager_module, monkeypatch
):
    module, glib = manager_module
    monkeypatch.setattr(module.time, "monotonic", lambda: 1.0)
    manager = module.HotkeyManager(RECORDING_MODE_HOLD)
    events = _wire(manager)

    manager.trigger_record_press()
    manager.trigger_cancel()
    manager.trigger_record_release(True)
    glib.drain()

    assert events == ["start", "cancel"]
