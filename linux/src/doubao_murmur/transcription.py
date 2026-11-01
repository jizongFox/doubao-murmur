"""State machine orchestrator for the recording lifecycle.

Mirrors TranscriptionManager.swift.

Key design decisions:
- GLib.idle_add() marshals callbacks from the asyncio thread to GTK main thread
- GLib.timeout_add() replaces DispatchQueue.main.asyncAfter for delayed execution
- State machine exactly mirrors macOS: idle -> starting -> recording -> stopping -> idle
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace

from gi.repository import GLib

from doubao_murmur.app_state import AppState, LoginStatus, RecordingState
from doubao_murmur.asr_client import ASRClient, ASRServiceError, is_handshake_auth_rejection
from doubao_murmur.audio_capture import AudioCapture
from doubao_murmur.config import (
    FINAL_RESULT_QUIET_PERIOD,
    SESSION_NOTICE_DURATION,
    SESSION_START_TIMEOUT,
    STOP_SAFETY_TIMEOUT,
)
from doubao_murmur.params_store import ASRParams, ParamsStore
from doubao_murmur.session_status import ConnectionPhase, SessionFault, SessionStatus

logger = logging.getLogger(__name__)


class TranscriptionManager:
    """Orchestrates the recording lifecycle."""

    def __init__(self, app_state: AppState) -> None:
        self.app_state = app_state
        self.asr_client: ASRClient | None = None
        self._session: object | None = None
        self.audio_capture = AudioCapture()

        self.using_cached_params = False
        self.awaiting_final_result = False
        self.safety_timer_id: int | None = None
        # Pending completion, rescheduled on each late result so only the last wins.
        self.quiet_timer_id: int | None = None
        self.start_timer_id: int | None = None
        self.notice_timer_id: int | None = None
        self.clear_text_timer_id: int | None = None

        # Callbacks set by app.py
        self.on_auth_expired = None  # () -> None
        self.on_show_login = None  # () -> None
        self.on_params_needed = None  # (callback: (ASRParams|None)->None) -> None
        self.on_save_params = None  # (ASRParams) -> bool, after recording acceptance
        self.on_overlay_show = None  # () -> None
        self.on_overlay_hide = None  # () -> None
        self.on_paste = None  # (text: str) -> None
        self.on_cancel_enabled_changed = None  # (enabled: bool) -> None

    def _wire_asr_callbacks(self, session: object) -> None:
        """Marshal callbacks to GTK and check their session when dispatched."""
        def dispatch(callback, *args):
            GLib.idle_add(self._dispatch_session_callback, session, callback, *args)

        self.asr_client.on_open = lambda: dispatch(self._on_asr_open)
        self.asr_client.on_result = lambda text: dispatch(self._on_asr_result, text)
        self.asr_client.on_finish = lambda: dispatch(self._on_asr_finish)
        self.asr_client.on_error = lambda err: dispatch(self._on_asr_error, err)
        self.asr_client.on_auth_error = lambda: dispatch(self._on_auth_error)
        self.asr_client.on_response = lambda: dispatch(self._on_asr_response)
        self.asr_client.on_close = lambda: dispatch(self._on_asr_close)

    def _dispatch_session_callback(self, session: object, callback, *args) -> bool:
        """Old recordings cannot mutate state, even after a new one starts."""
        if session is self._session:
            callback(*args)
        return GLib.SOURCE_REMOVE

    # --- Recording controls ---

    def handle_toggle(self) -> None:
        """Called on GTK main thread from hotkey manager."""
        state = self.app_state.recording_state
        if state == RecordingState.IDLE:
            self.handle_start()
        elif state in (RecordingState.STARTING, RecordingState.RECORDING):
            self.handle_stop()
        # STOPPING: ignore

    def handle_start(self) -> None:
        """Start recording unless a recording is already in progress."""
        if self.app_state.recording_state == RecordingState.IDLE:
            self._start_recording()

    def handle_stop(self) -> None:
        """Stop only an active or starting recording."""
        if self.app_state.recording_state in (
            RecordingState.STARTING,
            RecordingState.RECORDING,
        ):
            self._stop_recording()

    def _start_recording(self) -> None:
        if self.app_state.login_status != LoginStatus.LOGGED_IN:
            logger.warning("Not logged in, showing login window")
            if self.on_show_login:
                self.on_show_login()
            return

        logger.info("Starting recording...")
        self._cancel_pending_timers()
        session = self._session = object()
        # Each recording owns its transport and buffered audio. Never reuse a
        # client while its previous connection may still be shutting down.
        self.asr_client = ASRClient()
        self._wire_asr_callbacks(session)
        self._set_state(RecordingState.STARTING)
        self.app_state.transcription_text = ""
        self.app_state.error_message = None
        self.app_state.session_status = SessionStatus(
            phase=ConnectionPhase.PARAMETERS, phase_since=time.monotonic()
        )
        if self.on_overlay_show:
            self.on_overlay_show()

        # Start audio immediately (buffered in ASR client until WS connects)
        try:
            self.audio_capture.start(on_audio_data=self.asr_client.send_audio)
        except Exception as e:
            logger.error("Audio capture failed: %s", e)
            self._show_notice(SessionFault.MICROPHONE)
            return

        self.start_timer_id = GLib.timeout_add(
            int(SESSION_START_TIMEOUT * 1000), self._start_timeout
        )
        # Try cached params first, fall back to WebView extraction
        cached = ParamsStore.load()
        if cached:
            logger.info("Using cached ASR params")
            self.using_cached_params = True
            self._connect(cached)
        elif self.on_params_needed:
            self.using_cached_params = False
            self.on_params_needed(
                lambda params: self._dispatch_session_callback(
                    session, self._on_params_extracted, params
                )
            )
        else:
            self._show_notice(SessionFault.PARAMETERS)

    def _connect(self, params: ASRParams) -> None:
        self.app_state.session_status = replace(
            self.app_state.session_status,
            phase=ConnectionPhase.CONNECTING, phase_since=time.monotonic(),
        )
        self.asr_client.connect(params)

    def _start_timeout(self) -> bool:
        self.start_timer_id = None
        fault = (
            SessionFault.PARAMETER_TIMEOUT
            if self.app_state.session_status.phase == ConnectionPhase.PARAMETERS
            else SessionFault.CONNECTION_TIMEOUT
        )
        self._show_notice(fault)
        return GLib.SOURCE_REMOVE

    def _stop_recording(self) -> None:
        logger.info("Stopping recording...")
        self._set_state(RecordingState.STOPPING)
        self.audio_capture.stop()
        # Flushes trailing silence so the server finalises the last word.
        self.asr_client.finish_sending()
        self.awaiting_final_result = True

        # Before connection, retain buffered speech under the original startup
        # deadline. Only a connected stream gets the final-result timeout.
        if self.app_state.session_status.phase == ConnectionPhase.CONNECTED:
            self._schedule_stop_timeout()

    def _schedule_stop_timeout(self) -> None:
        self.safety_timer_id = GLib.timeout_add(
            int(STOP_SAFETY_TIMEOUT * 1000), self._safety_timeout
        )

    def _schedule_final_completion(self) -> None:
        """Wait for the result stream to go quiet before accepting the transcript.

        The server streams corrections right up to the end: after the audio stops
        it replays the pending partial results first, and only then sends the one
        carrying the final word. Completing on the first result to arrive after
        stopping truncates the tail -- the "last two characters are missing"
        symptom -- so each late result pushes the deadline back instead.
        """
        if self.quiet_timer_id is not None:
            GLib.source_remove(self.quiet_timer_id)
        self.quiet_timer_id = GLib.timeout_add(
            int(FINAL_RESULT_QUIET_PERIOD * 1000), self._on_stream_quiet
        )

    def _on_stream_quiet(self) -> bool:
        self.quiet_timer_id = None
        if self.awaiting_final_result:
            logger.info("Result stream quiet, completing")
            self.awaiting_final_result = False
            self._complete_transcription()
        return GLib.SOURCE_REMOVE

    def _safety_timeout(self) -> bool:
        # Clear first: completing resets state, which drops pending stop timers,
        # and this source must not be among them while it is still running.
        self.safety_timer_id = None
        if self.app_state.recording_state == RecordingState.STOPPING:
            logger.info("Safety timeout, completing with current text")
            self.awaiting_final_result = False
            self._complete_transcription()
        return GLib.SOURCE_REMOVE

    # --- ASR callbacks (on GTK main thread via GLib.idle_add) ---

    def _on_asr_open(self) -> bool:
        if self.start_timer_id is not None:
            GLib.source_remove(self.start_timer_id)
            self.start_timer_id = None
        self.app_state.session_status = replace(
            self.app_state.session_status,
            phase=ConnectionPhase.CONNECTED, phase_since=time.monotonic(),
        )
        if self.app_state.recording_state == RecordingState.STARTING:
            self._set_state(RecordingState.RECORDING)
        elif (
            self.app_state.recording_state == RecordingState.STOPPING
            and self.safety_timer_id is None
        ):
            self._schedule_stop_timeout()
        return GLib.SOURCE_REMOVE

    def _on_asr_response(self) -> bool:
        self.app_state.session_status = replace(
            self.app_state.session_status, last_response_at=time.monotonic()
        )
        return GLib.SOURCE_REMOVE

    def _on_asr_result(self, text: str) -> bool:
        self.app_state.transcription_text = text
        if self.app_state.session_status.phase != ConnectionPhase.CONNECTED:
            self._on_asr_open()
        if self.awaiting_final_result:
            self._schedule_final_completion()
        return GLib.SOURCE_REMOVE

    def _on_asr_finish(self) -> bool:
        self.awaiting_final_result = False
        if self.app_state.recording_state in (
            RecordingState.STOPPING,
            RecordingState.RECORDING,
            RecordingState.STARTING,
        ):
            self._complete_transcription()
        return GLib.SOURCE_REMOVE

    def _on_asr_close(self) -> bool:
        if self.app_state.recording_state == RecordingState.STOPPING:
            self._complete_transcription()
        else:
            self._show_notice(SessionFault.DISCONNECTED)
        return GLib.SOURCE_REMOVE

    def _on_asr_error(self, error) -> bool:
        if self.app_state.recording_state == RecordingState.IDLE:
            return GLib.SOURCE_REMOVE
        logger.error("ASR error: %s", error)
        # Only treat the error as an auth problem when the server actively
        # rejected the handshake over the login. Network failures (timeouts,
        # DNS, resets) used to be lumped in here, which cleared perfectly
        # valid cached credentials and forced a re-login after every
        # transient connectivity hiccup. In-band auth errors and the silent
        # close of an expired login arrive via _on_auth_error instead.
        if self.using_cached_params and is_handshake_auth_rejection(error):
            self._handle_auth_failure()
            return GLib.SOURCE_REMOVE
        if isinstance(error, ASRServiceError):
            fault = SessionFault.SERVICE
        elif isinstance(error, (TimeoutError, asyncio.TimeoutError)):
            fault = SessionFault.CONNECTION_TIMEOUT
        elif self.app_state.session_status.phase == ConnectionPhase.CONNECTED:
            fault = SessionFault.DISCONNECTED
        else:
            fault = SessionFault.CONNECTION_FAILED
        self._show_notice(fault)
        return GLib.SOURCE_REMOVE

    def _on_auth_error(self) -> bool:
        # A pending handshake may finish after the user cancels recording.
        if self.app_state.recording_state == RecordingState.IDLE:
            return GLib.SOURCE_REMOVE
        self._handle_auth_failure()
        return GLib.SOURCE_REMOVE

    # --- Completion & Reset ---

    def _show_notice(self, fault: SessionFault) -> None:
        """Release resources now; a notice must not block the next recording."""
        status = replace(
            self.app_state.session_status,
            phase=ConnectionPhase.CLOSED, phase_since=time.monotonic(), fault=fault,
        )
        self._release_session()
        self.app_state.session_status = status
        self.notice_timer_id = GLib.timeout_add(
            int(SESSION_NOTICE_DURATION * 1000), self._dismiss_notice
        )

    def _dismiss_notice(self) -> bool:
        self.notice_timer_id = None
        self.app_state.session_status = SessionStatus()
        self.app_state.transcription_text = ""
        if self.on_overlay_hide:
            self.on_overlay_hide()
        return GLib.SOURCE_REMOVE

    def _clear_transcription_text(self) -> bool:
        self.clear_text_timer_id = None
        self.app_state.transcription_text = ""
        return GLib.SOURCE_REMOVE

    def _cancel_pending_timers(self) -> None:
        """Prevent delayed work from surviving reset or entering a new recording."""
        for attr in (
            "quiet_timer_id",
            "safety_timer_id",
            "start_timer_id",
            "notice_timer_id",
            "clear_text_timer_id",
        ):
            timer_id = getattr(self, attr)
            if timer_id is not None:
                GLib.source_remove(timer_id)
                setattr(self, attr, None)

    def _complete_transcription(self) -> None:
        text = self.app_state.transcription_text.strip()
        logger.info("Completing transcription: '%s'", text[:50])
        if not text:
            self._show_notice(SessionFault.NO_TEXT)
            return
        if self.on_paste:
            self.on_paste(text)
        self._reset_to_idle()

    def _release_session(self) -> None:
        # Invalidate queued callbacks before stopping the resources that emit them.
        self._session = None
        self.awaiting_final_result = False
        self._cancel_pending_timers()
        self.audio_capture.stop()
        if self.asr_client:
            self.asr_client.disconnect()
        self._set_state(RecordingState.IDLE)
        self.using_cached_params = False

    def _reset_to_idle(self) -> bool:
        self._release_session()
        self.app_state.session_status = SessionStatus()
        self.app_state.error_message = None
        if self.on_overlay_hide:
            self.on_overlay_hide()
        # Clear text after short delay
        self.clear_text_timer_id = GLib.timeout_add(
            200, self._clear_transcription_text
        )
        return GLib.SOURCE_REMOVE

    def handle_cancel(self) -> None:
        if (
            self.app_state.recording_state == RecordingState.IDLE
            and self.notice_timer_id is None
        ):
            return
        logger.info("Cancelling transcription")
        self._reset_to_idle()

    def _handle_auth_failure(self) -> None:
        logger.warning("Auth failure, clearing cached params")
        ParamsStore.clear()
        self._show_notice(SessionFault.AUTH)
        self.app_state.login_status = LoginStatus.NOT_LOGGED_IN
        if self.on_auth_expired:
            self.on_auth_expired()

    def _set_state(self, new_state: RecordingState) -> None:
        self.app_state.recording_state = new_state
        if self.on_cancel_enabled_changed:
            self.on_cancel_enabled_changed(new_state != RecordingState.IDLE)

    def _on_params_extracted(self, params: ASRParams | None) -> None:
        """Persist and connect only after the recording accepts this completion."""
        if params is None:
            self._show_notice(SessionFault.PARAMETERS)
        elif self.on_save_params is None or not self.on_save_params(params):
            self._show_notice(SessionFault.CREDENTIAL_SAVE)
        else:
            self._connect(params)
