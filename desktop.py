"""Executor for Claude's `computer_toolset_20260801` member tools.

Claude never touches your machine. It emits `tool_use` blocks naming one of the
17 member actions; this module is the thing that actually moves the mouse and
returns a `tool_result` payload. What it does NOT contain is anything
machine-specific -- that lives behind `backend.py`, in `mac.py` or `x11.py`.

Two coordinate spaces matter:
  * model space   -- pixels of the (downscaled) screenshot Claude was shown
  * logical space -- OS points, what the window server wants
`Desktop._frame` converts one to the other and is refreshed on every full
screenshot: `scale` for the downscale and any Retina factor, `origin` for where
the shot was cropped from.

By default the shot is one window rather than the whole display (see
`window.py`), so `origin` is usually not (0, 0) -- Claude's coordinates are
measured from the corner of that window.
"""

from __future__ import annotations

import base64
import io
import math
import os
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass

from PIL import Image

try:
    import pyautogui
except Exception as exc:                 # noqa: BLE001 -- almost always a missing display
    raise RuntimeError(
        f"pyautogui could not start ({exc}). On Linux it needs an X display: check "
        "$DISPLAY, and `sudo apt install python3-xlib xclip`.") from exc

import backend as be
import motion as mo
from backend import ActionError, FailSafeAbort      # re-exported; agent.py imports them here
from motion import MotionProfile
from window import Rect, WindowTarget

pyautogui.FAILSAFE = False          # we run our own, see _guard() below
pyautogui.PAUSE = 0.0
pyautogui.DARWIN_CATCH_UP_TIME = 0.004   # keyboard only now; the pointer is ours

# Two separate limits, and a picture can pass one while failing the other. A
# 2576x1776 shot is inside the edge limit and still 6100 image tokens, which the
# API rejects outright -- it will NOT downscale for you.
MAX_EDGE = 2576          # longest side, in model pixels
MAX_IMAGE_TOKENS = 4784  # and the whole picture must fit in this many tokens
PIXELS_PER_TOKEN = 750
MAX_PIXELS = MAX_IMAGE_TOKENS * PIXELS_PER_TOKEN   # ~3.59 megapixels
SCROLL_UNIT = 2      # wheel notches per unit Claude asks for
CORNER_MARGIN = 4    # px from a corner that counts as "slammed"


def display_index() -> int:
    """Which display to capture, 1-based. Read late, so a .env file still counts."""
    try:
        return max(1, int(os.environ.get("CLAUDE_DISPLAY", "1")))
    except ValueError:
        return 1

__all__ = ["Desktop", "Frame", "ActionError", "FailSafeAbort"]


# --------------------------------------------------------------------------
# key names: Claude speaks X11 ("Return", "ctrl+s", "super+space"). The
# modifier half of the map is the backend's, since Command and Super are not
# the same key and not the same habit.
# --------------------------------------------------------------------------

def _map_key(token: str, keymap: dict) -> str:
    t = token.strip()
    low = t.lower()
    if low in keymap:
        return keymap[low]
    if len(t) == 1:
        return t.lower()
    if low.startswith("f") and low[1:].isdigit():
        return low
    return low


def _split_combo(text: str, keymap: dict) -> list[str]:
    """'ctrl+shift+t' -> ['ctrl', 'shift', 't']"""
    t = (text or "").strip()
    if t == "+":
        return ["+"]
    return [_map_key(p, keymap) for p in t.split("+") if p != ""]


@contextmanager
def _held(mods: list[str]):
    for m in mods:
        pyautogui.keyDown(m)
    try:
        yield
    finally:
        for m in reversed(mods):
            pyautogui.keyUp(m)


# --------------------------------------------------------------------------
# tool_result payload helpers
# --------------------------------------------------------------------------

def _ok():
    return [{"type": "text", "text": "OK"}]


def _text(s: str):
    return [{"type": "text", "text": s}]


def _image(b64: str):
    return [{"type": "image",
             "source": {"type": "base64", "media_type": "image/png", "data": b64}}]


