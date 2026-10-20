"""Constants and configuration for Doubao Murmur Linux port.

Mirrors the fixed parameters from the macOS DoubaoASRClient.swift.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# --- Doubao ASR WebSocket ---

WSS_BASE_URL = "wss://ws-samantha.doubao.com/samantha/audio/asr"

FIXED_QUERY_PARAMS = {
    "version_code": "20800",
    "language": "zh",
    "device_platform": "web",
    "aid": "497858",
    "real_aid": "497858",
    "pkg_type": "release_version",
    "pc_version": "3.12.3",
    "region": "",
    "sys_region": "",
    "samantha_web": "1",
    "use-olympus-account": "1",
    "format": "pcm",
}

ORIGIN = "https://www.doubao.com"
LOGIN_URL = "https://www.doubao.com/chat"

# --- Audio capture ---

AUDIO_SAMPLE_RATE = 16000
AUDIO_CHANNELS = 1
AUDIO_DTYPE = "int16"
AUDIO_BLOCKSIZE = 1600  # samples per callback (100ms at 16kHz)

# --- Auth error detection ---

AUTH_ERROR_CODE = 709599054
AUTH_ERROR_KEYWORDS = [
    "cookie", "auth", "login", "session", "unauthorized", "expired",
]

# --- Paths ---

CONFIG_DIR_NAME = "doubao-murmur"
PARAMS_FILE = "asr_params.json"
KEYBOARD_FILE = "keyboard.json"
OVERLAY_FILE = "overlay.json"
SETTINGS_FILE = "settings.json"

RECORDING_MODE_TOGGLE = "toggle"
RECORDING_MODE_HOLD = "hold"
RECORDING_MODES = {RECORDING_MODE_TOGGLE, RECORDING_MODE_HOLD}


def get_config_dir() -> Path:
    """Get the XDG config directory for the app."""
    config_home = os.environ.get(
        "XDG_CONFIG_HOME", str(Path.home() / ".config")
    )
    app_dir = Path(config_home) / CONFIG_DIR_NAME
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


def get_params_path() -> Path:
    """Get the path to the ASR params JSON file."""
    return get_config_dir() / PARAMS_FILE


def get_keyboard_config_path() -> Path:
    """Get the path to the on-screen keyboard geometry JSON file."""
    return get_config_dir() / KEYBOARD_FILE


def get_overlay_config_path() -> Path:
    """Get the path to the overlay window position JSON file."""
    return get_config_dir() / OVERLAY_FILE


def get_settings_path() -> Path:
    """Get the path to user-facing application settings."""
    return get_config_dir() / SETTINGS_FILE


def load_recording_mode() -> str:
    """Load the recording mode, preserving toggle as the default."""
    path = get_settings_path()
    if not path.exists():
        return RECORDING_MODE_TOGGLE
    try:
        mode = json.loads(path.read_text(encoding="utf-8")).get(
            "recording_mode"
        )
        if mode in RECORDING_MODES:
            return mode
    except (OSError, ValueError, AttributeError) as e:
        logger.warning("Could not load settings: %s", e)
    return RECORDING_MODE_TOGGLE


def save_recording_mode(mode: str) -> None:
    """Persist the selected recording mode."""
    if mode not in RECORDING_MODES:
        raise ValueError(f"Unknown recording mode: {mode}")
    data = json.dumps({"recording_mode": mode}, indent=2)
    get_settings_path().write_text(data, encoding="utf-8")


# --- Timeouts ---

STOP_SAFETY_TIMEOUT = 1.5  # seconds; upper bound on waiting for final results
# Trailing digital silence flushed when recording stops. The service emits one
# result per audio message and withholds the last word until more audio arrives,
# so without this padding the tail of the utterance is never transcribed.
# Measured: losing ~150ms of tail audio costs the final two characters, and
# ~100ms of silence recovers them; 200ms leaves margin.
STOP_TRAILING_SILENCE_MS = 200
# The result stream must stay quiet this long before the transcript is accepted.
# After the audio ends the server replays pending partials before sending the
# one carrying the final word; that gap measures ~150ms.
FINAL_RESULT_QUIET_PERIOD = 0.25  # seconds
DEBOUNCE_INTERVAL = 0.3  # seconds
PASTE_DELAY = 0.05  # seconds between copy and paste simulation
AUTH_EXPIRY_DELAY = 2.0  # seconds before resetting after auth error

# --- Overlay UI ---

OVERLAY_WIDTH = 760
OVERLAY_HEIGHT = 88
# Transcription text wraps within this many "characters" (Pango's average
# char-width unit ~= the old 760px text column) and the overlay grows in
# height up to OVERLAY_MAX_LINES before the oldest words scroll off.
OVERLAY_TEXT_CHARS = 88
OVERLAY_MAX_LINES = 5
PTT_BUTTON_SIZE = 60

# --- User-Agent for WebView ---

WEBVIEW_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
