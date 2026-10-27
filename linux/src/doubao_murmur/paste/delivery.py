"""One ordered text-delivery workflow, without blocking the GTK main loop."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import CancelledError, Future
from enum import Enum

from gi.repository import GLib

from doubao_murmur.config import PASTE_DELAY
from doubao_murmur.paste.paste_helper import PasteHelper

logger = logging.getLogger(__name__)


class DeliveryOutcome(Enum):
    # Injection success cannot establish whether the focused app accepted paste.
    PASTE_REQUESTED = "paste_requested"
    COPIED_ONLY = "copied_only"
    FAILED = "failed"


class TextDelivery:
    """Own copy-before-paste ordering, fallback, and delivery shutdown.

    submit/close and on_result run on GTK. One worker processes entire requests
    serially, including GTK fallback, so the next copy cannot overtake a paste.
    close discards queued work and cancels waits without joining on GTK. An
    already-running OS operation cannot be retracted; it keeps its existing
    timeout. Cancellation is checked between stages and helper attempts.
    """

    def __init__(self, on_result: Callable[[DeliveryOutcome], None]) -> None:
        self._on_result = on_result
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._closed = threading.Event()
        self._gtk_lock = threading.Lock()
        self._gtk_copy: Future[bool] | None = None

    def submit(self, text: str) -> bool:
        """Accept nonempty text immediately; outcomes arrive later on GTK."""
        if not text or self._closed.is_set():
            return False
        self._queue.put(text)
        if self._worker is None:
            self._worker = threading.Thread(
                target=self._run, name="text-delivery", daemon=True,
            )
            self._worker.start()
        return True

    def close(self) -> None:
        """Cancel pending delivery without waiting for subprocesses or GTK."""
        with self._gtk_lock:
            if self._closed.is_set():
                return
            self._closed.set()
            if self._gtk_copy is not None:
                self._gtk_copy.cancel()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        if self._worker is not None:
            self._queue.put(None)

    def _run(self) -> None:
        while True:
            text = self._queue.get()
            if text is None or self._closed.is_set():
                return
            outcome = self._deliver(text)
            if not self._closed.is_set():
                GLib.idle_add(self._notify, outcome)

    def _deliver(self, text: str) -> DeliveryOutcome:
        copied = False
        try:
            copied = PasteHelper.copy_to_clipboard(text, self._closed)
            if not copied and not self._closed.is_set():
                copied = self._copy_on_gtk(text)
            if not copied:
                return DeliveryOutcome.FAILED
            # Event.wait makes the usual copy/paste delay interruptible on quit.
            if self._closed.wait(PASTE_DELAY):
                return DeliveryOutcome.COPIED_ONLY
            if PasteHelper.simulate_paste(self._closed):
                return DeliveryOutcome.PASTE_REQUESTED
            return DeliveryOutcome.COPIED_ONLY
        except Exception:
            logger.exception("Text delivery failed")
            return DeliveryOutcome.COPIED_ONLY if copied else DeliveryOutcome.FAILED

    def _copy_on_gtk(self, text: str) -> bool:
        result: Future[bool] = Future()
        with self._gtk_lock:
            if self._closed.is_set():
                return False
            self._gtk_copy = result
        GLib.idle_add(self._gtk_clipboard_copy, text, result)
        try:
            return result.result()  # Only the worker waits, never GTK.
        except CancelledError:
            return False
        finally:
            with self._gtk_lock:
                self._gtk_copy = None

    def _gtk_clipboard_copy(self, text: str, result: Future[bool]) -> bool:
        if not self._closed.is_set() and not result.done():
            try:
                copied = PasteHelper.copy_with_gtk(text)
            except Exception:
                logger.exception("GTK clipboard copy failed")
                copied = False
            if not result.done():
                result.set_result(copied)
        return GLib.SOURCE_REMOVE

    def _notify(self, outcome: DeliveryOutcome) -> bool:
        if not self._closed.is_set():
            self._on_result(outcome)
        return GLib.SOURCE_REMOVE
