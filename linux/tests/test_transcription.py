"""Recording lifecycle tests without microphone, network, or real timers."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("gi")

from doubao_murmur import doubao_recognition as cloud, transcription as module
from doubao_murmur.app_state import AppState, LoginStatus, RecordingState
from doubao_murmur.config import SESSION_NOTICE_DURATION
from doubao_murmur.doubao_recognition import (
    DoubaoRecognitionSession,
    FINAL_RESULT_QUIET_PERIOD,
    STOP_SAFETY_TIMEOUT,
    SESSION_START_TIMEOUT,
)
from doubao_murmur.session_status import ConnectionPhase, SessionFault, SessionStatus
from doubao_murmur.asr_client import ASRServiceError


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
def extract_params():
    return Mock(side_effect=lambda callback: callback(None))


@pytest.fixture
def auth_expired():
    return Mock()


@pytest.fixture
def recording(monkeypatch, extract_params, auth_expired):
    glib = FakeGLib()
    clients = []

    def create_client():
        clients.append(Mock())
        return clients[-1]

    monkeypatch.setattr(module, "GLib", glib)
    monkeypatch.setattr(cloud, "GLib", glib)
    monkeypatch.setattr(module.time, "monotonic", lambda: glib.now / 1000)
    monkeypatch.setattr(cloud, "ASRClient", create_client)
    monkeypatch.setattr(module, "AudioCapture", Mock())
    monkeypatch.setattr(cloud, "ParamsStore", Mock())
    state = AppState()
    state.login_status = LoginStatus.LOGGED_IN

    def reauthenticate():
        state.login_status = LoginStatus.NOT_LOGGED_IN
        auth_expired()

    def create_session(callbacks):
        return DoubaoRecognitionSession(
            callbacks, extract_params=extract_params,
            save_params=cloud.ParamsStore.save, on_auth_expired=reauthenticate,
        )

    manager = module.TranscriptionManager(state, create_session)
    manager.on_paste = Mock()
    manager.on_copy = Mock()
    manager.on_overlay_hide = Mock()
    return manager, glib, clients


@pytest.mark.parametrize("auth_failure", [False, True])
def test_exit_cleans_up_once_without_pasting(recording, auth_failure, auth_expired):
    manager, glib, clients = recording
    manager.handle_start()
    manager.app_state.transcription_text = "unfinished"

    if auth_failure:
        clients[-1].on_auth_error()
        glib.advance(0)
        cloud.ParamsStore.clear.assert_called_once_with()
        auth_expired.assert_called_once_with()
        assert manager.app_state.login_status == LoginStatus.NOT_LOGGED_IN
    else:
        manager.handle_cancel()
        cloud.ParamsStore.clear.assert_not_called()

    manager.audio_capture.stop.assert_called_once_with()
    clients[-1].disconnect.assert_called_once_with()
    manager.on_paste.assert_not_called()
    if auth_failure:
        manager.on_copy.assert_called_once_with("unfinished")
    else:
        manager.on_copy.assert_not_called()
    assert manager.app_state.recording_state == RecordingState.IDLE


def test_rejected_cached_login_cleans_up_and_prompts_login(recording, auth_expired):
    manager, glib, clients = recording
    manager.handle_start()
    manager.app_state.transcription_text = "unfinished"
    error = SimpleNamespace(response=SimpleNamespace(
        status_code=200,
        body=b'{"code":710012001,"message":"login invalid"}',
    ))

    clients[-1].on_error(error)
    glib.advance(0)

    cloud.ParamsStore.clear.assert_called_once_with()
    auth_expired.assert_called_once_with()
    manager.audio_capture.stop.assert_called_once_with()
    clients[-1].disconnect.assert_called_once_with()
    manager.on_paste.assert_not_called()
    assert manager.app_state.login_status == LoginStatus.NOT_LOGGED_IN
    assert manager.app_state.recording_state == RecordingState.IDLE


def test_cancelled_recording_ignores_late_auth_error(recording, auth_expired):
    manager, glib, clients = recording
    manager.handle_start()
    manager.handle_cancel()

    clients[-1].on_auth_error()
    glib.advance(0)

    cloud.ParamsStore.clear.assert_not_called()
    auth_expired.assert_not_called()
    assert manager.app_state.login_status == LoginStatus.LOGGED_IN
    assert manager.app_state.recording_state == RecordingState.IDLE


@pytest.mark.parametrize("next_state", ["idle", "starting", "recording", "stopping"])
@pytest.mark.parametrize("event, args", [
    ("on_open", ()),
    ("on_result", ("old recording",)),
    ("on_finish", ()),
    ("on_error", (RuntimeError("old network error"),)),
    ("on_auth_error", ()),
    ("on_response", ()),
    ("on_close", ()),
])
def test_cancelled_session_ignores_callbacks(recording, next_state, event, args, auth_expired):
    manager, glib, clients = recording
    manager.handle_start()
    old_callback = getattr(clients[-1], event)
    # Exercise both an event already queued in GTK and one emitted after reset.
    old_callback(*args)
    manager.handle_cancel()
    if next_state != "idle":
        manager.handle_start()
        if next_state != "starting":
            clients[-1].on_open()
            glib.advance(0)
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
    cloud.ParamsStore.clear.assert_not_called()
    auth_expired.assert_not_called()
    manager.on_paste.assert_not_called()
    manager.on_copy.assert_not_called()


def test_recording_uses_a_fresh_client_and_dispatches_current_events(recording):
    manager, glib, clients = recording
    manager.handle_start()
    old_client = clients[-1]
    manager.handle_cancel()
    manager.handle_start()

    assert clients[-1] is not old_client
    old_client.disconnect.assert_called_once_with()
    clients[-1].on_open()
    clients[-1].on_result("current recording")
    glib.advance(0)

    assert manager.app_state.recording_state == RecordingState.RECORDING
    assert manager.app_state.transcription_text == "current recording"
    manager.handle_stop()
    clients[-1].on_finish()
    glib.advance(0)
    manager.on_paste.assert_called_once_with("current recording")
    manager.on_copy.assert_not_called()


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("params", [None, Mock()])
def test_cancelled_session_ignores_parameter_extraction(recording, restart, params, extract_params):
    manager, glib, clients = recording
    cloud.ParamsStore.load.return_value = None
    callbacks = []
    extract_params.side_effect = callbacks.append
    manager.handle_start()
    old_client = clients[-1]
    manager.handle_cancel()
    if restart:
        manager.handle_start()
    expected_state = manager.app_state.recording_state

    callbacks[0](params)
    glib.advance(int(SESSION_NOTICE_DURATION * 1000))

    assert manager.app_state.recording_state == expected_state
    assert manager.app_state.error_message is None
    cloud.ParamsStore.save.assert_not_called()
    old_client.connect.assert_not_called()
    if restart:
        clients[-1].connect.assert_not_called()


def test_stop_before_parameter_extraction_preserves_current_session(recording, extract_params):
    manager, glib, clients = recording
    cloud.ParamsStore.load.return_value = None
    callbacks = []
    extract_params.side_effect = callbacks.append
    params = Mock()
    manager.handle_start()
    manager.handle_stop()

    callbacks[0](params)

    cloud.ParamsStore.save.assert_called_once_with(params)
    clients[-1].connect.assert_called_once_with(params)
    clients[-1].finish_sending.assert_called_once_with()
    assert manager.app_state.recording_state == RecordingState.STOPPING


def test_stop_waits_for_latest_result(recording):
    manager, glib, clients = recording
    manager.handle_start()
    clients[-1].on_open()
    glib.advance(0)
    manager.handle_stop()
    clients[-1].finish_sending.assert_called_once_with()
    clients[-1].on_result("first")
    glib.advance(0)
    glib.advance(int(FINAL_RESULT_QUIET_PERIOD * 1000) - 1)
    clients[-1].on_result("final words")
    glib.advance(0)
    glib.advance(int(FINAL_RESULT_QUIET_PERIOD * 1000))

    manager.on_paste.assert_called_once_with("final words")
    assert manager.app_state.recording_state == RecordingState.IDLE
    glib.advance(int(STOP_SAFETY_TIMEOUT * 1000))
    manager.on_paste.assert_called_once()


def test_safety_timeout_completes_without_finish_event(recording):
    manager, glib, clients = recording
    manager.handle_start()
    clients[-1].on_result("current words")
    glib.advance(0)
    manager.handle_stop()
    glib.advance(int(STOP_SAFETY_TIMEOUT * 1000))

    manager.on_paste.assert_called_once_with("current words")
    assert manager.app_state.recording_state == RecordingState.IDLE


def test_cancel_discards_pending_completion(recording):
    manager, glib, clients = recording
    manager.handle_start()
    manager.handle_stop()
    clients[-1].on_result("cancelled")
    glib.advance(0)
    manager.handle_cancel()
    glib.advance(int(STOP_SAFETY_TIMEOUT * 1000))

    manager.on_paste.assert_not_called()


def test_delayed_clear_does_not_erase_next_recording(recording):
    manager, glib, clients = recording
    manager.handle_start()
    manager.handle_cancel()
    manager.handle_start()
    clients[-1].on_result("new recording")
    glib.advance(0)
    glib.advance(200)

    assert manager.app_state.transcription_text == "new recording"


def test_old_error_reset_does_not_stop_next_recording(recording):
    manager, glib, clients = recording
    manager.handle_start()
    clients[-1].on_error(RuntimeError("network unavailable"))
    glib.advance(0)
    cloud.ParamsStore.clear.assert_not_called()
    manager.handle_cancel()
    manager.handle_start()
    clients[-1].on_open()
    glib.advance(0)
    glib.advance(int(SESSION_NOTICE_DURATION * 1000))

    assert manager.app_state.recording_state == RecordingState.RECORDING


def test_error_reset_still_returns_to_idle(recording):
    manager, glib, clients = recording
    manager.handle_start()
    clients[-1].on_error(RuntimeError("network unavailable"))
    glib.advance(0)
    glib.advance(int(SESSION_NOTICE_DURATION * 1000))

    assert manager.app_state.recording_state == RecordingState.IDLE
    manager.on_paste.assert_not_called()
    cloud.ParamsStore.clear.assert_not_called()


def test_idle_text_is_still_cleared_after_delay(recording):
    manager, glib, clients = recording
    manager.handle_start()
    clients[-1].on_result("cancelled text")
    glib.advance(0)
    manager.handle_cancel()
    glib.advance(199)
    assert manager.app_state.transcription_text == "cancelled text"

    glib.advance(1)

    assert manager.app_state.transcription_text == ""


def test_repeated_errors_have_one_reset(recording):
    manager, glib, clients = recording
    manager.handle_start()
    clients[-1].on_error(RuntimeError("first error"))
    glib.advance(0)
    glib.advance(100)
    clients[-1].on_error(RuntimeError("second error"))
    glib.advance(0)
    glib.advance(int(SESSION_NOTICE_DURATION * 1000) + 200)

    clients[-1].disconnect.assert_called_once_with()
    assert manager.app_state.recording_state == RecordingState.IDLE


@pytest.mark.parametrize("failure", ["microphone", "missing_params"])
def test_startup_failure_reset_is_cancelled_for_next_recording(recording, failure):
    manager, glib, clients = recording
    if failure == "microphone":
        manager.audio_capture.start.side_effect = RuntimeError("no microphone")
    else:
        cloud.ParamsStore.load.return_value = None
    manager.handle_start()
    assert manager.app_state.session_status.fault is not None
    manager.handle_cancel()

    manager.audio_capture.start.side_effect = None
    cloud.ParamsStore.load.return_value = Mock()
    manager.handle_start()
    clients[-1].on_open()
    glib.advance(0)
    glib.advance(int(SESSION_NOTICE_DURATION * 1000))

    assert manager.app_state.recording_state == RecordingState.RECORDING


@pytest.mark.parametrize("waiting_for_params", [False, True])
def test_startup_deadline_covers_params_and_connection(recording, waiting_for_params, extract_params):
    manager, glib, clients = recording
    callbacks = []
    if waiting_for_params:
        cloud.ParamsStore.load.return_value = None
        extract_params.side_effect = callbacks.append
    manager.handle_start()
    glib.advance(int(SESSION_START_TIMEOUT * 1000))

    assert manager.app_state.recording_state == RecordingState.IDLE
    assert manager.app_state.session_status.fault == (
        SessionFault.PARAMETER_TIMEOUT if waiting_for_params
        else SessionFault.CONNECTION_TIMEOUT
    )
    manager.audio_capture.stop.assert_called_once()
    clients[-1].disconnect.assert_called_once()
    manager.on_overlay_hide.assert_not_called()
    cloud.ParamsStore.clear.assert_not_called()
    if callbacks:
        callbacks[0](Mock())
        clients[-1].connect.assert_not_called()
        cloud.ParamsStore.save.assert_not_called()


def test_stopping_before_connect_keeps_speech_until_original_deadline(recording):
    manager, glib, clients = recording
    manager.handle_start()
    glib.advance(1000)
    manager.handle_stop()
    glib.advance(int(STOP_SAFETY_TIMEOUT * 1000))
    assert manager.app_state.recording_state == RecordingState.STOPPING
    clients[-1].disconnect.assert_not_called()

    glib.advance(int(SESSION_START_TIMEOUT * 1000) - glib.now)
    assert manager.app_state.recording_state == RecordingState.IDLE
    assert manager.app_state.session_status.fault == SessionFault.CONNECTION_TIMEOUT
    manager.on_paste.assert_not_called()


def test_connection_after_stop_starts_final_result_budget(recording):
    manager, glib, clients = recording
    manager.handle_start()
    manager.handle_stop()
    glib.advance(3000)
    clients[-1].on_open()
    glib.advance(0)
    assert manager.app_state.session_status.phase == ConnectionPhase.CONNECTED
    assert manager.app_state.recording_state == RecordingState.STOPPING
    glib.advance(int(STOP_SAFETY_TIMEOUT * 1000) - 1)
    clients[-1].on_result("buffered speech")
    glib.advance(0)
    glib.advance(1)
    manager.on_paste.assert_called_once_with("buffered speech")
    glib.advance(int(SESSION_START_TIMEOUT * 1000))
    assert manager.app_state.session_status.fault is None


def test_empty_responses_update_health_without_completing_or_erasing_text(recording):
    manager, glib, clients = recording
    manager.handle_start()
    clients[-1].on_open()
    clients[-1].on_result("keep this text")
    glib.advance(0)
    manager.handle_stop()
    clients[-1].on_response()
    glib.advance(0)
    glib.advance(int(FINAL_RESULT_QUIET_PERIOD * 1000) + 1)
    assert manager.app_state.session_status.last_response_at == 0
    assert manager.app_state.transcription_text == "keep this text"
    assert manager.app_state.recording_state == RecordingState.STOPPING
    manager.on_paste.assert_not_called()


@pytest.mark.parametrize("ending", ["timeout", "finish", "close"])
def test_empty_completion_releases_resources_but_keeps_notice(recording, ending):
    manager, glib, clients = recording
    manager.handle_start()
    clients[-1].on_open()
    glib.advance(0)
    manager.handle_stop()
    if ending == "timeout":
        glib.advance(int(STOP_SAFETY_TIMEOUT * 1000))
    else:
        getattr(clients[-1], f"on_{ending}")()
        glib.advance(0)

    assert manager.app_state.recording_state == RecordingState.IDLE
    assert manager.app_state.session_status.fault == SessionFault.NO_TEXT
    clients[-1].disconnect.assert_called_once()
    manager.on_paste.assert_not_called()
    manager.on_overlay_hide.assert_not_called()
    glib.advance(int(SESSION_NOTICE_DURATION * 1000))
    manager.on_overlay_hide.assert_called_once()
    assert manager.app_state.session_status.fault is None


@pytest.mark.parametrize("error, fault", [
    (OSError("network unavailable"), SessionFault.CONNECTION_FAILED),
    (TimeoutError(), SessionFault.CONNECTION_TIMEOUT),
    (ASRServiceError("busy"), SessionFault.SERVICE),
])
def test_failure_releases_immediately_and_old_notice_cannot_hide_new_session(
    recording, error, fault
):
    manager, glib, clients = recording
    manager.handle_start()
    old_client = clients[-1]
    old_client.on_error(error)
    glib.advance(0)
    assert manager.app_state.session_status.fault == fault
    assert manager.app_state.recording_state == RecordingState.IDLE
    old_client.disconnect.assert_called_once()
    manager.audio_capture.stop.assert_called_once()
    cloud.ParamsStore.clear.assert_not_called()

    manager.handle_start()  # No cancellation/recovery delay is required.
    clients[-1].on_open()
    glib.advance(0)
    old_client.on_close()
    old_client.on_error(error)
    glib.advance(int(SESSION_NOTICE_DURATION * 1000))
    assert manager.app_state.recording_state == RecordingState.RECORDING
    assert manager.app_state.session_status.fault is None
    manager.on_overlay_hide.assert_not_called()


def test_unexpected_peer_close_preserves_partial_text_without_pasting(recording):
    manager, glib, clients = recording
    manager.handle_start()
    clients[-1].on_open()
    clients[-1].on_result("partial text")
    clients[-1].on_close()
    glib.advance(0)
    assert manager.app_state.recording_state == RecordingState.IDLE
    assert manager.app_state.session_status.fault == SessionFault.DISCONNECTED
    assert manager.app_state.transcription_text == "partial text"
    manager.on_paste.assert_not_called()
    manager.on_copy.assert_called_once_with("partial text")
    cloud.ParamsStore.clear.assert_not_called()


@pytest.mark.parametrize("ending", ["close", "network", "timeout", "service", "auth"])
def test_failed_session_copies_latest_result_once_before_cleanup(recording, ending):
    manager, glib, clients = recording
    manager.handle_start()
    client = clients[-1]
    client.on_open()
    client.on_result("earlier hypothesis")
    client.on_result("  corrected complete transcript  ")
    glib.advance(0)

    def copy_before_cleanup(text):
        client.disconnect.assert_not_called()
        assert manager.app_state.recording_state == RecordingState.RECORDING

    manager.on_copy.side_effect = copy_before_cleanup
    if ending == "close":
        client.on_close()
    elif ending == "auth":
        client.on_auth_error()
    else:
        error = {
            "network": OSError("connection reset"),
            "timeout": TimeoutError("connection timed out"),
            "service": ASRServiceError("service stopped"),
        }[ending]
        client.on_error(error)
    # Other terminal callbacks may already be queued for the failed session.
    client.on_close()
    client.on_error(OSError("late error"))
    glib.advance(0)

    manager.on_copy.assert_called_once_with("corrected complete transcript")
    manager.on_paste.assert_not_called()
    client.disconnect.assert_called_once()
    assert manager.app_state.recording_state == RecordingState.IDLE
    glib.advance(int(SESSION_NOTICE_DURATION * 1000))
    assert manager.app_state.transcription_text == ""
    manager.on_copy.assert_called_once()


@pytest.mark.parametrize("text", ["", " \n\t"])
def test_failure_without_recognized_text_does_not_replace_clipboard(recording, text):
    manager, glib, clients = recording
    manager.handle_start()
    clients[-1].on_open()
    clients[-1].on_result(text)
    clients[-1].on_error(OSError("connection lost"))
    glib.advance(int(SESSION_NOTICE_DURATION * 1000))

    manager.on_copy.assert_not_called()
    manager.on_paste.assert_not_called()


def test_disconnect_while_waiting_for_final_result_copies_without_pasting(recording):
    manager, glib, clients = recording
    manager.handle_start()
    clients[-1].on_open()
    glib.advance(0)
    clients[-1].on_result("latest recognized text")
    glib.advance(0)
    manager.handle_stop()
    clients[-1].on_error(OSError("connection reset while finalizing"))
    glib.advance(int(SESSION_NOTICE_DURATION * 1000))

    manager.on_copy.assert_called_once_with("latest recognized text")
    manager.on_paste.assert_not_called()
    assert manager.app_state.recording_state == RecordingState.IDLE


def test_batch_backend_needs_neither_login_nor_doubao_deadlines(monkeypatch):
    glib = FakeGLib()
    monkeypatch.setattr(module, "GLib", glib)
    audio = Mock()
    audio.start.side_effect = lambda on_audio_data: on_audio_data(b"\x01\x00")
    monkeypatch.setattr(module, "AudioCapture", lambda: audio)
    state = AppState()
    state.login_status = LoginStatus.NOT_LOGGED_IN
    sessions = []

    class BatchSession:
        def __init__(self, callbacks):
            self.callbacks = callbacks
            self.pcm = []
            self.closed = False

        def start(self):
            # PortAudio can already have delivered a block while opening.
            assert self.pcm == [b"\x01\x00"]

        def send_audio(self, pcm):
            self.pcm.append(pcm)

        def finish(self):
            audio.stop.assert_called_once()
            glib.timeout_add(10_000, self.inference_finished)

        def inference_finished(self):
            self.callbacks.ready()
            self.callbacks.result("本地识别的全文")
            self.callbacks.completed()
            return glib.SOURCE_REMOVE

        def close(self):
            self.closed = True

    def create_session(callbacks):
        sessions.append(BatchSession(callbacks))
        return sessions[-1]

    manager = module.TranscriptionManager(state, create_session)
    manager.on_paste = Mock()
    manager.handle_start()
    assert state.recording_state == RecordingState.STARTING
    manager.handle_stop()  # End input while the recognizer is still preparing.
    glib.advance(9_999)
    assert state.recording_state == RecordingState.STOPPING
    manager.on_paste.assert_not_called()

    glib.advance(1)
    manager.on_paste.assert_called_once_with("本地识别的全文")
    assert sessions[0].closed
    assert state.recording_state == RecordingState.IDLE
    assert state.login_status == LoginStatus.NOT_LOGGED_IN


def test_late_callbacks_from_a_cancelled_backend_cannot_end_next_recording(monkeypatch):
    glib = FakeGLib()
    monkeypatch.setattr(module, "GLib", glib)
    monkeypatch.setattr(module, "AudioCapture", Mock())
    state = AppState()
    callbacks = []

    def create_session(events):
        callbacks.append(events)
        return Mock()

    manager = module.TranscriptionManager(state, create_session)
    manager.on_copy = Mock()
    manager.on_paste = Mock()
    manager.handle_start()
    manager.handle_cancel()
    manager.handle_start()
    callbacks[1].ready()
    callbacks[1].result("current session")
    callbacks[0].result("cancelled session")
    callbacks[0].failed(SessionFault.RECOGNITION)
    callbacks[0].completed()

    assert state.recording_state == RecordingState.RECORDING
    assert state.transcription_text == "current session"
    manager.on_copy.assert_not_called()
    manager.on_paste.assert_not_called()


@pytest.mark.parametrize("ending", ["cancel", "failure"])
def test_backend_close_error_leaves_recorder_ready_for_next_session(recording, ending):
    manager, glib, clients = recording
    state = manager.app_state
    manager.on_cancel_enabled_changed = Mock()
    manager.handle_start()
    clients[0].disconnect.side_effect = RuntimeError("cleanup failed")

    if ending == "cancel":
        manager.handle_cancel()
        manager.on_overlay_hide.assert_called_once()
        manager.on_copy.assert_not_called()
    else:
        clients[0].on_open()
        clients[0].on_result("partial transcript")
        clients[0].on_error(ConnectionResetError("connection lost"))
        glib.advance(0)
        assert state.session_status.fault == SessionFault.DISCONNECTED
        manager.on_copy.assert_called_once_with("partial transcript")

    assert state.recording_state == RecordingState.IDLE
    manager.audio_capture.stop.assert_called_once()
    manager.on_cancel_enabled_changed.assert_called_with(False)
    manager.on_paste.assert_not_called()
    manager.handle_start()
    assert state.recording_state == RecordingState.STARTING
    assert len(clients) == 2
    assert state.session_status.fault is None


def test_only_failure_callback_ends_recording_and_sets_fault(monkeypatch):
    monkeypatch.setattr(module, "GLib", FakeGLib())
    monkeypatch.setattr(module, "AudioCapture", Mock())
    state = AppState()
    backend = Mock()
    factory = Mock(return_value=backend)
    manager = module.TranscriptionManager(state, factory)
    manager.handle_start()
    callbacks, = factory.call_args.args
    callbacks.ready()

    callbacks.progress(SessionStatus(
        phase=ConnectionPhase.CONNECTED, phase_since=2.0, last_response_at=3.0,
        fault=SessionFault.RECOGNITION,
    ))
    assert state.session_status == SessionStatus(
        phase=ConnectionPhase.CONNECTED, phase_since=2.0, last_response_at=3.0,
    )
    assert state.recording_state == RecordingState.RECORDING
    backend.close.assert_not_called()

    callbacks.failed(SessionFault.RECOGNITION)
    assert state.session_status.fault == SessionFault.RECOGNITION
    assert state.recording_state == RecordingState.IDLE
    backend.close.assert_called_once()
