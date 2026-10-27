"""Desktop operations used by TextDelivery.

CLI operations block and belong on the delivery worker. Only copy_with_gtk
touches GTK and must run on its main thread. Successful key injection means
the helper accepted the request, not that the target application pasted it.

Clipboard priority: wl-copy, xclip, xsel, then GTK (via TextDelivery).
Injection priority: native X11 tries xdotool first, then uinput/ydotool/wtype;
Wayland tries uinput/ydotool/wtype first, then xdotool for XWayland windows.
"""

from __future__ import annotations

import logging
import subprocess
from threading import Event

from doubao_murmur.host_tools import command_candidates
from doubao_murmur.desktop import is_x11_session
from doubao_murmur.paste.kwin_window import active_window_class as kwin_active_window_class
from doubao_murmur.paste.uinput_injector import UinputPaster

logger = logging.getLogger(__name__)

# Terminal emulators interpret Ctrl+V as a control sequence; their paste
# shortcut is Ctrl+Shift+V instead. Matched against the focused window's
# WM class (lowercased).
_TERMINAL_WM_CLASSES = {
    "konsole",
    "yakuake",
    "alacritty",
    "kitty",
    "foot",
    "wezterm",
    "org.wezfurlong.wezterm",
    "gnome-terminal-server",
    "xterm",
    "urxvt",
    "st",
    "terminator",
    "tilix",
    "xfce4-terminal",
    "lxterminal",
    "deepin-terminal",
    "qterminal",
    "io.elementary.terminal",
    "ghostty",
    "com.mitchellh.ghostty",
    "warp",
    "warp-terminal",
    "dev.warp.warp",
}


