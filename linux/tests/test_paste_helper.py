"""Tests for PasteHelper."""

import subprocess
import threading
from unittest.mock import patch, MagicMock

import pytest

from doubao_murmur.paste.paste_helper import PasteHelper


@pytest.fixture(autouse=True)
def isolate_desktop_backends(monkeypatch):
    """Never inject real keys or query the running compositor in unit tests."""
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    with patch(
        "doubao_murmur.paste.paste_helper.UinputPaster.is_available",
        return_value=False,
    ), patch(
        "doubao_murmur.paste.paste_helper.kwin_active_window_class",
        return_value="",
    ):
        yield


class TestCopyToClipboard:
    def test_wl_copy_preferred(self):
        with patch("shutil.which", return_value="/usr/bin/wl-copy") as mock_which, \
             patch("subprocess.run") as mock_run:
            assert PasteHelper.copy_to_clipboard("hello") is True
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][0] == ["wl-copy"]
            assert args.kwargs["input"] == b"hello"

    def test_xclip_fallback(self):
        def which_side_effect(cmd):
            if cmd == "xclip":
                return "/usr/bin/xclip"
            return None

        with patch("shutil.which", side_effect=which_side_effect), \
             patch("subprocess.run") as mock_run:
            assert PasteHelper.copy_to_clipboard("hello") is True
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][0] == ["xclip", "-selection", "clipboard"]

    def test_empty_text_ignored(self):
        with patch("subprocess.run") as mock_run:
            assert PasteHelper.copy_to_clipboard("") is False
            mock_run.assert_not_called()


class TestSimulatePaste:
    @pytest.mark.parametrize("terminal", [False, True])
    def test_native_x11_uses_xdotool_without_uinput(self, monkeypatch, terminal):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        with patch("shutil.which", return_value="/usr/bin/xdotool"), \
             patch.object(PasteHelper, "_focused_window_is_terminal", return_value=terminal), \
             patch("doubao_murmur.paste.paste_helper.UinputPaster.is_available") as uinput, \
             patch("subprocess.run") as run:
            assert PasteHelper.simulate_paste() is True
            run.assert_called_once_with(
                ["xdotool", "key", "ctrl+shift+v" if terminal else "ctrl+v"],
                check=True, timeout=3,
            )
            uinput.assert_not_called()

    def test_x11_failure_keeps_ydotool_fallback(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        with patch("shutil.which", return_value="/usr/bin/tool"), \
             patch.object(PasteHelper, "_focused_window_is_terminal", return_value=False), \
             patch("subprocess.run", side_effect=[OSError("xdotool failed"), MagicMock()]) as run:
            assert PasteHelper.simulate_paste() is True
            assert [call.args[0][0] for call in run.call_args_list] == ["xdotool", "ydotool"]

    def test_wayland_with_xwayland_keeps_uinput_first(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        with patch.object(PasteHelper, "_focused_window_is_terminal", return_value=False), \
             patch("doubao_murmur.paste.paste_helper.UinputPaster.is_available", return_value=True), \
             patch("doubao_murmur.paste.paste_helper.UinputPaster.paste", return_value=True) as paste, \
             patch("subprocess.run") as run:
            assert PasteHelper.simulate_paste() is True
            paste.assert_called_once_with(use_shift=False)
            run.assert_not_called()

    def test_ydotool_preferred(self):
        with patch("shutil.which", return_value="/usr/bin/ydotool"), \
             patch.object(PasteHelper, "_focused_window_is_terminal",
                          return_value=False), \
             patch("subprocess.run") as mock_run:
            assert PasteHelper.simulate_paste() is True
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][0] == [
                "ydotool", "key", "29:1", "47:1", "47:0", "29:0"
            ]

    def test_ydotool_terminal_uses_ctrl_shift_v(self):
        with patch("shutil.which", return_value="/usr/bin/ydotool"), \
             patch.object(PasteHelper, "_focused_window_is_terminal",
                          return_value=True), \
             patch("subprocess.run") as mock_run:
            assert PasteHelper.simulate_paste() is True
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][0] == [
                "ydotool", "key",
                "29:1", "42:1", "47:1", "47:0", "42:0", "29:0"
            ]

    def test_wtype_fallback(self):
        def which_side_effect(cmd):
            if cmd == "wtype":
                return "/usr/bin/wtype"
            return None

        with patch("shutil.which", side_effect=which_side_effect), \
             patch.object(PasteHelper, "_focused_window_is_terminal",
                          return_value=False), \
             patch("subprocess.run") as mock_run:
            assert PasteHelper.simulate_paste() is True
            mock_run.assert_called_once()

    def test_xdotool_fallback(self):
        def which_side_effect(cmd):
            if cmd == "xdotool":
                return "/usr/bin/xdotool"
            return None

        with patch("shutil.which", side_effect=which_side_effect), \
             patch.object(PasteHelper, "_focused_window_is_terminal",
                          return_value=False), \
             patch("subprocess.run") as mock_run:
            assert PasteHelper.simulate_paste() is True
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][0] == ["xdotool", "key", "ctrl+v"]

    def test_xdotool_terminal_uses_ctrl_shift_v(self):
        def which_side_effect(cmd):
            if cmd == "xdotool":
                return "/usr/bin/xdotool"
            return None

        with patch("shutil.which", side_effect=which_side_effect), \
             patch.object(PasteHelper, "_focused_window_is_terminal",
                          return_value=True), \
             patch("subprocess.run") as mock_run:
            assert PasteHelper.simulate_paste() is True
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][0] == ["xdotool", "key", "ctrl+shift+v"]

    def test_flatpak_spawn_host_fallback(self):
        def which_side_effect(cmd):
            if cmd == "flatpak-spawn":
                return "/usr/bin/flatpak-spawn"
            return None

        with patch("os.path.exists", return_value=True), \
             patch("shutil.which", side_effect=which_side_effect), \
             patch.object(PasteHelper, "_focused_window_is_terminal",
                          return_value=False), \
             patch("subprocess.run") as mock_run:
            assert PasteHelper.simulate_paste() is True
            mock_run.assert_called()
            args = mock_run.call_args
            assert args[0][0] == [
                "flatpak-spawn", "--host", "ydotool", "key",
                "29:1", "47:1", "47:0", "29:0"
            ]


