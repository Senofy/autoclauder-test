"""The machine-specific half of the executor, and how it gets chosen.

`desktop.py` holds everything that is the same on every machine -- pointer
paths, the frame and its coordinate arithmetic, cropping, the action table.
Everything that is not -- how you post a mouse event, how you grab a display,
how you ask which window has focus -- lives behind this interface, in `mac.py`
or `x11.py`.

A backend supplies:

    name            "macOS" / "X11", for logs and the banner
    keymap          Claude's X11-flavoured key names -> pyautogui's names
    paste_combo     what pastes the clipboard ("command"+v vs ctrl+v)
    platform_notes  the part of the system prompt that is untrue on the other OS

    screen_size()                       -> (w, h) logical points
    cursor_position()                   -> (x, y)
    move(x, y, drag_button)             absolute pointer move, dragging if held
    press/release(button, x, y, n)      n is the click count, 1/2/3
    scroll(vertical, horizontal)        one notch, +1 up / +1 right
    capture(display_index)              -> PIL.Image, native pixels
    display_rect(index)                 -> window.Rect, logical points
    list_windows()                      -> [window.WindowInfo], front to back
    frontmost_pid()                     -> int | None
    clip_read() / clip_write(text)
"""

from __future__ import annotations

import os
import sys


class ActionError(Exception):
    """Action could not be carried out. Reported back to Claude, run continues."""


class FailSafeAbort(RuntimeError):
    """Human grabbed the mouse and threw it into a corner. Kills the run."""


# Key names Claude uses (X11 flavoured) -> pyautogui's names. Modifiers differ
# per platform and are added by the backend; these are the same everywhere.
BASE_KEYMAP = {
    "return": "enter", "enter": "enter", "kp_enter": "enter",
    "escape": "esc", "esc": "esc",
    "backspace": "backspace", "delete": "delete", "kp_delete": "delete",
    "tab": "tab", "space": "space", "insert": "insert",
    "page_up": "pageup", "prior": "pageup",
    "page_down": "pagedown", "next": "pagedown",
    "home": "home", "end": "end",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "caps_lock": "capslock", "capslock": "capslock",
    "ctrl": "ctrl", "control": "ctrl",
    "shift": "shift",
}


def default_name() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "x11"
    raise RuntimeError(f"no backend for {sys.platform}; this runs on macOS and on X11")


def load(name: str | None = None):
    """Backend for this machine. CLAUDE_BACKEND=macos|x11 overrides the guess."""
    name = (name or os.environ.get("CLAUDE_BACKEND") or default_name()).lower()
    if name in ("macos", "mac", "darwin"):
        try:
            import mac
        except ImportError as exc:
            raise RuntimeError(f"the macOS backend needs pyobjc ({exc}); "
                               "pip install -r requirements.txt") from exc
        return mac.Backend()
    if name in ("x11", "linux"):
        try:
            import x11
        except ImportError as exc:
            raise RuntimeError(f"the X11 backend needs python-xlib ({exc}); "
                               "sudo apt install python3-xlib xclip") from exc
        return x11.Backend()
    raise RuntimeError(f"unknown backend {name!r}; try macos or x11")
