"""Host session policy, independent of Ubuntu release and GTK's XWayland backend."""

import os


def is_x11_session() -> bool:
    """True only for an X11 desktop, not a Wayland session with DISPLAY set.

    Login managers provide XDG_SESSION_TYPE. For older launch environments,
    WAYLAND_DISPLAY takes precedence over DISPLAY. Unknown sessions retain
    the existing Wayland-compatible fallback behavior.
    """
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session in ("x11", "wayland"):
        return session == "x11"
    return bool(os.environ.get("DISPLAY")) and not os.environ.get("WAYLAND_DISPLAY")
