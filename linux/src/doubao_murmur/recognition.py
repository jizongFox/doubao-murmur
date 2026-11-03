"""Boundary between microphone capture and a recognition implementation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from doubao_murmur.session_status import SessionFault, SessionStatus


@dataclass(frozen=True)
class RecognitionCallbacks:
    """Notify the controller on the GTK thread, in order.

    Report readiness once when preparation is complete. Progress updates
    carry diagnostics; any fault in a progress snapshot is ignored. Text
    updates replace the entire transcript; emit the last text before
    completing. Report one terminal outcome via completed or failed.
    """

    ready: Callable[[], None]
    progress: Callable[[SessionStatus], None]
    result: Callable[[str], None]
    completed: Callable[[], None]
    failed: Callable[[SessionFault], None]


class RecognitionSession(Protocol):
    """One recording. Lifecycle methods run on GTK and must not block it."""

    def start(self) -> None:
        """Begin preparation. Buffer incoming audio until it can be processed."""
        ...

    def send_audio(self, pcm: bytes) -> None:
        """Accept 16 kHz mono signed PCM16 LE from the microphone thread.

        May arrive before start or during preparation. This call must return
        promptly; do not run inference or I/O here.
        """
        ...

    def finish(self) -> None:
        """End input, even if still preparing, and finish the received audio.

        The implementation decides when the result is final and owns its
        deadlines. A batch implementation may produce all text after this call.
        """
        ...

    def close(self) -> None:
        """Cancel pending work, including preparation, and suppress callbacks.

        Called on every terminal path, including from a terminal callback, and
        may be called before start. Release resources without joining a worker
        on GTK. Closing an already closed session is safe.
        """
        ...


# Construct a fresh session without starting work or emitting callbacks.
# Return None when recording cannot start yet (e.g. login is required).
SessionFactory = Callable[[RecognitionCallbacks], RecognitionSession | None]
