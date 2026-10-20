"""Tests for config module."""

import os
from pathlib import Path

import pytest

from doubao_murmur.config import (
    AUDIO_BLOCKSIZE,
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    AUTH_ERROR_CODE,
    AUTH_ERROR_KEYWORDS,
    RECORDING_MODE_HOLD,
    RECORDING_MODE_TOGGLE,
    WSS_BASE_URL,
    get_config_dir,
    get_params_path,
    get_settings_path,
    load_recording_mode,
    save_recording_mode,
)


def test_wss_url():
    assert WSS_BASE_URL == "wss://ws-samantha.doubao.com/samantha/audio/asr"


def test_audio_params():
    assert AUDIO_SAMPLE_RATE == 16000
    assert AUDIO_CHANNELS == 1
    assert AUDIO_BLOCKSIZE == 1600


def test_auth_error_code():
    assert AUTH_ERROR_CODE == 709599054
    assert "cookie" in AUTH_ERROR_KEYWORDS
    assert "auth" in AUTH_ERROR_KEYWORDS
    assert "session" in AUTH_ERROR_KEYWORDS


def test_get_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config_dir = get_config_dir()
    assert config_dir == tmp_path / "doubao-murmur"
    assert config_dir.exists()


def test_get_params_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = get_params_path()
    assert path == tmp_path / "doubao-murmur" / "asr_params.json"


def test_recording_mode_defaults_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert load_recording_mode() == RECORDING_MODE_TOGGLE

    save_recording_mode(RECORDING_MODE_HOLD)
    assert load_recording_mode() == RECORDING_MODE_HOLD


def test_invalid_recording_mode_falls_back_to_toggle(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    get_settings_path().write_text('{"recording_mode": "unknown"}')

    assert load_recording_mode() == RECORDING_MODE_TOGGLE
