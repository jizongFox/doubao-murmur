"""Tests for overlay position persistence and reset."""

import pytest
from unittest.mock import Mock

pytest.importorskip("gi")

from doubao_murmur.ui import overlay as overlay_module
from doubao_murmur.ui.overlay import Overlay
from doubao_murmur.app_state import AppState, RecordingState
from doubao_murmur.session_status import ConnectionPhase, SessionFault, SessionStatus
from doubao_murmur.ui.overlay import diagnostic_text


def _overlay_at(x, y):
    overlay = Overlay.__new__(Overlay)
    overlay._saved_x = x
    overlay._saved_y = y
    overlay._window = None
    return overlay


def test_show_keeps_saved_x11_position(monkeypatch):
    overlay = _overlay_at(4711, 2950)
    overlay._window = object()
    overlay._anim_timer = 1
    overlay._update_content = lambda: None
    presented = []

    monkeypatch.setattr(overlay_module, "using_layer_shell", lambda: False)
    monkeypatch.setattr(
        overlay_module,
        "present_overlay",
        lambda _window, _role, x=None, y=None: presented.append((x, y)),
    )

    overlay.show()

    assert presented == [(4711, 2950)]


def test_reset_position_removes_saved_file(tmp_path, monkeypatch):
    position_file = tmp_path / "overlay.json"
    position_file.write_text('{"x": 5000, "y": 5000}')
    monkeypatch.setattr(
        overlay_module, "get_overlay_config_path", lambda: position_file
    )
    overlay = _overlay_at(5000, 5000)

    overlay.reset_position()

    assert overlay._saved_x is None
    assert overlay._saved_y is None
    assert not position_file.exists()


@pytest.mark.parametrize("phase, expected", [
    (ConnectionPhase.PARAMETERS, "正在获取连接参数 · 已等待 3 秒"),
    (ConnectionPhase.CONNECTING, "正在连接识别服务 · 已等待 3 秒"),
    (ConnectionPhase.CONNECTED, "已连接 · 等待首个服务响应 3 秒"),
])
def test_diagnostics_show_actual_stage_and_elapsed_time(phase, expected):
    assert diagnostic_text(SessionStatus(phase=phase, phase_since=10), 13.9) == expected


def test_response_age_does_not_claim_network_quality_or_missing_speech():
    status = SessionStatus(
        phase=ConnectionPhase.CONNECTED, phase_since=10, last_response_at=11
    )
    assert diagnostic_text(status, 30) == "已连接 · 最近服务响应 19 秒前"


@pytest.mark.parametrize("fault", list(SessionFault))
def test_every_fault_has_a_user_facing_diagnostic(fault):
    assert diagnostic_text(SessionStatus(fault=fault), 0)


def test_state_changes_refresh_overlay_without_forwarding_callbacks(monkeypatch):
    monkeypatch.setattr(Overlay, "_load_position", lambda self: None)
    state = AppState()
    overlay = Overlay(state)
    overlay._label = Mock()
    overlay._diagnostics = Mock()

    state.recording_state = RecordingState.RECORDING
    overlay._label.set_text.assert_called_with("正在聆听...")
    state.transcription_text = "hello"
    overlay._label.set_text.assert_called_with("hello")
    state.session_status = SessionStatus(
        phase=ConnectionPhase.CLOSED, fault=SessionFault.DISCONNECTED
    )
    overlay._label.set_text.assert_called_with("hello")
    overlay._diagnostics.set_text.assert_called_with("网络 / 识别：识别连接已中断，请重试")
    overlay._diagnostics.add_css_class.assert_called_with("error")
    state.error_message = "login error"
    overlay._label.set_text.assert_called_with("login error")


def test_diagnostic_clock_refresh_uses_existing_visible_overlay_timer(monkeypatch):
    monkeypatch.setattr(Overlay, "_load_position", lambda self: None)
    monkeypatch.setattr(overlay_module.time, "monotonic", lambda: 15)
    state = AppState()
    state.session_status = SessionStatus(phase=ConnectionPhase.CONNECTING, phase_since=10)
    overlay = Overlay(state)
    overlay._indicator = Mock()
    overlay._diagnostics = Mock()
    assert overlay._tick_indicator() == overlay_module.GLib.SOURCE_CONTINUE
    overlay._diagnostics.set_text.assert_called_with("网络 / 识别：正在连接识别服务 · 已等待 5 秒")
