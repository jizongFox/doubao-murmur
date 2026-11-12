"""Persist ASR params (cookies, device_id, web_id) to a JSON file.

Mirrors ASRParamsStore.swift.
Location: $XDG_CONFIG_HOME/doubao-murmur/asr_params.json
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass

from doubao_murmur.config import get_params_path

logger = logging.getLogger(__name__)


@dataclass
class ASRParams:
    """Parameters needed to establish a WSS ASR connection."""

    cookies: dict[str, str]
    device_id: str
    web_id: str

    @property
    def cookie_header(self) -> str:
        """Build the Cookie header string for HTTP/WSS requests."""
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


class ParamsStore:
    """Persist ASR params to JSON file."""

    @staticmethod
    def save(params: ASRParams) -> bool:
        """Persist credentials, returning whether the write succeeded."""
        try:
            data = json.dumps(asdict(params), ensure_ascii=False, indent=2)
            get_params_path().write_text(data, encoding="utf-8")
            logger.info("Saved ASR params to %s", get_params_path())
            return True
        except Exception as e:
            logger.error("Failed to save params: %s", e)
            return False

    @staticmethod
    def load() -> ASRParams | None:
        try:
            data = json.loads(get_params_path().read_text(encoding="utf-8"))
            params = ASRParams(**data)
            if (
                not isinstance(params.cookies, dict)
                or not all(isinstance(value, str) for value in params.cookies.values())
                or not isinstance(params.device_id, str)
                or not isinstance(params.web_id, str)
            ):
                raise ValueError("Invalid ASR parameter types")
            return params
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.error("Failed to load ASR params: %s", type(e).__name__)
            return None

    @staticmethod
    def clear() -> None:
        try:
            get_params_path().unlink(missing_ok=True)
            logger.info("Cleared saved params")
        except Exception as e:
            logger.error("Failed to clear params: %s", e)
