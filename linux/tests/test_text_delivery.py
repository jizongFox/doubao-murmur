"""Delivery ordering and shutdown without a clipboard, tools, or injected keys."""

import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("gi")

from doubao_murmur.paste import delivery as module
from doubao_murmur.paste.delivery import DeliveryOutcome, TextDelivery


class GtkQueue:
    SOURCE_REMOVE = False

    def __init__(self):
        self.pending = queue.Queue()
        self.thread = threading.get_ident()

    def idle_add(self, callback, *args):
        self.pending.put((callback, args))
        return 1

    def dispatch(self, timeout=5):
        assert threading.get_ident() == self.thread
        callback, args = self.pending.get(timeout=timeout)
        assert callback(*args) == self.SOURCE_REMOVE

    def until(self, condition):
        deadline = time.monotonic() + 5
        while not condition():
            self.dispatch(timeout=max(0, deadline - time.monotonic()))

    def drain(self):
        while not self.pending.empty():
            self.dispatch()


@pytest.fixture
def delivery(monkeypatch):
    gtk = GtkQueue()
    outcomes = []
    monkeypatch.setattr(module, "GLib", gtk)
    monkeypatch.setattr(module, "PASTE_DELAY", 0)
    copy = Mock(return_value=True)
    gtk_copy = Mock(return_value=False)
    paste = Mock(return_value=True)
    monkeypatch.setattr(module.PasteHelper, "copy_to_clipboard", copy)
    monkeypatch.setattr(module.PasteHelper, "copy_with_gtk", gtk_copy)
    monkeypatch.setattr(module.PasteHelper, "simulate_paste", paste)

    def on_result(outcome):
        assert threading.get_ident() == gtk.thread
        outcomes.append(outcome)

    service = TextDelivery(on_result=on_result)
    yield SimpleNamespace(
        service=service, gtk=gtk, outcomes=outcomes,
        copy=copy, gtk_copy=gtk_copy, paste=paste,
    )
    service.close()
    if service._worker:
        service._worker.join(5)
        assert not service._worker.is_alive(), "delivery worker did not shut down"
    gtk.drain()  # Cancelled GTK work must be harmless even if it later dispatches.


def test_cli_copy_and_paste_run_off_gtk_and_report_on_gtk(delivery):
    d = delivery
    stages = []

    def record(stage):
        assert threading.get_ident() != d.gtk.thread
        stages.append(stage)
        return True

    d.copy.side_effect = lambda *_: record("copy")
    d.paste.side_effect = lambda *_: record("paste")

    assert d.service.submit("transcript") is True
    d.gtk.until(lambda: d.outcomes)

    assert stages == ["copy", "paste"]
    assert d.outcomes == [DeliveryOutcome.PASTE_REQUESTED]
    d.gtk_copy.assert_not_called()


def test_no_paste_after_all_copy_backends_fail(delivery):
    d = delivery
    d.copy.return_value = False
    d.service.submit("must not paste the old clipboard")
    d.gtk.until(lambda: d.outcomes)

    d.gtk_copy.assert_called_once()
    d.paste.assert_not_called()
    assert d.outcomes == [DeliveryOutcome.FAILED]


def test_gtk_fallback_runs_on_main_and_still_precedes_paste(delivery):
    d = delivery
    d.copy.return_value = False
    copied = threading.Event()

    def gtk_copy(text):
        assert threading.get_ident() == d.gtk.thread
        assert text == "transcript"
        copied.set()
        return True

    def paste(*_):
        assert threading.get_ident() != d.gtk.thread
        assert copied.is_set()
        return True

    d.gtk_copy.side_effect = gtk_copy
    d.paste.side_effect = paste
    d.service.submit("transcript")
    d.gtk.until(lambda: d.outcomes)

    assert d.outcomes == [DeliveryOutcome.PASTE_REQUESTED]


def test_copy_success_and_injection_failure_reports_copied_only(delivery):
    d = delivery
    d.paste.return_value = False
    d.service.submit("available for manual paste")
    d.gtk.until(lambda: d.outcomes)

    assert d.outcomes == [DeliveryOutcome.COPIED_ONLY]


