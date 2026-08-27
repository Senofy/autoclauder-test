"""Windows backend: SendInput for the pointer, ImageGrab for pixels, EnumWindows for the rest.

    pip install -r requirements.txt

No new dependency: everything here is `ctypes` against user32/gdi32/kernel32,
plus Pillow's `ImageGrab`, which is already required. Only `ctypes.windll`
exists solely on Windows, so this module imports (and is tested) anywhere.

Four things differ from the other two backends:

* **DPI awareness is not optional.** An unaware process is lied to: Windows
  virtualises coordinates to 96 DPI and scales what it hands back, so on any
  display above 100% scaling every click lands somewhere else. `_claim_dpi()`
  runs at import, before anything creates a window or a device context, which
  is the only point at which the claim is allowed to take effect.
* **There is no click-count field**, as on X11. Windows infers a double click
  from the gap between presses (`GetDoubleClickTime`, 500ms by default), and
  `motion.between_clicks` already produces 60-130ms.
* **A drag is motion with a button held.** No separate dragged event.
* **`GetWindowRect` lies on Windows 10 and later** -- it includes an invisible
  resize border, so a window measured that way is a few pixels bigger than what
  you see. `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)` is the true
  visible rectangle, and that is what the crop needs.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import byref, sizeof

from PIL import Image, ImageGrab

import pyautogui

from backend import ActionError, BASE_KEYMAP
from window import OVERLAY_LAYER, Rect, WindowInfo

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

MOUSEEVENTF = {
    "move": 0x0001, "absolute": 0x8000, "virtualdesk": 0x4000,
    "leftdown": 0x0002, "leftup": 0x0004,
    "rightdown": 0x0008, "rightup": 0x0010,
    "middledown": 0x0020, "middleup": 0x0040,
    "wheel": 0x0800, "hwheel": 0x1000,
}
_BUTTONS = {"left": ("leftdown", "leftup"),
            "right": ("rightdown", "rightup"),
            "middle": ("middledown", "middleup")}
WHEEL_DELTA = 120
INPUT_MOUSE = 0

SM_CXSCREEN, SM_CYSCREEN = 0, 1
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
MONITORINFOF_PRIMARY = 1

# Window classes that are the desktop itself rather than something you work in.
# The analogue of the macOS Dock and the X11 panel.
FURNITURE_CLASSES = {
    "shell_traywnd", "shell_secondarytraywnd", "progman", "workerw",
    "button", "notifyiconoverflowwindow", "windows.ui.core.corewindow",
    "xamlexplorerhostislandwindow", "foregroundstaging", "applicationmanager_dev",
}
# Classes that are a panel drawn over the top: menus, tooltips, combo popups.
OVERLAY_CLASSES = {"#32768", "tooltips_class32", "combolbox", "dropdown"}

KEYMAP = dict(BASE_KEYMAP)
KEYMAP.update({
    "alt": "alt", "option": "alt", "alt_l": "alt", "alt_r": "altright",
    # `super` is the Windows key here. The shortcut modifier is Control, which
    # is what the system prompt tells Claude to reach for.
    "super": "win", "super_l": "win", "super_r": "winright",
    "meta": "win", "cmd": "win", "command": "win", "win": "win",
})

PLATFORM_NOTES = """* This is Windows. The shortcut modifier is Control: copy is `ctrl+c`, close is `ctrl+w`, new tab is `ctrl+t`. `super` is the Windows key.
* To start an application, press `super`, type its name, and press `Return` -- the Start menu searches as you type."""


# --------------------------------------------------------------------------
# structures. Declared with plain ctypes types so this file imports on any OS;
# only the windll calls below are Windows-only.
# --------------------------------------------------------------------------

LONG, DWORD, WORD = ctypes.c_long, ctypes.c_ulong, ctypes.c_ushort
ULONG_PTR = ctypes.c_size_t          # 4 bytes on Win32, 8 on Win64, like the SDK
HWND, HANDLE = ctypes.c_void_p, ctypes.c_void_p


class RECT(ctypes.Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                ("dwFlags", DWORD)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", LONG), ("dy", LONG), ("mouseData", DWORD),
                ("dwFlags", DWORD), ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("padding", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", DWORD), ("u", _INPUTUNION)]


# WINFUNCTYPE is stdcall and exists only on Windows; CFUNCTYPE keeps the module
# importable elsewhere, which is what lets the test suite drive this code.
_CALLBACK = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
ENUMWINDOWSPROC = _CALLBACK(ctypes.c_bool, HWND, ctypes.c_void_p)
MONITORENUMPROC = _CALLBACK(ctypes.c_bool, HANDLE, HANDLE, ctypes.POINTER(RECT),
                            ctypes.c_void_p)


def _dll(name: str):
    """user32 and friends, or a clear error off Windows."""
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        raise RuntimeError("this backend needs Windows; there is no ctypes.windll here")
    return getattr(windll, name)


def _claim_dpi() -> str:
    """Tell Windows we speak in real pixels. Must happen before any window exists.

    Without this the process is DPI-virtualised: on a display at 125% or 150%
    scaling the API reports a fictional coordinate space and hands back scaled
    screenshots, so every coordinate Claude sends lands in the wrong place.
    """
    try:                                    # Windows 10 1703+, per-monitor v2
        if _dll("user32").SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor v2"
    except Exception:                       # noqa: BLE001
        pass
    try:                                    # Windows 8.1+
        if _dll("shcore").SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except Exception:                       # noqa: BLE001
        pass
    try:                                    # Vista+
        if _dll("user32").SetProcessDPIAware():
            return "system"
    except Exception:                       # noqa: BLE001
        pass
    return ""


class Backend:
    name = "Windows"
    os_label = "Windows"
    keymap = KEYMAP
    paste_combo = ("ctrl", "v")
    platform_notes = PLATFORM_NOTES

    def __init__(self) -> None:
        if os.name != "nt" and not hasattr(ctypes, "windll"):
            raise RuntimeError("the Windows backend needs Windows")
        self.user32 = _dll("user32")
        self.kernel32 = _dll("kernel32")
        self.dpi = _claim_dpi()
        self.warning = "" if self.dpi else (
            "could not claim DPI awareness; on a display scaled above 100% every "
            "coordinate will be wrong")

    # ---------------- pointer ----------------

    def screen_size(self) -> tuple[int, int]:
        return (int(self.user32.GetSystemMetrics(SM_CXSCREEN)),
                int(self.user32.GetSystemMetrics(SM_CYSCREEN)))

    def _virtual_screen(self) -> Rect:
        g = self.user32.GetSystemMetrics
        return Rect(float(g(SM_XVIRTUALSCREEN)), float(g(SM_YVIRTUALSCREEN)),
                    float(g(SM_CXVIRTUALSCREEN)), float(g(SM_CYVIRTUALSCREEN)))

    def cursor_position(self) -> tuple[float, float]:
        p = POINT()
        self.user32.GetCursorPos(byref(p))
        return (float(p.x), float(p.y))

    def _send(self, flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> None:
        ev = INPUT(type=INPUT_MOUSE)
        ev.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=data, dwFlags=flags,
                           time=0, dwExtraInfo=0)
        if not self.user32.SendInput(1, byref(ev), sizeof(INPUT)):
            raise ActionError("SendInput was blocked -- another process is probably "
                              "running elevated and this one is not")

    def move(self, x: float, y: float, drag_button: str | None = None) -> None:
        # A drag is motion with the button still down; Windows has no separate
        # dragged event, so drag_button is deliberately ignored.
        v = self._virtual_screen()
        # Absolute coordinates are 0..65535 across the whole virtual desktop.
        nx = round((x - v.x) * 65535 / max(1.0, v.w - 1))
        ny = round((y - v.y) * 65535 / max(1.0, v.h - 1))
        self._send(MOUSEEVENTF["move"] | MOUSEEVENTF["absolute"]
                   | MOUSEEVENTF["virtualdesk"], dx=nx, dy=ny)

    def press(self, button: str, x: float, y: float, click_state: int = 1) -> None:
        self._send(MOUSEEVENTF[_BUTTONS[button][0]])

    def release(self, button: str, x: float, y: float, click_state: int = 1) -> None:
        self._send(MOUSEEVENTF[_BUTTONS[button][1]])

    def scroll(self, vertical: int, horizontal: int) -> None:
        if vertical:
            self._send(MOUSEEVENTF["wheel"], data=WHEEL_DELTA * (1 if vertical > 0 else -1))
        elif horizontal:
            self._send(MOUSEEVENTF["hwheel"], data=WHEEL_DELTA * (1 if horizontal > 0 else -1))

    # ---------------- screen ----------------

    def capture(self, display_index: int, region: Rect) -> tuple[Image.Image, Rect]:
        """Pixels of `region`. DPI-aware, so one pixel is one point and the
        region comes back exactly as asked for."""
        want = region.clamp(self.display_rect(display_index))
        x, y = int(round(want.x)), int(round(want.y))
        w, h = int(round(want.right)) - x, int(round(want.bottom)) - y
        if w < 1 or h < 1:
            raise ActionError(f"nothing to capture at {region}")
        try:
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        except Exception as exc:                       # noqa: BLE001
            raise ActionError(f"could not read the screen: {exc}") from exc
        return img.convert("RGB"), Rect(float(x), float(y), float(w), float(h))

    def _monitors(self) -> list[Rect]:
        """Every monitor, primary first, then left to right."""
        found: list[tuple[int, float, Rect]] = []

        def each(hmon, _hdc, _rect, _data):
            info = MONITORINFO()
            info.cbSize = sizeof(MONITORINFO)
            if self.user32.GetMonitorInfoW(hmon, byref(info)):
                r = info.rcMonitor
                found.append((0 if info.dwFlags & MONITORINFOF_PRIMARY else 1,
                              float(r.left),
                              Rect(float(r.left), float(r.top),
                                   float(r.right - r.left), float(r.bottom - r.top))))
            return True

        self.user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(each), 0)
        if not found:
            return [self._virtual_screen()]
        return [r for _p, _x, r in sorted(found, key=lambda t: (t[0], t[1]))]

    def display_rect(self, index: int = 1) -> Rect:
        mons = self._monitors()
        return mons[min(max(index, 1), len(mons)) - 1]

    # ---------------- windows ----------------

    def list_windows(self) -> list[WindowInfo]:
        """Top-level windows, front to back. EnumWindows walks them in z-order."""
        out: list[WindowInfo] = []

        def each(hwnd, _data):
            info = self._describe(hwnd)
            if info is not None:
                out.append(info)
            return True

        self.user32.EnumWindows(ENUMWINDOWSPROC(each), 0)
        return out

    def _describe(self, hwnd) -> WindowInfo | None:
        if not self.user32.IsWindowVisible(hwnd):
            return None
        cls = self._class_name(hwnd).lower()
        if cls in FURNITURE_CLASSES:
            return None
        if self._cloaked(hwnd):
            # A UWP window on another virtual desktop is still "visible" to
            # EnumWindows; the compositor is simply not drawing it.
            return None

        rect = self._frame(hwnd)
        if rect is None:
            return None
        style = self.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        overlay = (cls in OVERLAY_CLASSES) or bool(style & WS_EX_TOPMOST)
        pid = ctypes.c_ulong(0)
        self.user32.GetWindowThreadProcessId(hwnd, byref(pid))
        return WindowInfo(app=self._process_name(pid.value),
                          title=self._window_text(hwnd),
                          pid=int(pid.value),
                          layer=OVERLAY_LAYER if overlay else 0,
                          rect=rect)

    def _frame(self, hwnd) -> Rect | None:
        """The rectangle you can actually see.

        GetWindowRect includes an invisible resize border on Windows 10+, so it
        reports a window several pixels wider than it looks. The DWM extended
        frame bounds is the real one.
        """
        r = RECT()
        try:
            ok = _dll("dwmapi").DwmGetWindowAttribute(
                hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, byref(r), sizeof(RECT)) == 0
        except Exception:                              # noqa: BLE001
            ok = False
        if not ok and not self.user32.GetWindowRect(hwnd, byref(r)):
            return None
        w, h = r.right - r.left, r.bottom - r.top
        if w <= 0 or h <= 0:
            return None
        return Rect(float(r.left), float(r.top), float(w), float(h))

    def _cloaked(self, hwnd) -> bool:
        value = DWORD(0)
        try:
            if _dll("dwmapi").DwmGetWindowAttribute(
                    hwnd, DWMWA_CLOAKED, byref(value), sizeof(DWORD)) == 0:
                return bool(value.value)
        except Exception:                              # noqa: BLE001
            pass
        return False

    def _window_text(self, hwnd) -> str:
        n = int(self.user32.GetWindowTextLengthW(hwnd))
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        self.user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def _class_name(self, hwnd) -> str:
        buf = ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def _process_name(self, pid: int) -> str:
        """'chrome.exe' -> 'chrome', which is what --window-app matches on."""
        h = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            size = ctypes.c_ulong(260)
            buf = ctypes.create_unicode_buffer(size.value)
            if not self.kernel32.QueryFullProcessImageNameW(h, 0, buf, byref(size)):
                return ""
            name = buf.value.replace("\\", "/").rsplit("/", 1)[-1]
            return name[:-4] if name.lower().endswith(".exe") else name
        finally:
            self.kernel32.CloseHandle(h)

    def frontmost_pid(self) -> int | None:
        hwnd = self.user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.c_ulong(0)
        self.user32.GetWindowThreadProcessId(hwnd, byref(pid))
        return int(pid.value) or None

    # ---------------- clipboard ----------------

    def clip_read(self) -> str | None:
        if not self.user32.OpenClipboard(None):
            return None
        try:
            handle = self.user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = self.kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.c_wchar_p(ptr).value or ""
            finally:
                self.kernel32.GlobalUnlock(handle)
        finally:
            self.user32.CloseClipboard()

    def clip_write(self, text: str) -> None:
        if not self.user32.OpenClipboard(None):
            raise ActionError("could not open the clipboard; another process holds it")
        try:
            self.user32.EmptyClipboard()
            data = ctypes.create_unicode_buffer(text)
            handle = self.kernel32.GlobalAlloc(GMEM_MOVEABLE, sizeof(data))
            if not handle:
                raise ActionError("could not allocate clipboard memory")
            ptr = self.kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, byref(data), sizeof(data))
            self.kernel32.GlobalUnlock(handle)
            # On success the system owns that memory; do not free it.
            if not self.user32.SetClipboardData(CF_UNICODETEXT, handle):
                raise ActionError("SetClipboardData failed")
        finally:
            self.user32.CloseClipboard()
