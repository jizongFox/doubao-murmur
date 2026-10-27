"""Recording lifecycle tests without microphone, network, or real timers."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("gi")

from doubao_murmur import transcription as module
from doubao_murmur.app_state import AppState, LoginStatus, RecordingState
from doubao_murmur.config import (
    AUTH_EXPIRY_DELAY,
    FINAL_RESULT_QUIET_PERIOD,
    STOP_SAFETY_TIMEOUT,
)


class FakeGLib:
    SOURCE_REMOVE = False

    def __init__(self):
        self.now = 0
        self.sources = {}
        self.next_id = 0

    def timeout_add(self, milliseconds, callback, *args):
        self.next_id += 1
        self.sources[self.next_id] = (self.now + milliseconds, callback, args)
        return self.next_id

    def idle_add(self, callback, *args):
        return self.timeout_add(0, callback, *args)

    def source_remove(self, source_id):
        assert source_id in self.sources, "removed an absent or running source"
        del self.sources[source_id]

    def advance(self, milliseconds):
        deadline = self.now + milliseconds
        while self.sources:
            source_id = min(self.sources, key=lambda key: self.sources[key][0])
            when, callback, args = self.sources[source_id]
            if when > deadline:
                break
            self.now = when
            del self.sources[source_id]
            assert callback(*args) == self.SOURCE_REMOVE
        self.now = deadline


@pytest.fixture
def recording(monkeypatch):
    glib = FakeGLib()
    monkeypatch.setattr(module, "GLib", glib)
    monkeypatch.setattr(module, "ASRClient", Mock(side_effect=lambda: Mock()))
    monkeypatch.setattr(module, "AudioCapture", Mock())
    monkeypatch.setattr(module, "ParamsStore", Mock())
    state = AppState()
    state.login_status = LoginStatus.LOGGED_IN
    manager = module.TranscriptionManager(state)
    manager.on_save_params = module.ParamsStore.save
    manager.on_paste = Mock()
    manager.on_overlay_hide = Mock()
    manager.on_auth_expired = Mock()
    return manager, glib


def test_start_requires_login(recording):
    manager, _ = recording
    manager.app_state.login_status = LoginStatus.NOT_LOGGED_IN
    manager.on_show_login = Mock()

    manager.handle_start()

    manager.on_show_login.assert_called_once_with()
    manager.audio_capture.start.assert_not_called()
    module.ASRClient.assert_not_called()


@pytest.mark.parametrize("auth_failure", [False, True])
def test_exit_cleans_up_once_without_pasting(recording, auth_failure):
    manager, _ = recording
    manager.handle_start()
    manager.app_state.transcription_text = "unfinished"

    if auth_failure:
        manager._on_auth_error()
        module.ParamsStore.clear.assert_called_once_with()
        manager.on_auth_expired.assert_called_once_with()
        assert manager.app_state.login_status == LoginStatus.NOT_LOGGED_IN
    else:
        manager.handle_cancel()
        module.ParamsStore.clear.assert_not_called()

    manager.audio_capture.stop.assert_called_once_with()
    manager.asr_client.disconnect.assert_called_once_with()
    manager.on_paste.assert_not_called()
    assert manager.app_state.recording_state == RecordingState.IDLE


def test_rejected_cached_login_cleans_up_and_prompts_login(recording):
    manager, _ = recording
    manager.handle_start()
    manager.app_state.transcription_text = "unfinished"
    error = SimpleNamespace(response=SimpleNamespace(
        status_code=200,
        body=b'{"code":710012001,"message":"login invalid"}',
    ))

    manager._on_asr_error(error)

    module.ParamsStore.clear.assert_called_once_with()
    manager.on_auth_expired.assert_called_once_with()
    manager.audio_capture.stop.assert_called_once_with()
    manager.asr_client.disconnect.assert_called_once_with()
    manager.on_paste.assert_not_called()
    assert manager.app_state.login_status == LoginStatus.NOT_LOGGED_IN
    assert manager.app_state.recording_state == RecordingState.IDLE


def test_cancelled_recording_ignores_late_auth_error(recording):
    manager, _ = recording
    manager.handle_start()
    manager.handle_cancel()

    manager._on_auth_error()

    module.ParamsStore.clear.assert_not_called()
    manager.on_auth_expired.assert_not_called()
    assert manager.app_state.login_status == LoginStatus.LOGGED_IN
    assert manager.app_state.recording_state == RecordingState.IDLE


@pytest.mark.parametrize("next_state", ["idle", "starting", "recording", "stopping"])
@pytest.mark.parametrize("event, args", [
    ("on_open", ()),
    ("on_result", ("old recording",)),
    ("on_finish", ()),
    ("on_error", (RuntimeError("old network error"),)),
    ("on_auth_error", ()),
])
def test_cancelled_session_ignores_callbacks(recording, next_state, event, args):
    manager, glib = recording
    manager.handle_start()
    old_callback = getattr(manager.asr_client, event)
    # Exercise both an event already queued in GTK and one emitted after reset.
    old_callback(*args)
    manager.handle_cancel()
    if next_state != "idle":
        manager.handle_start()
        if next_state != "starting":
            manager._on_asr_open()
        manager.app_state.transcription_text = "new recording"
        if next_state == "stopping":
            manager.handle_stop()
    expected_state = manager.app_state.recording_state
    expected_text = manager.app_state.transcription_text
    old_callback(*args)

    glib.advance(0)

    assert manager.app_state.recording_state == expected_state
    assert manager.app_state.transcription_text == expected_text
    assert manager.app_state.error_message is None
    assert manager.app_state.login_status == LoginStatus.LOGGED_IN
    module.ParamsStore.clear.assert_not_called()
    manager.on_auth_expired.assert_not_called()
    manager.on_paste.assert_not_called()


def test_recording_uses_a_fresh_client_and_dispatches_current_events(recording):
    manager, glib = recording
    manager.handle_start()
    old_client = manager.asr_client
    manager.handle_cancel()
    manager.handle_start()

    assert manager.asr_client is not old_client
    old_client.disconnect.assert_called_once_with()
    manager.asr_client.on_open()
    manager.asr_client.on_result("current recording")
    glib.advance(0)

    assert manager.app_state.recording_state == RecordingState.RECORDING
    assert manager.app_state.transcription_text == "current recording"
    manager.handle_stop()
    manager.asr_client.on_finish()
    glib.advance(0)
    manager.on_paste.assert_called_once_with("current recording")


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("params", [None, Mock()])
def test_cancelled_session_ignores_parameter_extraction(recording, restart, params):
    manager, glib = recording
    module.ParamsStore.load.return_value = None
    callbacks = []
    manager.on_params_needed = callbacks.append
    manager.handle_start()
    old_client = manager.asr_client
    manager.handle_cancel()
    if restart:
        manager.handle_start()
    expected_state = manager.app_state.recording_state

    callbacks[0](params)
    glib.advance(int(AUTH_EXPIRY_DELAY * 1000))

    assert manager.app_state.recording_state == expected_state
    assert manager.app_state.error_message is None
    module.ParamsStore.save.assert_not_called()
    old_client.connect.assert_not_called()
    if restart:
        manager.asr_client.connect.assert_not_called()


def test_stop_before_parameter_extraction_preserves_current_session(recording):
    manager, _ = recording
    module.ParamsStore.load.return_value = None
    callbacks = []
    manager.on_params_needed = callbacks.append
    params = Mock()
    manager.handle_start()
    manager.handle_stop()

    callbacks[0](params)

    module.ParamsStore.save.assert_called_once_with(params)
    manager.asr_client.connect.assert_called_once_with(params)
    manager.asr_client.finish_sending.assert_called_once_with()
    assert manager.app_state.recording_state == RecordingState.STOPPING


def test_stop_waits_for_latest_result(recording):
    manager, glib = recording
    manager.handle_start()
    manager._on_asr_open()
    manager.handle_stop()
    manager.asr_client.finish_sending.assert_called_once_with()
    manager._on_asr_result("first")
    glib.advance(int(FINAL_RESULT_QUIET_PERIOD * 1000) - 1)
    manager._on_asr_result("final words")
    glib.advance(int(FINAL_RESULT_QUIET_PERIOD * 1000))

    manager.on_paste.assert_called_once_with("final words")
    assert manager.app_state.recording_state == RecordingState.IDLE
    glib.advance(int(STOP_SAFETY_TIMEOUT * 1000))
    manager.on_paste.assert_called_once()


def test_safety_timeout_completes_without_finish_event(recording):
    manager, glib = recording
    manager.handle_start()
    manager._on_asr_result("current words")
    manager.handle_stop()
    glib.advance(int(STOP_SAFETY_TIMEOUT * 1000))

    manager.on_paste.assert_called_once_with("current words")
    assert manager.app_state.recording_state == RecordingState.IDLE


def test_cancel_discards_pending_completion(recording):
    manager, glib = recording
    manager.handle_start()
    manager.handle_stop()
    manager._on_asr_result("cancelled")
    manager.handle_cancel()
    glib.advance(int(STOP_SAFETY_TIMEOUT * 1000))

    manager.on_paste.assert_not_called()


def test_delayed_clear_does_not_erase_next_recording(recording):
    manager, glib = recording
    manager.handle_start()
    manager.handle_cancel()
    manager.handle_start()
    manager._on_asr_result("new recording")
    glib.advance(200)

    assert manager.app_state.transcription_text == "new recording"


def test_old_error_reset_does_not_stop_next_recording(recording):
    manager, glib = recording
    manager.handle_start()
    manager._on_asr_error(RuntimeError("network unavailable"))
    module.ParamsStore.clear.assert_not_called()
    manager.handle_cancel()
    manager.handle_start()
    manager._on_asr_open()
    glib.advance(int(AUTH_EXPIRY_DELAY * 1000))

    assert manager.app_state.recording_state == RecordingState.RECORDING


def test_error_reset_still_returns_to_idle(recording):
    manager, glib = recording
    manager.handle_start()
    manager._on_asr_error(RuntimeError("network unavailable"))
    glib.advance(int(AUTH_EXPIRY_DELAY * 1000))

    assert manager.app_state.recording_state == RecordingState.IDLE
    manager.on_paste.assert_not_called()
    module.ParamsStore.clear.assert_not_called()


def test_idle_text_is_still_cleared_after_delay(recording):
    manager, glib = recording
    manager.handle_start()
    manager._on_asr_result("cancelled text")
    manager.handle_cancel()
    glib.advance(199)
    assert manager.app_state.transcription_text == "cancelled text"

    glib.advance(1)

    assert manager.app_state.transcription_text == ""


def test_repeated_errors_have_one_reset(recording):
    manager, glib = recording
    manager.handle_start()
    manager._on_asr_error(RuntimeError("first error"))
    glib.advance(100)
    manager._on_asr_error(RuntimeError("second error"))
    glib.advance(int(AUTH_EXPIRY_DELAY * 1000) + 200)

    manager.asr_client.disconnect.assert_called_once_with()
    assert manager.app_state.recording_state == RecordingState.IDLE


@pytest.mark.parametrize("failure", ["microphone", "missing_params", "extraction"])
def test_startup_failure_reset_is_cancelled_for_next_recording(recording, failure):
    manager, glib = recording
    if failure == "microphone":
        manager.audio_capture.start.side_effect = RuntimeError("no microphone")
    else:
        module.ParamsStore.load.return_value = None
        if failure == "extraction":
            manager.on_params_needed = lambda callback: callback(None)
    manager.handle_start()
    assert manager.app_state.error_message is not None
    manager.handle_cancel()

    manager.audio_capture.start.side_effect = None
    module.ParamsStore.load.return_value = Mock()
    manager.handle_start()
    manager._on_asr_open()
    glib.advance(int(AUTH_EXPIRY_DELAY * 1000))

    assert manager.app_state.recording_state == RecordingState.RECORDING
