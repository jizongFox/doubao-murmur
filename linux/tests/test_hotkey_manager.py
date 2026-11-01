"""Tests for selectable toggle and hold-to-talk behavior."""

import importlib
import sys
import types
import threading
from unittest.mock import Mock

import pytest

from doubao_murmur.config import RECORDING_MODE_HOLD, RECORDING_MODE_TOGGLE


class FakeGLib:
    SOURCE_REMOVE = False
    SOURCE_CONTINUE = True

    def __init__(self):
        self.pending = []
        self.timers = {}
        self.next_source = 1000

    def idle_add(self, callback, *args):
        self.pending.append((callback, args))
        return len(self.pending)

    def drain(self):
        while self.pending:
            callback, args = self.pending.pop(0)
            callback(*args)

    def timeout_add_seconds(self, interval, callback):
        assert interval >= 1, "Recovery must not busy-poll"
        self.next_source += 1
        self.timers[self.next_source] = callback
        return self.next_source

    def source_remove(self, source):
        del self.timers[source]

    def tick(self):
        for source, callback in list(self.timers.items()):
            if not callback():
                self.timers.pop(source, None)


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


def _backend(ready=True):
    listener = Mock(ready=ready, is_running=False)

    def start():
        listener.is_running = listener.ready
        return listener.is_running

    listener.start.side_effect = start
    listener.stop.side_effect = lambda: setattr(listener, "is_running", False)
    return listener


@pytest.mark.parametrize("x11_session,xrecord_started,use_evdev", [
    (True, True, False),
    (True, False, True),
    (False, True, True),
    (False, False, True),
])
def test_backend_selection(manager_module, monkeypatch, x11_session,
                           xrecord_started, use_evdev):
    module, _ = manager_module
    monkeypatch.setattr(module, "is_x11_session", lambda: x11_session)
    x11 = _backend(ready=xrecord_started)
    evdev = _backend()
    manager = module.HotkeyManager()

    manager.start(x11_listener=x11, evdev_listener=evdev)
    manager.stop()

    assert evdev.start.called is use_evdev
    assert evdev.stop.called is use_evdev
    assert x11.stop.called is xrecord_started


def test_backends_recover_when_session_becomes_ready(manager_module, monkeypatch):
    module, glib = manager_module
    monkeypatch.setattr(module, "is_x11_session", lambda: False)
    x11, evdev = _backend(False), _backend(False)
    manager = module.HotkeyManager()
    manager.start(x11_listener=x11, evdev_listener=evdev)
    assert not manager.has_global_hotkey

    x11.ready = True
    glib.tick()
    assert manager.has_global_hotkey
    assert not evdev.is_running  # XWayland alone cannot see native Wayland keys.
    evdev.ready = True
    glib.tick()

    assert x11.is_running and evdev.is_running
    assert x11.start.call_count == 2  # Never restart the healthy backend.
    assert evdev.start.call_count == 3
    glib.tick()
    assert x11.start.call_count == 2 and evdev.start.call_count == 3
    manager.stop()
    assert not glib.timers
    assert not manager.has_global_hotkey


def test_native_x11_keeps_healthy_fallback_until_it_exits(manager_module, monkeypatch):
    module, glib = manager_module
    monkeypatch.setattr(module, "is_x11_session", lambda: True)
    x11, evdev = _backend(False), _backend()
    manager = module.HotkeyManager()
    manager.start(x11_listener=x11, evdev_listener=evdev)
    assert evdev.is_running

    x11.ready = True
    glib.tick()

    # A working fallback must not be replaced while it may be tracking a key.
    assert evdev.is_running and not x11.is_running
    x11.start.assert_called_once()
    evdev.stop.assert_not_called()
    evdev.is_running = False
    glib.tick()
    assert x11.is_running and not evdev.is_running
    manager.stop()


def test_dead_backend_restarts_and_shutdown_cancels_recovery(
    manager_module, monkeypatch
):
    module, glib = manager_module
    monkeypatch.setattr(module, "is_x11_session", lambda: False)
    evdev = _backend()
    manager = module.HotkeyManager()
    manager.start(evdev_listener=evdev)
    evdev.is_running = False  # Worker exited after a device or session failure.
    glib.tick()
    assert evdev.is_running
    assert evdev.start.call_count == 2

    manager.stop()
    glib.tick()
    assert evdev.start.call_count == 2
    assert not glib.timers


def test_start_replaces_previous_recovery_timer(manager_module, monkeypatch):
    module, glib = manager_module
    monkeypatch.setattr(module, "is_x11_session", lambda: False)
    old, new = _backend(False), _backend()
    manager = module.HotkeyManager()
    manager.start(evdev_listener=old)
    manager.start(evdev_listener=new)
    assert len(glib.timers) == 1
    glib.tick()
    old.start.assert_called_once()
    new.start.assert_called_once()
    manager.stop()


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