@dataclass
class Frame:
    scale: float               # model-space px * scale = logical points
    width: int
    height: int
    origin: tuple[float, float] = (0.0, 0.0)   # logical point the shot starts at
    label: str = "full screen"                 # what Claude is looking at


class Desktop:
    def __init__(self, motion: MotionProfile | None = None,
                 rng: random.Random | None = None, failsafe: bool = True,
                 window: WindowTarget | None = None, backend=None) -> None:
        self.backend = backend if backend is not None else be.load()
        self.display = display_index()
        self.logical_w, self.logical_h = self.backend.screen_size()
        self.motion = motion or MotionProfile()
        self.rng = rng or random.Random()
        self.failsafe = failsafe
        # None means the whole display. agent.py passes a WindowTarget unless
        # you ask for --full-screen.
        self.window = window
        self.view = "full screen"      # description of the most recent capture
        self._frame: Frame | None = None
        # Seeded with wherever the pointer already is, so a mouse resting in a
        # corner doesn't trip the failsafe on the first action.
        self._commanded: tuple[float, float] = self.backend.cursor_position()

    # ---------------- failsafe ----------------

    def _in_corner(self, x: float, y: float) -> bool:
        m = CORNER_MARGIN
        return ((x <= m or x >= self.logical_w - 1 - m)
                and (y <= m or y >= self.logical_h - 1 - m))

    def _guard(self) -> None:
        """Abort if the pointer is in a corner and we didn't put it there."""
        if not self.failsafe:
            return
        x, y = self.backend.cursor_position()
        if not self._in_corner(x, y):
            return
        cx, cy = self._commanded
        if math.hypot(x - cx, y - cy) > 12:
            raise FailSafeAbort(
                "pointer was thrown into a screen corner -- aborting run")

    # ---------------- motion ----------------

    def _place(self, x: float, y: float, drag_button: str | None = None) -> None:
        self.backend.move(x, y, drag_button)
        self._commanded = (x, y)

    def glide(self, target: tuple[float, float], drag_button: str | None = None) -> None:
        """Travel to `target` the way a hand would, not the way a teleport would."""
        self._guard()
        start = self.backend.cursor_position()
        samples = mo.path(start, (float(target[0]), float(target[1])),
                          self.motion, self.rng,
                          bounds=(self.logical_w, self.logical_h))
        for i, (x, y, dt) in enumerate(samples):
            self._place(x, y, drag_button)
            if dt > 0:
                time.sleep(dt)
            if i % 6 == 5:
                self._guard()
        if not samples:                      # already there; still register the point
            self._place(float(target[0]), float(target[1]), drag_button)

    # ---------------- capture ----------------

    def display_rect(self) -> Rect:
        """Bounds of the display we capture, in logical points."""
        try:
            return self.backend.display_rect(self.display)
        except Exception:                    # noqa: BLE001
            # No answer from the window server (exotic setup, stubbed backend):
            # assume one display starting at the origin.
            return Rect(0.0, 0.0, float(self.logical_w), float(self.logical_h))

    def _view_rect(self) -> tuple[Rect, str]:
        """The region this screenshot should cover, and what to call it."""
        d = self.display_rect()
        if self.window is None:
            return d, "full screen"
        try:
            found = self.window.resolve(self.backend, d)
        except Exception as exc:                      # noqa: BLE001
            return d, f"full screen (window lookup failed: {exc})"
        if found is None:
            which = f"app '{self.window.app}'" if self.window.app else "focused window"
            return d, f"full screen ({which} not on screen)"
        return found

    def _fit(self, img: Image.Image) -> Image.Image:
        """Shrink until the picture satisfies both limits, or leave it alone."""
        w, h = img.size
        k = min(MAX_EDGE / max(w, h), math.sqrt(MAX_PIXELS / (w * h)))
        if k >= 1.0:
            return img
        # Floor rather than round: rounding a scale factor derived from an area
        # can land a pixel or two over the ceiling, and the API does not round
        # in your favour.
        return img.resize((max(1, math.floor(w * k)), max(1, math.floor(h * k))),
                          Image.LANCZOS)

    @staticmethod
    def _encode(img: Image.Image) -> str:
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode()

    def screenshot_b64(self) -> str:
        rect, label = self._view_rect()
        native, covered = self.backend.capture(self.display, rect)
        img = self._fit(native)
        # Everything Claude clicks from here is measured against THIS image.
        self._frame = Frame(scale=covered.w / img.size[0],
                            width=img.size[0], height=img.size[1],
                            origin=(covered.x, covered.y), label=label)
        self.view = f"{label} ({round(covered.w)}x{round(covered.h)} pt)"
        return self._encode(img)

    def _ensure_frame(self) -> Frame:
        if self._frame is None:
            self.screenshot_b64()
        assert self._frame is not None
        return self._frame

    def zoom_b64(self, region) -> str:
        f = self._ensure_frame()
        x0, y0, x1, y1 = region
        if x1 <= x0 or y1 <= y0:
            raise ActionError(f"invalid zoom region {region}")
        # Model pixels -> logical points, through the same origin a click uses,
        # so zoom reads the right patch whether the frame is a window or a
        # whole display. It deliberately does NOT touch self._frame: click
        # coordinates stay in full-screenshot space.
        want = Rect(f.origin[0] + x0 * f.scale, f.origin[1] + y0 * f.scale,
                    (x1 - x0) * f.scale, (y1 - y0) * f.scale)
        native, _covered = self.backend.capture(self.display, want)
        return self._encode(self._fit(native))

    # ---------------- coordinates ----------------

    def to_logical(self, coord) -> tuple[float, float]:
        f = self._ensure_frame()
        x, y = coord
        return (f.origin[0] + x * f.scale, f.origin[1] + y * f.scale)

    def to_model(self, x: float, y: float) -> tuple[int, int]:
        f = self._ensure_frame()
        return (round((x - f.origin[0]) / f.scale), round((y - f.origin[1]) / f.scale))

    # ---------------- typing ----------------

    def combo(self, text: str) -> list[str]:
        return _split_combo(text, self.backend.keymap)

    def type_text(self, text: str) -> None:
        limit = 120 if self.motion.enabled else 250
        if not text.isascii() or len(text) > limit:
            # Keystroke synthesis of unicode is unreliable, and typing a
            # paragraph at human speed takes half a minute. Paste instead, then
            # put the user's clipboard back.
            old = self.backend.clip_read()
            self.backend.clip_write(text)
            time.sleep(0.06)
            pyautogui.hotkey(*self.backend.paste_combo)
            time.sleep(0.18)
            if old is not None:
                self.backend.clip_write(old)
            return

        for i, (ch, delay) in enumerate(mo.key_intervals(text, self.motion, self.rng)):
            pyautogui.write(ch)
            time.sleep(delay)
            if i % 20 == 19:
                self._guard()

    # ---------------- dispatch ----------------

    def run(self, name: str, args: dict):
        fn = getattr(self, f"_do_{name}", None)
        if fn is None:
            raise ActionError(f"unsupported action: {name}")
        self._guard()
        return fn(args or {})

    def _do_screenshot(self, a):
        b64 = self.screenshot_b64()
        if self.window is None:
            return _image(b64)
        # Say what the crop is. Without this Claude reads a windowless image and
        # wonders where the menu bar went.
        return _text(f"Screenshot of {self.view}. "
                     "Coordinates are pixels of this image.") + _image(b64)

    def _do_zoom(self, a):
        return _image(self.zoom_b64(a["region"]))

    def _press(self, button: str, clicks: int) -> None:
        """Down/up pairs with a real hold time and a rising click state."""
        x, y = self._commanded
        for n in range(1, clicks + 1):
            self.backend.press(button, x, y, n)
            time.sleep(mo.hold(self.motion, self.rng))
            self.backend.release(button, x, y, n)
            if n < clicks:
                time.sleep(mo.between_clicks(self.motion, self.rng))

    def click_at(self, point, button: str = "left", clicks: int = 1,
                 modifiers=()) -> None:
        """Click a point in LOGICAL coordinates. `point` None clicks where we are.

        Claude's actions arrive in model space and go through `to_logical`;
        `program.py` replays anchors that are already logical and has no frame
        to measure against. Both end up here.
        """
        if point is not None:
            self.glide(point)
        time.sleep(mo.settle(self.motion, self.rng))
        with _held(list(modifiers)):
            self._press(button, clicks)

    def drag_at(self, start, end, modifiers=()) -> None:
        """Press at `start`, travel to `end`, release. Logical coordinates."""
        self.glide(start)
        time.sleep(mo.settle(self.motion, self.rng))
        with _held(list(modifiers)):
            self.backend.press("left", start[0], start[1], 1)
            time.sleep(mo.hold(self.motion, self.rng))
            self.glide(end, drag_button="left")
            time.sleep(mo.settle(self.motion, self.rng))
            self.backend.release("left", end[0], end[1], 1)

    def _click(self, a, button: str, clicks: int):
        mods = self.combo(a["text"]) if a.get("text") else []
        point = self.to_logical(a["coordinate"]) if a.get("coordinate") else None
        self.click_at(point, button, clicks, mods)
        return _ok()

    def _do_left_click(self, a):    return self._click(a, "left", 1)
    def _do_right_click(self, a):   return self._click(a, "right", 1)
    def _do_middle_click(self, a):  return self._click(a, "middle", 1)
    def _do_double_click(self, a):  return self._click(a, "left", 2)
    def _do_triple_click(self, a):  return self._click(a, "left", 3)

    def _do_left_click_drag(self, a):
        self.drag_at(self.to_logical(a["start_coordinate"]),
                     self.to_logical(a["coordinate"]),
                     self.combo(a["text"]) if a.get("text") else [])
        return _ok()

    def _do_mouse_move(self, a):
        self.glide(self.to_logical(a["coordinate"]))
        return _ok()

    def _do_left_mouse_down(self, a):
        x, y = self._commanded
        self.backend.press("left", x, y, 1)
        return _ok()

    def _do_left_mouse_up(self, a):
        x, y = self._commanded
        self.backend.release("left", x, y, 1)
        return _ok()

    def _do_cursor_position(self, a):
        x, y = self.to_model(*self.backend.cursor_position())
        return _text(f"[{x}, {y}]")

    def _do_scroll(self, a):
        mods = self.combo(a["text"]) if a.get("text") else []
        if a.get("coordinate"):
            self.glide(self.to_logical(a["coordinate"]))
            time.sleep(mo.settle(self.motion, self.rng))
        notches = max(1, int(a.get("scroll_amount", 3)) * SCROLL_UNIT)
        d = a["scroll_direction"]
        if d not in ("up", "down", "left", "right"):
            raise ActionError(f"bad scroll_direction: {d}")
        vert = {"up": 1, "down": -1}.get(d, 0)
        horiz = {"right": 1, "left": -1}.get(d, 0)
        with _held(mods):
            # one notch at a time with uneven gaps -- a wheel is not a slider
            for _ in range(notches):
                self.backend.scroll(vert, horiz)
                if self.motion.enabled:
                    time.sleep(self.rng.uniform(0.012, 0.045) / max(0.05, self.motion.speed))
        return _ok()

    def _do_type(self, a):
        self.type_text(a["text"])
        return _ok()

    def _do_key(self, a):
        keys = self.combo(a["text"])
        if not keys:
            raise ActionError("key action with empty text")
        for _ in range(max(1, min(100, int(a.get("repeat", 1) or 1)))):
            if len(keys) == 1:
                pyautogui.press(keys[0])
            else:
                pyautogui.hotkey(*keys)
            time.sleep(self.rng.uniform(0.02, 0.06) if self.motion.enabled else 0.025)
        return _ok()

    def _do_hold_key(self, a):
        with _held(self.combo(a["text"])):
            time.sleep(min(float(a["duration"]), 300.0))
        return _ok()

    def _do_wait(self, a):
        time.sleep(min(float(a["duration"]), 300.0))
        return _ok()
