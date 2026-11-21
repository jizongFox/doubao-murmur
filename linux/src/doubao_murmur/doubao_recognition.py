"""Doubao credential, transport, and finalization policy for one recording."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import replace

from gi.repository import GLib

from doubao_murmur.asr_client import (
    ASRClient, ASRResponseTimeout, ASRServiceError, is_handshake_auth_rejection,
)
from doubao_murmur.params_store import ASRParams, ParamsStore
from doubao_murmur.recognition import RecognitionCallbacks
from doubao_murmur.session_status import ConnectionPhase, SessionFault, SessionStatus

logger = logging.getLogger(__name__)

SESSION_START_TIMEOUT = 8.0  # parameter extraction and handshake share this budget
STOP_SAFETY_TIMEOUT = 1.5  # final-result budget once both connected and stopped
FINAL_RESULT_QUIET_PERIOD = 0.25  # allow the last correction to arrive (~150 ms)


class DoubaoRecognitionSession:
    """Translate Doubao-specific outcomes into the recognition contract."""

    def __init__(
        self,
        callbacks: RecognitionCallbacks,
        *,
        cached_params: ASRParams | None,
        extract_params: Callable[[Callable[[ASRParams | None], None]], None],
        save_params: Callable[[ASRParams], bool],
        on_auth_expired: Callable[[], None],
    ) -> None:
        self._callbacks = callbacks
        self._cached_params = cached_params
        self._extract_params = extract_params
        self._save_params = save_params
        self._on_auth_expired = on_auth_expired
        self._status = SessionStatus()
        self._input_ended = False
        self._closed = False
        self._start_timer: int | None = None
        self._stop_timer: int | None = None
        self._quiet_timer: int | None = None

        self._client = ASRClient()
        self._client.on_open = self._on_main(self._on_open)
        self._client.on_response = self._on_main(self._on_response)
        self._client.on_result = self._on_main(self._on_result)
        self._client.on_finish = self._on_main(callbacks.completed)
        self._client.on_close = self._on_main(self._on_peer_close)
        self._client.on_error = self._on_main(self._on_error)
        self._client.on_auth_error = self._on_main(self._auth_failed)

    def _on_main(self, callback):
        def dispatch(*args):
            if not self._closed:
                callback(*args)
            return GLib.SOURCE_REMOVE

        return lambda *args: GLib.idle_add(dispatch, *args)

    def start(self) -> None:
        self._set_phase(ConnectionPhase.PARAMETERS)
        self._start_timer = GLib.timeout_add(
            int(SESSION_START_TIMEOUT * 1000), self._start_timeout
        )
        if self._cached_params is not None:
            self._connect(self._cached_params)
        else:
            self._extract_params(self._on_params)

    def _on_params(self, params: ASRParams | None) -> None:
        # WebView may complete after stop, but never accept a cancelled request.
        if self._closed:
            return
        if params is None:
            self._callbacks.failed(SessionFault.PARAMETERS)
        elif not self._save_params(params):
            self._callbacks.failed(SessionFault.CREDENTIAL_SAVE)
        else:
            self._connect(params)

    def _connect(self, params: ASRParams) -> None:
        self._set_phase(ConnectionPhase.CONNECTING)
        self._client.connect(params)

    def _set_phase(self, phase: ConnectionPhase) -> None:
        self._status = replace(self._status, phase=phase, phase_since=time.monotonic())
        self._callbacks.progress(self._status)

    def send_audio(self, pcm: bytes) -> None:
        self._client.send_audio(pcm)

    def finish(self) -> None:
        self._input_ended = True
        # The transport appends trailing silence, even before the socket opens.
        self._client.finish_sending()
        if self._status.phase == ConnectionPhase.CONNECTED:
            self._schedule_stop_timeout()

    def _on_open(self) -> None:
        if self._status.phase == ConnectionPhase.CONNECTED:
            return
        if self._start_timer is not None:
            GLib.source_remove(self._start_timer)
            self._start_timer = None
        self._set_phase(ConnectionPhase.CONNECTED)
        self._callbacks.ready()
        if self._input_ended and self._stop_timer is None:
            self._schedule_stop_timeout()

    def _on_response(self) -> None:
        self._status = replace(self._status, last_response_at=time.monotonic())
        self._callbacks.progress(self._status)

    def _on_result(self, text: str) -> None:
        self._callbacks.result(text)
        self._on_open()
        if self._input_ended:
            # Doubao can replay intermediate results after stop. The last
            # correction wins; an empty response never restarts this timer.
            if self._quiet_timer is not None:
                GLib.source_remove(self._quiet_timer)
            self._quiet_timer = GLib.timeout_add(
                int(FINAL_RESULT_QUIET_PERIOD * 1000), self._finish_after_quiet
            )

    def _schedule_stop_timeout(self) -> None:
        self._stop_timer = GLib.timeout_add(
            int(STOP_SAFETY_TIMEOUT * 1000), self._finish_after_timeout
        )

    def _finish_after_quiet(self) -> bool:
        self._quiet_timer = None
        self._callbacks.completed()
        return GLib.SOURCE_REMOVE

    def _finish_after_timeout(self) -> bool:
        self._stop_timer = None
        logger.info("ASR final result timed out after stop")
        self._callbacks.completed()
        return GLib.SOURCE_REMOVE

    def _start_timeout(self) -> bool:
        self._start_timer = None
        fault = (
            SessionFault.PARAMETER_TIMEOUT
            if self._status.phase == ConnectionPhase.PARAMETERS
            else SessionFault.CONNECTION_TIMEOUT
        )
        self._callbacks.failed(fault)
        return GLib.SOURCE_REMOVE

    def _on_peer_close(self) -> None:
        if self._input_ended:
            self._callbacks.completed()
        else:
            self._callbacks.failed(SessionFault.DISCONNECTED)

    def _on_error(self, error: Exception) -> None:
        logger.error("ASR error: %s", type(error).__name__)
        if self._cached_params is not None and is_handshake_auth_rejection(error):
            self._auth_failed()
            return
        if isinstance(error, ASRServiceError):
            fault = SessionFault.SERVICE
        elif isinstance(error, ASRResponseTimeout):
            fault = SessionFault.RESPONSE_TIMEOUT
        elif isinstance(error, (TimeoutError, asyncio.TimeoutError)):
            fault = SessionFault.CONNECTION_TIMEOUT
        elif self._status.phase == ConnectionPhase.CONNECTED:
            fault = SessionFault.DISCONNECTED
        else:
            fault = SessionFault.CONNECTION_FAILED
        self._callbacks.failed(fault)

    def _auth_failed(self) -> None:
        ParamsStore.clear()
        self._callbacks.failed(SessionFault.AUTH)
        self._on_auth_expired()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for timer in (self._start_timer, self._stop_timer, self._quiet_timer):
            if timer is not None:
                GLib.source_remove(timer)
        self._client.disconnect()
