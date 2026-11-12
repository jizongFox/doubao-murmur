"""Set up terminal output and a private log for each Linux app launch."""

import logging
import os
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from doubao_murmur.config import CONFIG_DIR_NAME

_FORMAT = "[%(asctime)s] %(name)s %(levelname)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S %z"
logger = logging.getLogger(__name__)


class _RunLogHandler(TimedRotatingFileHandler):
    def _open(self):
        # Apply the same permissions when a fresh file opens after rollover.
        return open(
            self.baseFilename, self.mode, encoding=self.encoding, errors=self.errors,
            opener=lambda path, flags: os.open(path, flags, 0o600),
        )


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=_FORMAT, datefmt=_DATE_FORMAT)
    state_home = Path(os.environ.get("XDG_STATE_HOME", ""))
    if not state_home.is_absolute():
        state_home = Path.home() / ".local" / "state"
    log_dir = state_home / CONFIG_DIR_NAME / "logs"
    started = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = log_dir / f"run-{started}-{os.getpid()}.log"
    try:
        log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        handler = _RunLogHandler(
            path, when="D", interval=2, utc=True, encoding="utf-8",
            errors="backslashreplace",
        )
    except OSError as error:
        logger.warning("Could not create run log: %s", error)
        return

    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))
    logging.getLogger("doubao_murmur").addHandler(handler)
