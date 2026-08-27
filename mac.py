"""macOS backend: Quartz for the pointer, `screencapture` for the screen.

Pointer events go through Quartz directly rather than through pyautogui, for
three reasons: pyautogui never sets `kCGMouseEventClickState`, so its
double-click is two single clicks and most Mac apps read it as such; it sleeps
`DARWIN_CATCH_UP_TIME` after every event, which wrecks the motion timing; and
its failsafe only fires inside its own calls. Keyboard still goes through
pyautogui, which handles keymaps well.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

import Quartz
from PIL import Image

import pyautogui

from backend import ActionError, BASE_KEYMAP
from window import Rect, WindowInfo

_BUTTONS = {
    "left":   (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp,
               Quartz.kCGEventLeftMouseDragged, Quartz.kCGMouseButtonLeft),
    "right":  (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp,
               Quartz.kCGEventRightMouseDragged, Quartz.kCGMouseButtonRight),
    "middle": (Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp,
               Quartz.kCGEventOtherMouseDragged, Quartz.kCGMouseButtonCenter),
}

KEYMAP = dict(BASE_KEYMAP)
KEYMAP.update({
    "alt": "option", "option": "option", "alt_l": "option", "alt_r": "option",
    "super": "command", "super_l": "command", "super_r": "command",
    "meta": "command", "cmd": "command", "command": "command", "win": "command",
})

PLATFORM_NOTES = """* The Command key is `super` in key combos. Copy is `super+c`, Spotlight is `super+space`, quit is `super+q`, new tab is `super+t`.
* Prefer Spotlight (`super+space`, type the app name, `Return`) over hunting for icons in the Dock."""


def _post(ev_type, x: float, y: float, button=0, click_state: int = 0) -> None:
    ev = Quartz.CGEventCreateMouseEvent(None, ev_type, (x, y), button)
    if click_state:
        # The field pyautogui omits. Without it NSEvent.clickCount is 1 and a
        # "double click" opens nothing.
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, click_state)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


class Backend:
    name = "macOS"
    os_label = "macOS"
    keymap = KEYMAP
    paste_combo = ("command", "v")
    platform_notes = PLATFORM_NOTES
    warning = ""            # the X11 backend has things to say here; this one does not

    # ---------------- pointer ----------------

    def screen_size(self) -> tuple[int, int]:
        return pyautogui.size()

    def cursor_position(self) -> tuple[float, float]:
        p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return (p.x, p.y)

    def move(self, x: float, y: float, drag_button: str | None = None) -> None:
        if drag_button:
            # Dragged events, not moved events -- many views ignore the latter.
            _post(_BUTTONS[drag_button][2], x, y, _BUTTONS[drag_button][3])
        else:
            _post(Quartz.kCGEventMouseMoved, x, y)

    def press(self, button: str, x: float, y: float, click_state: int = 1) -> None:
        down, _up, _drag, btn = _BUTTONS[button]
        _post(down, x, y, btn, click_state=click_state)

    def release(self, button: str, x: float, y: float, click_state: int = 1) -> None:
        _down, up, _drag, btn = _BUTTONS[button]
        _post(up, x, y, btn, click_state=click_state)

    def scroll(self, vertical: int, horizontal: int) -> None:
        ev = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine,
                                                  2, vertical, horizontal)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    # ---------------- screen ----------------

    def capture(self, display_index: int, region: Rect) -> tuple[Image.Image, Rect]:
        """Pixels covering `region`, and the rectangle they actually cover.

        `screencapture` grabs a whole display, so the crop happens here. On a
        Retina display one point is two pixels, which is why the caller is told
        what it got rather than assuming.
        """
        full = self._grab(display_index)
        d = self.display_rect(display_index)
        px = full.size[0] / d.w if d.w else 1.0          # native pixels per point
        want = region.clamp(d)
        box = (max(0, round((want.x - d.x) * px)), max(0, round((want.y - d.y) * px)),
               min(full.size[0], round((want.right - d.x) * px)),
               min(full.size[1], round((want.bottom - d.y) * px)))
        if box[2] - box[0] < 1 or box[3] - box[1] < 1:
            raise ActionError(f"nothing to capture at {region}")
        img = full if box == (0, 0, full.size[0], full.size[1]) else full.crop(box)
        covered = Rect(d.x + box[0] / px, d.y + box[1] / px,
                       (box[2] - box[0]) / px, (box[3] - box[1]) / px)
        return img, covered

    def _grab(self, display_index: int = 1) -> Image.Image:
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            r = subprocess.run(
                ["screencapture", "-x", "-C", "-D", str(display_index), "-t", "png", path],
                capture_output=True, timeout=30,
            )
            if r.returncode != 0 or not os.path.getsize(path):
                raise ActionError(
                    "screencapture failed. Grant Screen Recording to your terminal in "
                    "System Settings > Privacy & Security > Screen & System Audio Recording, "
                    "then fully quit and reopen the terminal."
                )
            return Image.open(path).convert("RGB")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def display_rect(self, index: int = 1) -> Rect:
        """Bounds of the display `screencapture -D <index>` grabs. 1-based."""
        err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
        if err or not count:
            raise RuntimeError("could not enumerate displays")
        did = list(ids[:count])[min(max(index, 1), count) - 1]
        b = Quartz.CGDisplayBounds(did)
        return Rect(float(b.origin.x), float(b.origin.y),
                    float(b.size.width), float(b.size.height))

    # ---------------- windows ----------------

    def list_windows(self) -> list[WindowInfo]:
        """On-screen windows, front to back. Quartz already sorts them that way."""
        opts = (Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements)
        out: list[WindowInfo] = []
        for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
            if float(w.get("kCGWindowAlpha", 1.0) or 0.0) <= 0.05:
                continue
            b = w.get("kCGWindowBounds") or {}
            out.append(WindowInfo(
                app=str(w.get("kCGWindowOwnerName") or ""),
                title=str(w.get("kCGWindowName") or ""),
                pid=int(w.get("kCGWindowOwnerPID", 0) or 0),
                layer=int(w.get("kCGWindowLayer", 0) or 0),
                rect=Rect(float(b.get("X", 0.0)), float(b.get("Y", 0.0)),
                          float(b.get("Width", 0.0)), float(b.get("Height", 0.0))),
                handle=w.get("kCGWindowNumber")))
        return out

    def frontmost_pid(self) -> int | None:
        """The app the user is actually in, per NSWorkspace. None if unavailable."""
        try:
            from AppKit import NSWorkspace
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            return int(app.processIdentifier()) if app else None
        except Exception:
            return None

    # ---------------- clipboard (pbcopy/pbpaste ship with macOS) -------------

    def set_window_rect(self, win: WindowInfo, rect: Rect) -> bool:
        """Move and size another application's window.

        Quartz cannot do this -- it is the Accessibility API's job, and the one
        route to that without a new dependency is System Events, in the same
        spirit as shelling out to pbcopy. Needs Automation permission for System
        Events as well as Accessibility, which is a separate prompt.
        """
        script = (f'tell application "System Events" to tell '
                  f'(first process whose unix id is {win.pid}) to '
                  f'tell (first window whose value of attribute "AXMain" is true) to '
                  f'set {{position, size}} to '
                  f'{{{{{round(rect.x)}, {round(rect.y)}}}, '
                  f'{{{round(rect.w)}, {round(rect.h)}}}}}')
        try:
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=10)
        except Exception:                              # noqa: BLE001
            return False
        return r.returncode == 0

    def clip_read(self) -> str | None:
        try:
            return subprocess.run(["pbpaste"], capture_output=True,
                                  text=True, timeout=5).stdout
        except Exception:
            return None

    def clip_write(self, text: str) -> None:
        subprocess.run(["pbcopy"], input=text, text=True, timeout=5)
