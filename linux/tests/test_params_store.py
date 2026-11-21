"""Tests for ParamsStore."""

import json

import pytest

# Patch config before importing params_store
import doubao_murmur.config as config


@pytest.fixture(autouse=True)
def tmp_config_dir(tmp_path, monkeypatch):
    """Redirect config dir to a temp directory for each test."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def test_save_and_load():
    from doubao_murmur.params_store import ASRParams, ParamsStore

    params = ASRParams(
        cookies={"sessionid": "abc123", "sid_tt": "xyz789"},
        device_id="dev_001",
        web_id="web_002",
    )
    assert ParamsStore.save(params) is True

    loaded = ParamsStore.load()
    assert loaded is not None
    assert loaded.cookies == params.cookies
    assert loaded.device_id == "dev_001"
    assert loaded.web_id == "web_002"


def test_cookie_header():
    from doubao_murmur.params_store import ASRParams

    params = ASRParams(
        cookies={"a": "1", "b": "2"},
        device_id="d",
        web_id="w",
    )
    header = params.cookie_header
    assert "a=1" in header
    assert "b=2" in header
    assert "; " in header


def test_load_nonexistent():
    from doubao_murmur.params_store import ParamsStore

    assert ParamsStore.load() is None


def test_clear():
    from doubao_murmur.params_store import ASRParams, ParamsStore

    params = ASRParams(cookies={"a": "1"}, device_id="d", web_id="w")
    ParamsStore.save(params)
    assert ParamsStore.load() == params

    ParamsStore.clear()
    assert ParamsStore.load() is None


@pytest.mark.parametrize("contents", [
    "{",
    json.dumps({"cookies": [], "device_id": "d", "web_id": "w"}),
    json.dumps({"cookies": {"sessionid": None}, "device_id": "d", "web_id": "w"}),
    json.dumps({"cookies": {}, "device_id": 1, "web_id": "w"}),
])
def test_load_invalid_cache(contents):
    from doubao_murmur.params_store import ParamsStore

    config.get_params_path().write_text(contents, encoding="utf-8")
    assert ParamsStore.load() is None
