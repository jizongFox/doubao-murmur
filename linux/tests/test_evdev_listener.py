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
        assert len(polls) == 1, "Failed device was polled again"
        return read_fds, [], []

    def read_failed(fd, size):
        if error_code is not None:
            raise OSError(error_code, os.strerror(error_code))
        return b""

    monkeypatch.setattr(evdev.select, "select", select_once)
    monkeypatch.setattr(evdev.os, "read", read_failed)

    listener._listen_loop(["/dev/input/event-test"])

    assert closed == [101]
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
