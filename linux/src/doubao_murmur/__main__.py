"""Entry point for Doubao Murmur Linux."""

import logging
import os
import sys

from doubao_murmur.run_logging import configure_logging

logger = logging.getLogger("doubao_murmur")


def main():
    configure_logging()
    logger.info(
        "Application starting (pid=%s, session=%s, GDK backend=%s)",
        os.getpid(),
        os.environ.get("XDG_SESSION_TYPE") or "unknown",
        os.environ.get("GDK_BACKEND") or "default",
    )
    try:
        from doubao_murmur.app import DoubaoMurmurApp

        status = DoubaoMurmurApp().run(sys.argv)
    except Exception:
        logger.exception("Application failed")
        raise
    logger.info("Application exited (status=%s)", status)
    sys.exit(status)


if __name__ == "__main__":
    main()