def test_worker_input_does_not_change_state_until_gtk_dispatch(manager_module):
    module, glib = manager_module
    manager = module.HotkeyManager(RECORDING_MODE_HOLD)
    main_thread = threading.get_ident()
    callback_threads = []
    manager.on_start = lambda: callback_threads.append(threading.get_ident())
    worker = threading.Thread(target=manager.trigger_record_press)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert not manager._hold_active
    assert callback_threads == []

    glib.drain()

    assert manager._hold_active
    assert callback_threads == [main_thread]


def test_concurrent_backend_presses_start_once(manager_module):
    module, glib = manager_module
    manager = module.HotkeyManager(RECORDING_MODE_HOLD)
    events = _wire(manager)
    barrier = threading.Barrier(3)

    def press():
        barrier.wait(timeout=5)
        manager.trigger_record_press()

    workers = [threading.Thread(target=press) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait(timeout=5)
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()
    glib.drain()
    manager.trigger_record_release(True)
    manager.trigger_record_release(True)
    glib.drain()

    assert events == ["start", "stop"]


@pytest.mark.parametrize("trigger, event", [
    ("trigger_toggle", "toggle"), ("trigger_keyboard", "keyboard"),
])
def test_debounce_uses_arrival_time_not_dispatch_time(
    manager_module, monkeypatch, trigger, event
):
    module, glib = manager_module
    now = [1.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    manager = module.HotkeyManager()
    events = _wire(manager)
    manager.on_keyboard = lambda: events.append("keyboard")
    send = getattr(manager, trigger)
    send()
    now[0] = 1.01
    send()  # auto-repeat / second backend
    now[0] = 2.0
    send()  # genuinely separate press, queued while GTK was busy
    now[0] = 10.0
    glib.drain()

    assert events == [event, event]


def test_hold_debounce_uses_release_arrival_time(manager_module, monkeypatch):
    module, glib = manager_module
    now = [1.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    manager = module.HotkeyManager(RECORDING_MODE_HOLD)
    events = _wire(manager)
    manager.trigger_record_press()
    manager.trigger_record_release(True)
    now[0] = 1.1
    manager.trigger_record_press()
    manager.trigger_record_release(True)
    now[0] = 2.0
    manager.trigger_record_press()
    manager.trigger_record_release(True)
    glib.drain()

    assert events == ["start", "stop", "start", "stop"]


@pytest.mark.parametrize("active", [False, True])
def test_mode_change_discards_pending_old_mode_input(manager_module, active):
    module, glib = manager_module
    manager = module.HotkeyManager(RECORDING_MODE_HOLD)
    events = _wire(manager)
    manager.trigger_record_press()
    if active:
        glib.drain()
    manager.trigger_record_release(True)

    manager.set_recording_mode(RECORDING_MODE_TOGGLE)
    glib.drain()

    assert events == (["start", "cancel"] if active else [])
    manager.trigger_record_release(True)
    glib.drain()
    assert events[-1] == "toggle"


def test_same_mode_keeps_pending_input_and_invalid_mode_raises(manager_module):
    module, glib = manager_module
    manager = module.HotkeyManager(RECORDING_MODE_HOLD)
    events = _wire(manager)
    manager.trigger_record_press()
    with pytest.raises(ValueError):
        manager.set_recording_mode("unknown")
    manager.set_recording_mode(RECORDING_MODE_HOLD)
    glib.drain()
    assert events == ["start"]


def test_cancel_enabled_is_checked_in_gtk_event_order(manager_module):
    module, glib = manager_module
    manager = module.HotkeyManager()
    events = _wire(manager)

    def start():
        events.append("toggle")
        manager.set_cancel_enabled(True)

    manager.on_toggle = start
    manager.trigger_record_release(True)
    manager.trigger_cancel()
    glib.drain()

    assert events == ["toggle", "cancel"]


def test_stop_discards_queued_and_late_input_even_after_restart(manager_module):
    module, glib = manager_module
    manager = module.HotkeyManager(RECORDING_MODE_HOLD)
    events = _wire(manager)
    manager.on_keyboard = lambda: events.append("keyboard")
    manager.set_cancel_enabled(True)
    for _ in range(2):
        manager.trigger_record_press()
        manager.trigger_record_release(True)
        manager.trigger_cancel()
        manager.trigger_toggle()
        manager.trigger_keyboard()
        manager.stop()
    manager.set_recording_mode(RECORDING_MODE_TOGGLE)
    manager.trigger_toggle()
    manager.set_recording_mode(RECORDING_MODE_HOLD)
    manager.start()
    glib.drain()
    assert events == []
    assert not manager.has_global_hotkey
    manager.trigger_record_press()
    glib.drain()
    assert events == ["start"]
