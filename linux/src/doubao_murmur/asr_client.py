"""WebSocket client for Doubao's streaming ASR service.

Mirrors DoubaoASRClient.swift.

Design decisions:
- `websockets` library (pure Python, asyncio-native) replaces URLSessionWebSocketTask
- Runs on a dedicated asyncio event loop in a background thread to avoid blocking
  GTK's GLib main loop
- Callbacks are invoked on the asyncio thread; callers MUST use GLib.idle_add()
  to marshal back to GTK's main thread
- Audio buffering matches macOS behavior: buffer until connected, then flush
- send_audio uses asyncio.run_coroutine_threadsafe for thread-safe sends
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
import uuid
from typing import Any
from urllib.parse import urlencode

from doubao_murmur.config import (
    AUDIO_SAMPLE_RATE,
    AUTH_ERROR_CODES,
    AUTH_ERROR_KEYWORDS,
    FIXED_QUERY_PARAMS,
    ORIGIN,
    STOP_TRAILING_SILENCE_MS,
    WSS_BASE_URL,
)
from doubao_murmur.params_store import ASRParams

logger = logging.getLogger(__name__)


class ASRServiceError(Exception):
    """The service returned a non-authentication error, not a network failure."""


class ASRClient:
    """WebSocket transport for one recording, including its buffered audio."""

    def __init__(self) -> None:
        self._ws: Any | None = None
        self._connected = False
        self._pending_audio: list[bytes] = []
        self._lock = threading.Lock()
        self._callback_lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._task: asyncio.Task | None = None
        self._disconnect_requested = threading.Event()
        self._terminal_reported = False

        # Callbacks (called from asyncio thread - must use GLib.idle_add for GTK)
        self.on_open = None  # () -> None
        self.on_result = None  # (text: str) -> None
        self.on_finish = None  # () -> None
        self.on_error = None  # (error: Exception | None) -> None
        self.on_auth_error = None  # () -> None
        self.on_response = None  # () -> None, valid results including empty text
        self.on_close = None  # () -> None, peer closed without a finish/error event

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self, params: ASRParams) -> None:
        """Start this recording's connection on a background asyncio loop."""
        with self._lock:
            if self._disconnect_requested.is_set():
                return
            if self._loop is not None:
                raise RuntimeError("Create a new ASRClient for each recording")
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._run_loop, args=(params,), daemon=True
            )
        self._thread.start()

    def _run_loop(self, params: ASRParams) -> None:
        """Own the connection task and release its loop on every exit path."""
        loop = self._loop
        asyncio.set_event_loop(loop)
        self._task = loop.create_task(self._connect_and_listen(params))
        if self._disconnect_requested.is_set():
            self._task.cancel()
        try:
            loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass  # Explicit disconnect, including cancellation during handshake.
        finally:
            with self._lock:
                self._disconnect_requested.set()
                self._connected = False
                self._ws = None
                self._pending_audio.clear()
            # WebSocket keepalives and queued audio sends belong to this loop too.
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
            with self._lock:
                loop.close()
            asyncio.set_event_loop(None)

    def _cancel_connection(self) -> None:
        """Runs on the worker loop; disconnect() never blocks the GTK thread."""
        if self._task and not self._task.done():
            self._task.cancel()

    async def _connect_and_listen(self, params: ASRParams) -> None:
        """Build URL, connect, receive messages."""
        url = self._build_url(params)
        headers = {
            "Cookie": params.cookie_header,
            "Origin": ORIGIN,
        }

        logger.info("Connecting to ASR WebSocket...")
        try:
            websockets = _load_websockets()
            header_kwargs = _websocket_header_kwargs(
                websockets.connect, headers
            )
            connect_kwargs = dict(
                open_timeout=5,
                close_timeout=3,
                max_size=2**20,
                **header_kwargs,
            )
            # Race IPv4/IPv6 (happy eyeballs). Without this, a blackholed
            # IPv6 path (common on mobile hotspots) stalls the connect for
            # seconds before falling back to IPv4. Older websockets/Python
            # don't forward the kwarg, hence the TypeError fallback.
            try:
                ws = await websockets.connect(
                    url, happy_eyeballs_delay=0.25, **connect_kwargs
                )
            except TypeError:
                ws = await websockets.connect(url, **connect_kwargs)
            try:
                with self._lock:
                    if self._disconnect_requested.is_set():
                        return
                    self._ws = ws
                    self._connected = True
                logger.info("Connected")

                # Flush buffered audio
                self._flush_audio_buffer()

                # disconnect() can race with the post-handshake setup above.
                # Do not announce a connection that was cancelled meanwhile.
                with self._callback_lock:
                    if self._disconnect_requested.is_set():
                        return
                    if self.on_open:
                        self.on_open()

                # Receive loop
                received_any = False
                async for message in ws:
                    if self._disconnect_requested.is_set():
                        return
                    received_any = True
                    self._handle_message(message)
                    if self._terminal_reported:
                        break

                # An expired login passes the handshake and is then closed by
                # the server (1000) before it sends anything. A valid login
                # gets a result for every audio frame long before the server
                # ends the session, and disconnect() clears self._ws before
                # closing, so our own closes never land here.
                if (
                    not received_any
                    and not self._terminal_reported
                    and not self._disconnect_requested.is_set()
                    and self._ws is ws
                    and getattr(ws, "close_code", None) == 1000
                ):
                    logger.warning(
                        "Server closed the connection before sending "
                        "anything: login expired"
                    )
                    self._report_terminal(self.on_auth_error)
                elif not self._terminal_reported:
                    self._report_terminal(self.on_close)
            finally:
                await ws.close()

        except Exception as e:
            if self._disconnect_requested.is_set():
                return
            logger.warning("ASR transport error: %s", e)
            self._report_terminal(self.on_error, e)

    def _report_terminal(self, callback, *args) -> None:
        """Send/receive failures race; only one terminal outcome may escape."""
        with self._callback_lock:
            if self._disconnect_requested.is_set() or self._terminal_reported:
                return
            self._terminal_reported = True
            self._connected = False
            if callback:
                callback(*args)

    async def _send(self, ws, data: bytes) -> None:
        """Observe send failures instead of abandoning their Future exceptions."""
        if self._disconnect_requested.is_set() or self._terminal_reported:
            return
        try:
            await ws.send(data)
        except Exception as error:
            self._report_terminal(self.on_error, error)
            self._cancel_connection()

    def _build_url(self, params: ASRParams) -> str:
        """Construct the full WSS URL with query parameters."""
        query = dict(FIXED_QUERY_PARAMS)
        query["device_id"] = params.device_id
        query["web_id"] = params.web_id
        query["tea_uuid"] = params.web_id
        query["web_tab_id"] = str(uuid.uuid4())
        return f"{WSS_BASE_URL}?{urlencode(query)}"

    def send_audio(self, data: bytes) -> None:
        """Send PCM audio data. Thread-safe. Buffers if not connected."""
        with self._lock:
            if self._disconnect_requested.is_set():
                return
            if self._connected and self._ws:
                if self._loop and self._loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._send(self._ws, data), self._loop
                    )
            else:
                self._pending_audio.append(data)

    def finish_sending(
        self, trailing_silence_ms: int = STOP_TRAILING_SILENCE_MS
    ) -> None:
        """Signal no more mic audio; keep WS open for the final results.

        Flushes a short tail of digital silence first. The service emits exactly
        one result per audio message it receives and withholds the last word until
        further audio arrives, so a stream that simply stops never gets its tail
        transcribed. The padding is queued at once rather than paced in real time,
        so it costs no extra wall-clock time.
        """
        chunk_ms = 100
        silence = bytes(AUDIO_SAMPLE_RATE * 2 * chunk_ms // 1000)
        silence_chunks = [
            silence
            for _ in range(max(1, trailing_silence_ms // chunk_ms))
        ] if trailing_silence_ms > 0 else []

        with self._lock:
            if self._disconnect_requested.is_set():
                return
            ws = self._ws
            was_connected = self._connected
            self._connected = False

            # If the user stops before the handshake completes, keep the
            # captured speech and queue the same tail used by the connected
            # path. _flush_audio_buffer() will send both in order on connect.
            if not was_connected:
                self._pending_audio.extend(silence_chunks)

            if was_connected and ws:
                if self._loop and self._loop.is_running():
                    for chunk in silence_chunks:
                        asyncio.run_coroutine_threadsafe(self._send(ws, chunk), self._loop)

        logger.info(
            "Finished sending audio (+%dms silence), waiting for final results",
            trailing_silence_ms,
        )

    def disconnect(self) -> None:
        """Cancel the connection, even if its handshake hasn't finished yet."""
        with self._callback_lock:
            with self._lock:
                if self._disconnect_requested.is_set():
                    return
                self._disconnect_requested.set()
                self._connected = False
                self._pending_audio.clear()
                # Clear first so the receive loop can distinguish our close.
                self._ws = None
                if self._loop and not self._loop.is_closed():
                    # Queue cancellation even if the worker hasn't started its loop.
                    self._loop.call_soon_threadsafe(self._cancel_connection)
        logger.info("Disconnected")

    def _flush_audio_buffer(self) -> None:
        with self._lock:
            buffered = list(self._pending_audio)
            self._pending_audio.clear()
        if buffered:
            logger.info("Flushing %d buffered audio chunks", len(buffered))
            for chunk in buffered:
                if self._ws and self._loop and self._loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._send(self._ws, chunk), self._loop
                    )

    def _handle_message(self, message) -> None:
        """Parse JSON message from server."""
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="replace")

        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict) or self._terminal_reported:
            return

        code = data.get("code", 0)
        event = data.get("event", "")
        msg = data.get("message", "")

        # Detect auth errors
        if code != 0 and _is_auth_error(code, msg):
            logger.warning("Auth error detected: code=%s, message=%s", code, msg)
            self._report_terminal(self.on_auth_error)
            return

        if code != 0:
            self._report_terminal(self.on_error, ASRServiceError(f"ASR code {code}"))
            return

        if event == "result":
            result = data.get("result") or {}
            if not isinstance(result, dict):
                return
            text = result.get("Text", "")
            if not isinstance(text, str):
                return
            if self.on_response:
                self.on_response()
            if text and self.on_result:
                self.on_result(text)
        elif event == "finish":
            logger.info("Received finish event")
            if self.on_response:
                self.on_response()
            self._report_terminal(self.on_finish)