class PasteHelper:
    """Clipboard and injection backends; ordering is owned by TextDelivery."""

    @staticmethod
    def copy_to_clipboard(text: str, stop: Event | None = None) -> bool:
        """Try CLI clipboard helpers in order; never access GTK here."""
        if not text:
            return False
        for tool, args in (
            ("wl-copy", []),
            ("xclip", ["-selection", "clipboard"]),
            ("xsel", ["--clipboard", "--input"]),
        ):
            for command in command_candidates(tool):
                if stop is not None and stop.is_set():
                    return False
                try:
                    subprocess.run(
                        command + args, input=text.encode(), check=True, timeout=3,
                    )
                    logger.info("Copied to clipboard via %s", tool)
                    return True
                except Exception as e:
                    logger.warning("%s failed: %s", tool, e)
        return False

    @staticmethod
    def copy_with_gtk(text: str) -> bool:
        """Last-resort clipboard copy, called only on the GTK main thread."""
        if not text:
            return False
        try:
            from gi.repository import Gdk

            display = Gdk.Display.get_default()
            if display:
                clipboard = display.get_clipboard()
                clipboard.set(text)
                logger.info("Copied to clipboard via GTK")
                return True
        except Exception as e:
            logger.error("All clipboard methods failed: %s", e)
        return False

    @staticmethod
    def simulate_paste(stop: Event | None = None) -> bool:
        """Simulate the paste keystroke for the focused window.

        Terminals use Ctrl+Shift+V; everything else uses Ctrl+V.
        """
        if stop is not None and stop.is_set():
            return False
        use_shift = PasteHelper._focused_window_is_terminal()
        if stop is not None and stop.is_set():
            return False
        x11_session = is_x11_session()

        # A native X11 desktop needs no uinput permissions or daemon. Do not
        # take this shortcut on Wayland merely because XWayland sets DISPLAY.
        if x11_session and PasteHelper._paste_with_xdotool(use_shift, stop):
            return True

        # Try uinput first: kernel-level injection that works on both
        # Wayland and X11 with no external tools. Gated on /dev/uinput
        # write access (SteamOS grants it; most distros do not by default).
        if UinputPaster.is_available():
            if stop is not None and stop.is_set():
                return False
            if UinputPaster.paste(use_shift=use_shift):
                logger.info("Paste simulated via uinput")
                return True
            logger.warning("uinput paste failed, falling back")

        # Try ydotool (works on both Wayland and X11)
        # Keycodes: 29=LEFTCTRL, 42=LEFTSHIFT, 47=V
        if use_shift:
            ydotool_keys = ["29:1", "42:1", "47:1", "47:0", "42:0", "29:0"]
        else:
            ydotool_keys = ["29:1", "47:1", "47:0", "29:0"]
        for command in command_candidates("ydotool"):
            if stop is not None and stop.is_set():
                return False
            try:
                subprocess.run(
                    command + ["key"] + ydotool_keys,
                    check=True,
                    timeout=3,
                )
                logger.info("Paste simulated via ydotool")
                return True
            except Exception as e:
                logger.warning("ydotool failed: %s", e)

        # Try wtype (Wayland virtual keyboard)
        if use_shift:
            wtype_args = ["-M", "ctrl", "-M", "shift", "-P", "v",
                          "-m", "shift", "-m", "ctrl"]
        else:
            wtype_args = ["-M", "ctrl", "-P", "v", "-m", "ctrl"]
        for command in command_candidates("wtype"):
            if stop is not None and stop.is_set():
                return False
            try:
                subprocess.run(
                    command + wtype_args,
                    check=True,
                    timeout=3,
                )
                logger.info("Paste simulated via wtype")
                return True
            except Exception as e:
                logger.warning("wtype failed: %s", e)

        # Try xdotool (X11 only)
        if not x11_session and PasteHelper._paste_with_xdotool(use_shift, stop):
            return True

        logger.error("No paste simulation method available")
        logger.info(
            "Text was copied to clipboard but could not auto-paste. "
            "Install xdotool on X11 or configure ydotool on Wayland."
        )
        return False

    @staticmethod
    def _paste_with_xdotool(use_shift: bool, stop: Event | None = None) -> bool:
        """Try local/Flatpak host X11 injection, reporting success for fallback."""
        xdotool_key = "ctrl+shift+v" if use_shift else "ctrl+v"
        for command in command_candidates("xdotool"):
            if stop is not None and stop.is_set():
                return False
            try:
                subprocess.run(
                    command + ["key", xdotool_key],
                    check=True,
                    timeout=3,
                )
                logger.info("Paste simulated via xdotool (%s)", xdotool_key)
                return True
            except Exception as e:
                logger.warning("xdotool failed: %s", e)
        return False

    @staticmethod
    def _focused_window_is_terminal() -> bool:
        """Check whether the focused window is a terminal emulator."""
        wm_classes = PasteHelper._focused_window_classes()
        if not wm_classes:
            return False
        # Wayland resourceClass may be a reverse-DNS app id like
        # "org.kde.konsole"; also match on the last dot-segment.
        candidates = set(wm_classes)
        for c in wm_classes:
            candidates.add(c.rsplit(".", 1)[-1])
        is_terminal = bool(candidates & _TERMINAL_WM_CLASSES)
        logger.info(
            "Focused window class: %s (terminal=%s)",
            "/".join(wm_classes),
            is_terminal,
        )
        return is_terminal

    @staticmethod
    def _focused_window_classes() -> list[str]:
        """Lowercased WM_CLASS entries of the focused window.

        On KDE Plasma Wayland (e.g. SteamOS desktop mode) X11 tools
        cannot see the active window, so ask KWin first via its
        scripting API.

        `getwindowclassname` only exists in recent xdotool releases;
        Debian/Ubuntu still ship 3.20160805, where it exits with
        "Unknown command". Detection then always failed, so every paste
        used Ctrl+V -- which terminals swallow instead of pasting. Fall
        back to xprop, which lives in x11-utils and is present on any
        desktop that has xdotool.
        """
        kwin_class = kwin_active_window_class()
        if kwin_class:
            return [kwin_class]

        for command in command_candidates("xdotool"):
            try:
                result = subprocess.run(
                    command + ["getactivewindow", "getwindowclassname"],
                    capture_output=True,
                    check=True,
                    timeout=3,
                )
                wm_class = result.stdout.decode().strip().lower()
                if wm_class:
                    return [wm_class]
            except Exception as e:
                logger.debug("xdotool getwindowclassname failed: %s", e)

        window_id = ""
        for command in command_candidates("xdotool"):
            try:
                result = subprocess.run(
                    command + ["getactivewindow"],
                    capture_output=True,
                    check=True,
                    timeout=3,
                )
                window_id = result.stdout.decode().strip()
                break
            except Exception as e:
                logger.warning("Active window detection failed: %s", e)
        if not window_id:
            return []

        for command in command_candidates("xprop"):
            try:
                result = subprocess.run(
                    command + ["-id", window_id, "WM_CLASS"],
                    capture_output=True,
                    check=True,
                    timeout=3,
                )
                # WM_CLASS(STRING) = "terminator", "Terminator"
                values = result.stdout.decode().partition("=")[2]
                return [
                    part.strip().strip('"').lower()
                    for part in values.split(",")
                    if part.strip()
                ]
            except Exception as e:
                logger.warning("xprop WM_CLASS lookup failed: %s", e)
        return []
