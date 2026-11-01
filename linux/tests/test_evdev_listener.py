"""Regression tests for disconnected input devices and transient reads."""

import errno
import os
import struct

import pytest

from doubao_murmur.hotkey import evdev_listener as evdev


def _keyboard_events():
    return b"".join(
        struct.pack(evdev.EVENT_FORMAT, 0, 0, evdev.EV_KEY, evdev.KEY_RIGHTALT, value)
        for value in (1, 0)
    )


def _listener():
    events = []

    def released(valid):
        events.append(("release", valid))
        listener._running = False

    listener = evdev.EvdevListener(
        lambda: events.append("press"), released, lambda: events.append("escape")
    )
    listener._running = True
    return listener, events


@pytest.mark.parametrize("error_code", [errno.ENODEV, errno.EIO, None])
def test_failed_device_is_removed_without_repolling(monkeypatch, error_code):
    listener, events = _listener()
    closed = []
    polls = []
    monkeypatch.setattr(evdev.os, "open", lambda path, flags: 101)
    monkeypatch.setattr(evdev.os, "close", closed.append)

    def select_once(read_fds, write_fds, error_fds, timeout):
        polls.append(list(read_fds))
        if len(polls) == 1:
            return read_fds, [], []
        assert not read_fds, "Failed device was polled again"
        assert timeout >= 0.5, "An empty device set must wait before retrying"
        listener._running = False
        return [], [], []

    def read_failed(fd, size):
        if error_code is not None:
            raise OSError(error_code, os.strerror(error_code))
        return b""

    monkeypatch.setattr(evdev.select, "select", select_once)
    monkeypatch.setattr(evdev.os, "read", read_failed)

    listener._listen_loop(["/dev/input/event-test"])

    assert closed == [101]
    assert polls == [[101], []]
    assert not listener._running
    assert events == []


def test_healthy_keyboard_continues_after_another_device_fails(monkeypatch):
    listener, events = _listener()
    closed = []
    polls = []
    device_fds = {"failed": 101, "keyboard": 102}
    monkeypatch.setattr(evdev.os, "open", lambda path, flags: device_fds[path])
    monkeypatch.setattr(evdev.os, "close", closed.append)

    def select_devices(read_fds, write_fds, error_fds, timeout):
        polls.append(list(read_fds))
        if len(polls) == 1:
            return [101], [], []
        assert read_fds == [102], "Failed device remained in the selector"
        return [102], [], []

    def read_device(fd, size):
        if fd == 101:
            raise OSError(errno.ENODEV, "No such device")
        return _keyboard_events()

    monkeypatch.setattr(evdev.select, "select", select_devices)
    monkeypatch.setattr(evdev.os, "read", read_device)

    listener._listen_loop(["failed", "keyboard"])

    assert events == ["press", ("release", True)]
    assert closed == [101, 102]
    assert polls == [[101, 102], [102]]


@pytest.mark.parametrize("error_code", [errno.EAGAIN, errno.EINTR])
def test_transient_read_error_keeps_keyboard(monkeypatch, error_code, caplog):
    listener, events = _listener()
    closed = []
    flags_used = []
    reads = []

    def open_device(path, flags):
        flags_used.append(flags)
        return 101

    def read_device(fd, size):
        reads.append(fd)
        if len(reads) == 1:
            raise OSError(error_code, os.strerror(error_code))
        return _keyboard_events()

    monkeypatch.setattr(evdev.os, "open", open_device)
    monkeypatch.setattr(evdev.os, "close", closed.append)
    monkeypatch.setattr(evdev.os, "read", read_device)
    monkeypatch.setattr(
        evdev.select, "select", lambda read_fds, *_args: (read_fds, [], [])
    )

    listener._listen_loop(["keyboard"])

    assert events == ["press", ("release", True)]
    assert closed == [101]
    assert reads == [101, 101]
    assert flags_used[0] & os.O_NONBLOCK
    assert not caplog.records


def test_keyboard_appearing_after_startup_is_added(monkeypatch):
    listener, events = _listener()
    opened, closed = [], []
    device_fds = {"mouse": 101, "keyboard": 102}

    def open_device(path, flags):
        opened.append(path)
        assert flags & os.O_NONBLOCK
        return device_fds[path]

    # A peripheral was readable at login; the actual keyboard appears later.
    clock = iter([0.0, 2.0])
    monkeypatch.setattr(evdev.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(evdev.glob, "glob", lambda *_: ["mouse", "keyboard"])
    monkeypatch.setattr(evdev.os, "open", open_device)
    monkeypatch.setattr(evdev.os, "close", closed.append)
    monkeypatch.setattr(evdev.os, "read", lambda *_: _keyboard_events())

    def select_keyboard(read_fds, *_):
        assert read_fds == [101, 102]
        return [102], [], []

    monkeypatch.setattr(evdev.select, "select", select_keyboard)
    listener._listen_loop(["mouse"])

    assert events == ["press", ("release", True)]
    assert opened == ["mouse", "keyboard"]  # Existing devices stay open.
    assert closed == [101, 102]


def test_device_permissions_can_recover_without_restarting_app(monkeypatch):
    listener, events = _listener()
    closed, polls = [], []
    ready = False
    now = [0.0]

    def open_device(path, flags):
        if not ready:
            raise PermissionError("Login device ACL not ready")
        return 101

    def select_device(read_fds, _, __, timeout):
        nonlocal ready
        polls.append(list(read_fds))
        if not ready:
            assert not read_fds and timeout >= 0.5
            ready = True
            now[0] = 2.0
            return [], [], []
        assert read_fds == [101]
        return [101], [], []

    monkeypatch.setattr(evdev.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(evdev.glob, "glob", lambda *_: ["keyboard"])
    monkeypatch.setattr(evdev.os, "open", open_device)
    monkeypatch.setattr(evdev.os, "close", closed.append)
    monkeypatch.setattr(evdev.os, "read", lambda *_: _keyboard_events())
    monkeypatch.setattr(evdev.select, "select", select_device)

    listener._listen_loop(["keyboard"])

    assert events == ["press", ("release", True)]
    assert polls == [[], [101]]
    assert closed == [101]
