"""UI-independent diagnostics for one voice session.

The transcription manager publishes immutable snapshots on the main thread.
Times use a monotonic clock; absent responses do not imply a broken network.
Recording controls remain governed by RecordingState, not these diagnostics.
"""

from dataclasses import dataclass
from enum import Enum


class ConnectionPhase(Enum):
    IDLE = "idle"
    PARAMETERS = "parameters"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    CLOSED = "closed"


class SessionFault(Enum):
    MICROPHONE = "microphone"
    PARAMETERS = "parameters"
    CREDENTIAL_SAVE = "credential_save"
    PARAMETER_TIMEOUT = "parameter_timeout"
    CONNECTION_TIMEOUT = "connection_timeout"
    CONNECTION_FAILED = "connection_failed"
    DISCONNECTED = "disconnected"
    SERVICE = "service"
    AUTH = "auth"
    NO_TEXT = "no_text"


@dataclass(frozen=True)
class SessionStatus:
    phase: ConnectionPhase = ConnectionPhase.IDLE
    phase_since: float = 0.0
    last_response_at: float | None = None
    fault: SessionFault | None = None