def test_requests_are_ordered_and_slow_helper_does_not_block_gtk(delivery):
    d = delivery
    entered, release = threading.Event(), threading.Event()
    stages = []

    def copy(text, *_):
        stages.append(("copy", text))
        if text == "first":
            entered.set()
            assert release.wait(5)
        return True

    def paste(*_):
        stages.append(("paste",))
        return True

    d.copy.side_effect = copy
    d.paste.side_effect = paste
    try:
        d.service.submit("first")
        assert entered.wait(5)
        d.service.submit("second")
        heartbeat = Mock(return_value=False)
        d.gtk.idle_add(heartbeat)
        d.gtk.dispatch()
        heartbeat.assert_called_once()
        assert stages == [("copy", "first")]
    finally:
        release.set()
    d.gtk.until(lambda: len(d.outcomes) == 2)

    assert stages == [("copy", "first"), ("paste",), ("copy", "second"), ("paste",)]


def test_gtk_fallback_cannot_be_overtaken_by_next_request(delivery):
    d = delivery
    d.copy.return_value = False
    d.gtk_copy.return_value = True
    d.service.submit("first")
    d.service.submit("second")
    # Wait for the first GTK copy request without executing it yet.
    pending = d.gtk.pending.get(timeout=5)
    assert d.copy.call_count == 1
    d.paste.assert_not_called()
    d.gtk.pending.put(pending)
    d.gtk.until(lambda: len(d.outcomes) == 2)

    assert [call.args[0] for call in d.gtk_copy.call_args_list] == ["first", "second"]
    assert d.outcomes == [DeliveryOutcome.PASTE_REQUESTED] * 2


def test_close_during_slow_copy_discards_later_delivery(delivery):
    d = delivery
    entered, release = threading.Event(), threading.Event()

    def slow_copy(*_):
        entered.set()
        assert release.wait(5)
        return True

    d.copy.side_effect = slow_copy
    try:
        d.service.submit("first")
        assert entered.wait(5)
        d.service.submit("discarded")
        d.service.close()  # Must return without joining the blocked helper.
        d.service.close()
        assert d.service.submit("after shutdown") is False
    finally:
        release.set()
    d.service._worker.join(5)
    d.gtk.drain()

    assert d.copy.call_count == 1
    d.paste.assert_not_called()
    assert d.outcomes == []


def test_close_releases_worker_waiting_for_gtk_without_dispatching(delivery):
    d = delivery
    d.copy.return_value = False
    d.service.submit("pending GTK copy")
    pending = d.gtk.pending.get(timeout=5)

    d.service.close()
    d.service._worker.join(5)
    assert not d.service._worker.is_alive()
    d.gtk.pending.put(pending)
    d.gtk.drain()

    d.gtk_copy.assert_not_called()
    d.paste.assert_not_called()
    assert d.outcomes == []


def test_close_interrupts_copy_paste_delay(delivery, monkeypatch):
    d = delivery
    monkeypatch.setattr(module, "PASTE_DELAY", 60)
    copied = threading.Event()
    d.copy.side_effect = lambda *_: (copied.set(), True)[1]
    d.service.submit("copied before quit")
    assert copied.wait(5)

    d.service.close()
    d.service._worker.join(5)
    assert not d.service._worker.is_alive()
    d.paste.assert_not_called()


def test_close_suppresses_already_queued_notification(delivery):
    d = delivery
    d.service.submit("completed")
    pending = d.gtk.pending.get(timeout=5)
    d.service.close()
    d.gtk.pending.put(pending)
    d.gtk.drain()
    assert d.outcomes == []


@pytest.mark.parametrize("failure", ["copy", "gtk_copy", "paste"])
def test_exception_reports_outcome_and_worker_processes_next_request(delivery, failure):
    d = delivery
    if failure == "gtk_copy":
        d.copy.return_value = False
    backend = getattr(d, failure)
    backend.side_effect = [RuntimeError("helper failed"), True]
    d.service.submit("failed")
    d.service.submit("next")
    d.gtk.until(lambda: len(d.outcomes) == 2)

    expected = DeliveryOutcome.COPIED_ONLY if failure == "paste" else DeliveryOutcome.FAILED
    assert d.outcomes == [expected, DeliveryOutcome.PASTE_REQUESTED]