class TestTerminalDetection:
    def _run_detection(self, classname: bytes) -> bool:
        def which_side_effect(cmd):
            if cmd == "xdotool":
                return "/usr/bin/xdotool"
            return None

        mock_result = MagicMock()
        mock_result.stdout = classname
        with patch("shutil.which", side_effect=which_side_effect), \
             patch("subprocess.run", return_value=mock_result) as mock_run:
            result = PasteHelper._focused_window_is_terminal()
            args = mock_run.call_args
            assert args[0][0] == [
                "xdotool", "getactivewindow", "getwindowclassname"
            ]
            return result

    def test_konsole_is_terminal(self):
        assert self._run_detection(b"konsole\n") is True

    def test_browser_is_not_terminal(self):
        assert self._run_detection(b"google-chrome\n") is False

    def test_case_insensitive(self):
        assert self._run_detection(b"Alacritty\n") is True

    def test_warp_is_terminal(self):
        # Warp's WM class is "dev.warp.Warp" (xdotool getwindowclassname).
        assert self._run_detection(b"dev.warp.Warp\n") is True

    def test_no_xdotool_returns_false(self):
        with patch("shutil.which", return_value=None), \
             patch("os.path.exists", return_value=False):
            assert PasteHelper._focused_window_is_terminal() is False


class TestBackendOutcomes:
    @pytest.mark.parametrize("backend", ["available", "no_display", "error"])
    def test_gtk_copy_reports_success_or_failure(self, backend):
        gi = pytest.importorskip("gi")
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk

        display = MagicMock()
        if backend == "error":
            display.get_clipboard.return_value.set.side_effect = RuntimeError("clipboard failed")
        with patch.object(Gdk.Display, "get_default", return_value=(
            None if backend == "no_display" else display
        )):
            assert PasteHelper.copy_with_gtk("transcript") is (backend == "available")
        if backend != "no_display":
            display.get_clipboard.return_value.set.assert_called_once_with("transcript")

    def test_copy_failure_is_explicit_and_does_not_access_gtk(self):
        with patch("shutil.which", return_value=None), \
             patch("os.path.exists", return_value=False), \
             patch.object(PasteHelper, "copy_with_gtk") as gtk:
            assert PasteHelper.copy_to_clipboard("text") is False
            gtk.assert_not_called()

    def test_copy_fallback_order_after_helper_errors(self):
        with patch("shutil.which", return_value="/usr/bin/tool"), \
             patch("subprocess.run", side_effect=[OSError(), OSError(), MagicMock()]) as run:
            assert PasteHelper.copy_to_clipboard("text") is True
            assert [call.args[0] for call in run.call_args_list] == [
                ["wl-copy"], ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
            ]

    def test_no_injection_backend_reports_failure(self):
        with patch("shutil.which", return_value=None), \
             patch("os.path.exists", return_value=False):
            assert PasteHelper.simulate_paste() is False

    def test_cancelled_helpers_do_not_copy_or_inject(self):
        stop = threading.Event()
        stop.set()
        with patch("shutil.which", return_value="/usr/bin/tool"), \
             patch("subprocess.run") as run, \
             patch.object(PasteHelper, "_focused_window_is_terminal") as focus:
            assert PasteHelper.copy_to_clipboard("text", stop) is False
            assert PasteHelper.simulate_paste(stop) is False
            run.assert_not_called()
            focus.assert_not_called()

    def test_shutdown_during_copy_failure_does_not_try_next_backend(self):
        stop = threading.Event()

        def fail(*args, **kwargs):
            stop.set()
            raise OSError("helper failed during shutdown")

        with patch("shutil.which", return_value="/usr/bin/tool"), \
             patch("subprocess.run", side_effect=fail) as run:
            assert PasteHelper.copy_to_clipboard("text", stop) is False
            run.assert_called_once()

    def test_shutdown_during_injection_failure_does_not_try_next_backend(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        stop = threading.Event()

        def fail(*args, **kwargs):
            stop.set()
            raise OSError("helper failed during shutdown")

        with patch("shutil.which", return_value="/usr/bin/tool"), \
             patch.object(PasteHelper, "_focused_window_is_terminal", return_value=False), \
             patch("subprocess.run", side_effect=fail) as run:
            assert PasteHelper.simulate_paste(stop) is False
            run.assert_called_once()
