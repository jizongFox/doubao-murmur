"""Own microphone capture, recording state, and transcript delivery."""

from __future__ import annotations

import logging
import time
from dataclasses import replace

from gi.repository import GLib

from doubao_murmur.app_state import AppState, RecordingState
from doubao_murmur.audio_capture import AudioCapture
from doubao_murmur.config import SESSION_NOTICE_DURATION
from doubao_murmur.recognition import RecognitionCallbacks, RecognitionSession, SessionFactory
from doubao_murmur.session_status import ConnectionPhase, SessionFault, SessionStatus

logger = logging.getLogger(__name__)


class TranscriptionManager:
    """Run the same recording flow for every recognition implementation."""

    def __init__(self, app_state: AppState, create_session: SessionFactory) -> None:
        self.app_state = app_state
        self._create_session = create_session
        self._session: RecognitionSession | None = None
        self.audio_capture = AudioCapture()
        self.notice_timer_id: int | None = None

        # Callbacks set by app.py
        self.on_overlay_show = None  # () -> None
        self.on_overlay_hide = None  # () -> None
        self.on_paste = None  # (text: str) -> None
        self.on_copy = None  # (text: str) -> None, interrupted recording only
        self.on_cancel_enabled_changed = None  # (enabled: bool) -> None

    def handle_toggle(self) -> None:
        state = self.app_state.recording_state
        if state == RecordingState.IDLE:
            self.handle_start()
        elif state in (RecordingState.STARTING, RecordingState.RECORDING):
            self.handle_stop()

    def handle_start(self) -> None:
        if self.app_state.recording_state != RecordingState.IDLE:
            return

        # Callbacks are already on GTK. Reject updates from a previous recording,
        # even if a backend worker was still finishing when it was cancelled.
        session: RecognitionSession | None = None

        def current(callback):
            def receive(*args):
                if session is not None and self._session is session:
                    callback(*args)
            return receive

        session = self._create_session(RecognitionCallbacks(
            ready=current(self._on_ready),
            progress=current(self._on_progress),
            result=current(lambda text: setattr(self.app_state, "transcription_text", text)),
            completed=current(self._complete_transcription),
            failed=current(self._show_notice),
        ))
        if session is None:
            return

        self._cancel_notice_timer()
        self._session = session
        self._set_state(RecordingState.STARTING)
        self.app_state.transcription_text = ""
        self.app_state.error_message = None
        self.app_state.session_status = SessionStatus()
        if self.on_overlay_show:
            self.on_overlay_show()

        try:
            self.audio_capture.start(on_audio_data=session.send_audio)
        except Exception as e:
            logger.error("Audio capture failed: %s", e)
            self._show_notice(SessionFault.MICROPHONE)
            return
        try:
            session.start()
        except Exception:
            logger.exception("Recognition startup failed")
            if self._session is session:
                self._show_notice(SessionFault.RECOGNITION)

    def handle_stop(self) -> None:
        session = self._session
        if session is None or self.app_state.recording_state not in (
            RecordingState.STARTING, RecordingState.RECORDING,
        ):
            return
        self._set_state(RecordingState.STOPPING)
        self.audio_capture.stop()
        try:
            session.finish()
        except Exception:
            logger.exception("Recognition finalization failed")
            if self._session is session:
                self._show_notice(SessionFault.RECOGNITION)

    def _on_ready(self) -> None:
        if self.app_state.recording_state == RecordingState.STARTING:
            self._set_state(RecordingState.RECORDING)

    def _on_progress(self, status: SessionStatus) -> None:
        # Only the terminal failure callback may publish a fault.
        self.app_state.session_status = replace(status, fault=None)

    def _show_notice(self, fault: SessionFault) -> None:
        """Keep interrupted text without pasting it into the focused window."""
        text = self.app_state.transcription_text.strip()
        if self._session is not None and text and self.on_copy:
            self.on_copy(text)
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

    def _cancel_notice_timer(self) -> None:
        if self.notice_timer_id is not None:
            GLib.source_remove(self.notice_timer_id)
            self.notice_timer_id = None

    def _complete_transcription(self) -> None:
        text = self.app_state.transcription_text.strip()
        if not text:
            self._show_notice(SessionFault.NO_TEXT)
            return
        logger.info("Recognition completed (state=%s)", self.app_state.recording_state.value)
        if self.on_paste:
            self.on_paste(text)
        self._reset_to_idle()

    def _release_session(self) -> None:
        # Invalidate callbacks before stopping the resources that emit them.
        session, self._session = self._session, None
        self._cancel_notice_timer()
        self.audio_capture.stop()
        if session is not None:
            try:
                session.close()
            except Exception:
                logger.exception("Recognition cleanup failed")
        self._set_state(RecordingState.IDLE)

    def _reset_to_idle(self) -> None:
        self._release_session()
        self.app_state.session_status = SessionStatus()
        self.app_state.error_message = None
        if self.on_overlay_hide:
            self.on_overlay_hide()
        self.app_state.transcription_text = ""

    def handle_cancel(self) -> None:
        if self._session is not None or self.notice_timer_id is not None:
            logger.info("Recording cancelled (state=%s)", self.app_state.recording_state.value)
            self._reset_to_idle()

    def _set_state(self, new_state: RecordingState) -> None:
        self.app_state.recording_state = new_state
        if self.on_cancel_enabled_changed:
            self.on_cancel_enabled_changed(new_state != RecordingState.IDLE)