def test_empty_text_and_close_before_first_submit_do_not_start_worker(delivery):
    d = delivery
    assert d.service.submit("") is False
    assert d.service._worker is None
    d.service.close()
    assert d.service.submit("after close") is False
    assert d.service._worker is None
    d.copy.assert_not_called()


@pytest.fixture
def application(delivery, monkeypatch, tmp_path):
    from doubao_murmur import app as app_module, transcription

    d = delivery
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for name in ("Overlay", "KeyboardWindow", "TrayIcon", "HotkeyManager"):
        monkeypatch.setattr(app_module, name, Mock())
    monkeypatch.setattr(app_module.X11KeyListener, "is_available", lambda: False)
    monkeypatch.setattr(app_module.EvdevListener, "is_available", lambda: False)
    monkeypatch.setattr(app_module.ParamsStore, "has_saved", lambda: True)
    monkeypatch.setattr(app_module.ParamsStore, "load", Mock())
    monkeypatch.setattr(transcription, "AudioCapture", Mock())
    monkeypatch.setattr(transcription, "ASRClient", Mock())
    # Use the existing deterministic recording timer fixture, not desktop timers.
    from tests.test_transcription import FakeGLib
    monkeypatch.setattr(transcription, "GLib", FakeGLib())
    app = app_module.DoubaoMurmurApp()
    monkeypatch.setattr(app, "send_notification", Mock())
    monkeypatch.setattr(app_module.Gio, "Notification", Mock())

    def create(on_result):
        original_result = d.service._on_result

        def result(outcome):
            original_result(outcome)
            on_result(outcome)

        d.service._on_result = result
        return d.service

    monkeypatch.setattr(app_module, "TextDelivery", create)
    app._setup_components()
    return app


def test_recording_cleans_up_while_delivery_is_still_blocked(application, delivery):
    from doubao_murmur.app_state import RecordingState

    app, d = application, delivery
    entered, release = threading.Event(), threading.Event()

    def copy(*_):
        entered.set()
        assert release.wait(5)
        return True

    d.copy.side_effect = copy
    manager = app.transcription_manager
    try:
        manager.handle_start()
        manager._on_asr_open()
        manager.app_state.transcription_text = "finished transcript"
        manager.handle_stop()
        manager._on_asr_finish()
        assert entered.wait(5)

        assert manager.app_state.recording_state == RecordingState.IDLE
        manager.asr_client.disconnect.assert_called_once()
        app.overlay.hide.assert_called_once()
        assert not d.outcomes
    finally:
        release.set()
    d.gtk.until(lambda: d.outcomes)
    app.send_notification.assert_not_called()


@pytest.mark.parametrize("outcome", list(DeliveryOutcome))
def test_delivery_notification_does_not_overwrite_new_recording(application, outcome):
    from doubao_murmur.app_state import RecordingState

    app = application
    app.app_state.recording_state = RecordingState.RECORDING
    app.app_state.transcription_text = "a newer recording"
    app.app_state.error_message = "current recording error"

    app._on_delivery_complete(outcome)

    assert app.app_state.recording_state == RecordingState.RECORDING
    assert app.app_state.transcription_text == "a newer recording"
    assert app.app_state.error_message == "current recording error"
    assert app.send_notification.call_count == (outcome != DeliveryOutcome.PASTE_REQUESTED)


def test_application_quit_closes_delivery_before_recording_cleanup(application, monkeypatch):
    app = application
    stages = []
    monkeypatch.setattr(app.text_delivery, "close", lambda: stages.append("delivery closed"))
    app.transcription_manager.handle_cancel = lambda: stages.append("recording cancelled")
    app.hotkey_manager.stop = lambda: stages.append("hotkeys stopped")
    monkeypatch.setattr(app, "release", lambda: None)
    monkeypatch.setattr(app, "quit", lambda: stages.append("quit"))

    app._quit()

    assert stages == ["delivery closed", "recording cancelled", "hotkeys stopped", "quit"]
