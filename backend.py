"""The machine-specific half of the executor, and how it gets chosen.

`desktop.py` holds everything that is the same on every machine -- pointer
paths, the frame and its coordinate arithmetic, cropping, the action table.
Everything that is not -- how you post a mouse event, how you grab a display,
how you ask which window has focus -- lives behind this interface, in `mac.py`,
`x11.py` or `win32.py`.

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

import inspect
import os
import sys
from typing import Protocol, runtime_checkable

from PIL import Image

from window import Rect, WindowInfo


@runtime_checkable
class Backend(Protocol):
    """What `desktop.py` is allowed to assume about a machine.

    Python has no compile step, so this is checked two ways instead: any
    backend must satisfy `isinstance(be, Backend)` -- which catches a missing
    method -- and `check_interface()` compares parameter names, which catches
    the subtler case of a signature drifting on one platform only. The test
    suite runs both against all three backends, so interface drift fails there
    rather than three steps into a live run on the one machine you cannot test.
    """

    name: str
    os_label: str
    keymap: dict
    paste_combo: tuple
    platform_notes: str
    warning: str

    def screen_size(self) -> tuple[int, int]: ...
    def cursor_position(self) -> tuple[float, float]: ...
    def move(self, x: float, y: float, drag_button: str | None = None) -> None: ...
    def press(self, button: str, x: float, y: float, click_state: int = 1) -> None: ...
    def release(self, button: str, x: float, y: float, click_state: int = 1) -> None: ...
    def scroll(self, vertical: int, horizontal: int) -> None: ...
    def capture(self, display_index: int, region: Rect) -> tuple[Image.Image, Rect]: ...
    def display_rect(self, index: int = 1) -> Rect: ...
    def list_windows(self) -> list[WindowInfo]: ...
    def frontmost_pid(self) -> int | None: ...
    def clip_read(self) -> str | None: ...
    def clip_write(self, text: str) -> None: ...


def check_interface(be) -> list[str]:
    """Every way `be` fails to be a Backend. Empty means it conforms."""
    problems = []
    for field in ("name", "os_label", "keymap", "paste_combo", "platform_notes",
                  "warning"):
        if not hasattr(be, field):
            problems.append(f"missing attribute {field!r}")
    for name, spec in vars(Backend).items():
        if name.startswith("_") or not callable(spec):
            continue
        got = getattr(be, name, None)
        if got is None:
            problems.append(f"missing method {name}()")
            continue
        want_params = list(inspect.signature(spec).parameters)[1:]     # drop self
        got_params = list(inspect.signature(got).parameters)
        if want_params != got_params:
            problems.append(
                f"{name}({', '.join(got_params)}) should be "
                f"{name}({', '.join(want_params)})")
    return problems


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
    if sys.platform in ("win32", "cygwin"):
        return "windows"
    raise RuntimeError(
        f"no backend for {sys.platform}; this runs on macOS, on X11 and on Windows")


def load(name: str | None = None):
    """Backend for this machine. CLAUDE_BACKEND=macos|x11|windows overrides the guess."""
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
    if name in ("windows", "win32", "win"):
        try:
            import win32
        except ImportError as exc:
            raise RuntimeError(f"the Windows backend could not load ({exc}); "
                               "pip install -r requirements.txt") from exc
        return win32.Backend()
    raise RuntimeError(f"unknown backend {name!r}; try macos, x11 or windows")