def _load_websockets():
    try:
        import websockets
    except Exception as e:
        raise RuntimeError(
            "websockets is not available; install the Python websockets "
            "package before recording"
        ) from e
    return websockets


def _websocket_header_kwargs(connect_func, headers: dict[str, str]) -> dict:
    """Return the correct header kwarg for installed websockets version."""
    try:
        params = inspect.signature(connect_func).parameters
    except (TypeError, ValueError):
        return {"additional_headers": headers}
    if "additional_headers" in params:
        return {"additional_headers": headers}
    return {"extra_headers": headers}


def _is_auth_error(code, message) -> bool:
    lower_msg = str(message or "").lower()
    return code in AUTH_ERROR_CODES or any(
        kw in lower_msg for kw in AUTH_ERROR_KEYWORDS
    )


def is_handshake_auth_rejection(error) -> bool:
    """True if the server refused the WebSocket handshake over the login.

    A plain HTTP 401/403 counts. The ASR endpoint itself answers a cookie that
    is not a login at all with HTTP 200 and a JSON body such as
    {"code": 710012001, "message": "login invalid"}. websockets>=14 raises
    InvalidStatus, whose .response carries that body; older releases raise
    InvalidStatusCode with only .status_code, so there the 200 case goes
    undetected. Anything else -- timeouts, DNS failures, connection resets --
    is a network problem, not an auth problem.
    """
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(response, "status_code", None)
    if status in (401, 403):
        return True
    body = getattr(response, "body", None)
    if status != 200 or not body:
        return False
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    code = data.get("code", 0)
    return code != 0 and _is_auth_error(code, data.get("message"))
