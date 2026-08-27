"""X11 backend: XTEST for input, the root window for pixels, EWMH for windows.

Debian-ish setup:

    sudo apt install python3-xlib xclip
    pip install -r requirements.txt

This is the same executor as on macOS with different hands. Three things are
genuinely different and worth knowing:

* There is no `kCGMouseEventClickState`. X has no click-count field at all --
  toolkits infer a double click from the gap between presses, which is what
  `motion.between_clicks` already produces (60-130ms, well under the ~400ms
  GTK/Qt threshold).
* There is no separate "dragged" event either. Motion while a button is held
  *is* the drag, so `move()` ignores which button is down.
* A wheel notch is a button press: 4 up, 5 down, 6 left, 7 right.

Wayland is not X11. Under a Wayland session this can only see and drive X
clients through XWayland; native Wayland windows come back black and receive
nothing. `Backend.warning` says so, and the agent prints it.
"""

from __future__ import annotations

import os
import subprocess

from PIL import Image

from Xlib import X, display as xdisplay
from Xlib.ext import xtest

import pyautogui

from backend import ActionError, BASE_KEYMAP
from window import OVERLAY_LAYER, Rect, WindowInfo

# X button numbers. Note 2 is middle and 3 is right, the opposite way round
# from how the Quartz constants are named.
_BUTTONS = {"left": 1, "middle": 2, "right": 3}
_WHEEL = {"up": 4, "down": 5, "left": 6, "right": 7}

KEYMAP = dict(BASE_KEYMAP)
KEYMAP.update({
    "alt": "alt", "option": "alt", "alt_l": "alt", "alt_r": "altright",
    # On a Mac `super` means Command. Here it means the actual Super key, and
    # the shortcut modifier is Control -- which is what the system prompt tells
    # Claude to use, so this mapping is only for the rare real Super combo.
    "super": "winleft", "super_l": "winleft", "super_r": "winright",
    "meta": "winleft", "cmd": "winleft", "command": "winleft", "win": "winleft",
})

PLATFORM_NOTES = """* This is Linux. The shortcut modifier is Control: copy is `ctrl+c`, quit is `ctrl+q`, new tab is `ctrl+t`. `super` is the Windows/Super key and usually opens the desktop's own launcher.
* To start an application, open the desktop's launcher (`super`, or `super+space` on some desktops) and type its name; a terminal window plus the command also works if one is already open."""

# _NET_WM_WINDOW_TYPE values that mean "panel drawn over the top", and the two
# that mean "desktop furniture, never the thing being worked in".
_OVERLAY_TYPES = {
    "_NET_WM_WINDOW_TYPE_MENU", "_NET_WM_WINDOW_TYPE_DROPDOWN_MENU",
    "_NET_WM_WINDOW_TYPE_POPUP_MENU", "_NET_WM_WINDOW_TYPE_TOOLTIP",
    "_NET_WM_WINDOW_TYPE_COMBO", "_NET_WM_WINDOW_TYPE_DND",
    "_NET_WM_WINDOW_TYPE_NOTIFICATION", "_NET_WM_WINDOW_TYPE_SPLASH",
}
_FURNITURE_TYPES = {"_NET_WM_WINDOW_TYPE_DOCK", "_NET_WM_WINDOW_TYPE_DESKTOP"}


class Backend:
    name = "X11"
    os_label = "Linux"
    keymap = KEYMAP
    paste_combo = ("ctrl", "v")
    platform_notes = PLATFORM_NOTES

    def __init__(self) -> None:
        if not os.environ.get("DISPLAY"):
            extra = (" This looks like a Wayland session: install XWayland, or log in"
                     " to an X11 session." if os.environ.get("WAYLAND_DISPLAY") else "")
            raise RuntimeError("$DISPLAY is not set, so there is no X server to drive." + extra)
        try:
            self.d = xdisplay.Display()
        except Exception as exc:                       # noqa: BLE001
            raise RuntimeError(f"cannot open the X display: {exc}") from exc
        self.root = self.d.screen().root
        self._atoms: dict[str, int] = {}

        self.warning = ""
        if not self.d.query_extension("XTEST"):
            self.warning = ("the X server has no XTEST extension, so synthetic input "
                            "will not arrive anywhere")
        elif os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            self.warning = ("this is a Wayland session -- only XWayland clients can be "
                            "seen or driven; native Wayland windows capture black")

    # ---------------- atoms ----------------

    def _atom(self, name: str) -> int:
        if name not in self._atoms:
            self._atoms[name] = self.d.intern_atom(name)
        return self._atoms[name]

    def _prop(self, w, name: str):
        try:
            p = w.get_full_property(self._atom(name), X.AnyPropertyType)
        except Exception:            # BadWindow: it closed between calls
            return None
        return p.value if p else None

    # ---------------- pointer ----------------

    def screen_size(self) -> tuple[int, int]:
        g = self.root.get_geometry()
        return (int(g.width), int(g.height))

    def cursor_position(self) -> tuple[float, float]:
        p = self.root.query_pointer()
        return (float(p.root_x), float(p.root_y))

    def move(self, x: float, y: float, drag_button: str | None = None) -> None:
        # A drag is just motion with the button still down; X has no separate
        # dragged event, so drag_button is deliberately ignored.
        xtest.fake_input(self.d, X.MotionNotify, x=int(round(x)), y=int(round(y)))
        self.d.sync()

    def press(self, button: str, x: float, y: float, click_state: int = 1) -> None:
        xtest.fake_input(self.d, X.ButtonPress, _BUTTONS[button])
        self.d.sync()

    def release(self, button: str, x: float, y: float, click_state: int = 1) -> None:
        xtest.fake_input(self.d, X.ButtonRelease, _BUTTONS[button])
        self.d.sync()

    def scroll(self, vertical: int, horizontal: int) -> None:
        if vertical:
            b = _WHEEL["up" if vertical > 0 else "down"]
        elif horizontal:
            b = _WHEEL["right" if horizontal > 0 else "left"]
        else:
            return
        xtest.fake_input(self.d, X.ButtonPress, b)
        xtest.fake_input(self.d, X.ButtonRelease, b)
        self.d.sync()

    # ---------------- screen ----------------

    def capture(self, display_index: int, region: Rect) -> tuple[Image.Image, Rect]:
        """Pixels of `region`, clamped to that display. X11 has no Retina scale,
        so one pixel is one point and the region comes back exactly."""
        want = region.clamp(self.display_rect(display_index))
        x, y = int(round(want.x)), int(round(want.y))
        w, h = int(round(want.right)) - x, int(round(want.bottom)) - y
        if w < 1 or h < 1:
            raise ActionError(f"nothing to capture at {region}")
        try:
            raw = self.root.get_image(x, y, w, h, X.ZPixmap, 0xffffffff)
        except Exception as exc:                       # noqa: BLE001
            raise ActionError(f"could not read the screen: {exc}") from exc
        data = raw.data
        if isinstance(data, str):
            data = data.encode("latin-1")
        if len(data) != w * h * 4:
            raise ActionError(
                f"unexpected image format: {len(data)} bytes for {w}x{h}. "
                "This backend needs a 24- or 32-bit display.")
        img = Image.frombytes("RGB", (w, h), data, "raw", "BGRX")
        return img, Rect(float(x), float(y), float(w), float(h))

    def _monitors(self) -> list[Rect]:
        """Every monitor, primary first, then left to right."""
        try:
            mons = self.root.xrandr_get_monitors().monitors
            got = []
            for m in mons:
                w = getattr(m, "width_in_pixels", None) or getattr(m, "width", 0)
                h = getattr(m, "height_in_pixels", None) or getattr(m, "height", 0)
                if w and h:
                    got.append((0 if getattr(m, "primary", False) else 1, m.x,
                                Rect(float(m.x), float(m.y), float(w), float(h))))
            if got:
                return [r for _p, _x, r in sorted(got, key=lambda t: (t[0], t[1]))]
        except Exception:                              # noqa: BLE001
            pass
        try:                                           # older servers: walk the CRTCs
            res = self.root.xrandr_get_screen_resources_current()
            got = []
            for crtc in res.crtcs:
                i = self.d.xrandr_get_crtc_info(crtc, res.config_timestamp)
                if i.mode and i.width and i.height:
                    got.append(Rect(float(i.x), float(i.y), float(i.width), float(i.height)))
            if got:
                return sorted(got, key=lambda r: (r.x, r.y))
        except Exception:                              # noqa: BLE001
            pass
        w, h = self.screen_size()                      # one big screen, then
        return [Rect(0.0, 0.0, float(w), float(h))]

    def display_rect(self, index: int = 1) -> Rect:
        mons = self._monitors()
        return mons[min(max(index, 1), len(mons)) - 1]

    # ---------------- windows ----------------

    def list_windows(self) -> list[WindowInfo]:
        """Top-level windows, front to back.

        `query_tree` on the root returns every top-level window in stacking
        order, bottom first -- including the override-redirect ones a window
        manager never sees, which is exactly what a menu or a tooltip is. That
        makes it the honest analogue of CGWindowListCopyWindowInfo, and a much
        better one than _NET_CLIENT_LIST_STACKING, which omits them.
        """
        try:
            children = self.root.query_tree().children
        except Exception:                              # noqa: BLE001
            return []
        out: list[WindowInfo] = []
        for w in reversed(children):
            info = self._describe(w)
            if info is not None:
                out.append(info)
        return out

    def _client_window(self, w, depth: int = 0):
        """The client window inside a window manager's frame.

        A reparenting WM wraps each client in a frame of its own, and the frame
        carries none of the properties we want. The client is the descendant
        with WM_STATE on it. Override-redirect windows are their own client.
        """
        if self._prop(w, "WM_STATE") is not None:
            return w
        if depth >= 3:
            return None
        try:
            kids = w.query_tree().children
        except Exception:                              # noqa: BLE001
            return None
        for c in kids:
            found = self._client_window(c, depth + 1)
            if found is not None:
                return found
        return None

    def _describe(self, w) -> WindowInfo | None:
        try:
            a = w.get_attributes()
            if a.map_state != X.IsViewable:
                return None
            if getattr(a, "win_class", X.InputOutput) == X.InputOnly:
                return None                            # no pixels to capture
            g = w.get_geometry()
        except Exception:                              # noqa: BLE001
            return None

        # Children of the root are already in root coordinates, frame included,
        # so this is the rectangle the user sees -- title bar and all.
        rect = Rect(float(g.x), float(g.y), float(g.width), float(g.height))

        client = self._client_window(w) or w
        types = {self._name_of_atom(t) for t in (self._prop(client, "_NET_WM_WINDOW_TYPE") or [])}
        if types & _FURNITURE_TYPES:
            return None                                # panel, dock, wallpaper
        states = {self._name_of_atom(s) for s in (self._prop(client, "_NET_WM_STATE") or [])}
        if "_NET_WM_STATE_HIDDEN" in states:
            return None

        override = bool(getattr(a, "override_redirect", False))
        layer = OVERLAY_LAYER if (override or (types & _OVERLAY_TYPES)) else 0

        pid = self._prop(client, "_NET_WM_PID")
        title = (self._decode(self._prop(client, "_NET_WM_NAME"))
                 or self._decode(self._prop(client, "WM_NAME")))
        return WindowInfo(app=self._app_name(client), title=title,
                          pid=int(pid[0]) if pid else 0, layer=layer, rect=rect,
                          handle=w)

    def _name_of_atom(self, atom: int) -> str:
        try:
            return self.d.get_atom_name(atom)
        except Exception:                              # noqa: BLE001
            return ""

    @staticmethod
    def _decode(value) -> str:
        if not value:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace").strip("\x00")
        if isinstance(value, str):
            return value.strip("\x00")
        return ""

    def _app_name(self, w) -> str:
        """WM_CLASS's class field: 'Discord', 'Google-chrome', 'Gnome-terminal'."""
        try:
            cls = w.get_wm_class()
            if cls:
                return str(cls[-1] or cls[0] or "")
        except Exception:                              # noqa: BLE001
            pass
        raw = self._decode(self._prop(w, "WM_CLASS"))
        return raw.split("\x00")[-1] if raw else ""

    def set_window_rect(self, win: WindowInfo, rect: Rect) -> bool:
        """Move and size a window, and say whether it actually happened.

        Three things this cannot do naively. The handle is the root child,
        which under a reparenting window manager -- Openbox, Mutter, KWin,
        xfwm, so nearly all of them -- is the *frame*, and the frame belongs to
        the WM. Resizing it directly is not the documented path and WMs vary
        between honouring it, ignoring it, and desyncing the frame from the
        client inside. The size request has to go to the client, where the WM
        picks it up as a ConfigureRequest and moves the frame to match.

        Which introduces the second thing: `win.rect` is the frame, decorations
        included, while a client resize sets the client area. Ask for the frame
        size and you get a window a title bar too tall.

        And the third: a ConfigureRequest is a request. The WM may refuse it,
        clamp it to size hints, or be tiling and ignore geometry entirely. So
        this reads the frame back afterwards rather than assuming.
        """
        frame = win.handle
        if frame is None:
            return False
        client = self._client_window(frame) or frame
        try:
            pad_w = pad_h = 0
            if client is not frame:
                fg, cg = frame.get_geometry(), client.get_geometry()
                pad_w, pad_h = fg.width - cg.width, fg.height - cg.height
            client.configure(width=max(1, int(round(rect.w - pad_w))),
                             height=max(1, int(round(rect.h - pad_h))))
            if client is not frame:
                # Position belongs to the frame; the client's x/y are relative
                # to it and mean something else entirely.
                frame.configure(x=int(round(rect.x)), y=int(round(rect.y)))
            self.d.sync()
            now = frame.get_geometry()
        except Exception:                              # noqa: BLE001
            return False
        return abs(now.width - rect.w) <= 2 and abs(now.height - rect.h) <= 2

    def frontmost_pid(self) -> int | None:
        active = self._prop(self.root, "_NET_ACTIVE_WINDOW")
        if not active or not active[0]:
            return None
        try:
            w = self.d.create_resource_object("window", active[0])
        except Exception:                              # noqa: BLE001
            return None
        pid = self._prop(self._client_window(w) or w, "_NET_WM_PID")
        return int(pid[0]) if pid else None

    # ---------------- clipboard ----------------

    _READ = (["xclip", "-selection", "clipboard", "-o"],
             ["xsel", "--clipboard", "--output"])
    _WRITE = (["xclip", "-selection", "clipboard"],
              ["xsel", "--clipboard", "--input"])

    def clip_read(self) -> str | None:
        for cmd in self._READ:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            except FileNotFoundError:
                continue
            except Exception:                          # noqa: BLE001
                return None
            return r.stdout if r.returncode == 0 else ""
        return None

    def clip_write(self, text: str) -> None:
        for cmd in self._WRITE:
            try:
                subprocess.run(cmd, input=text, text=True, timeout=5)
                return
            except FileNotFoundError:
                continue
            except Exception as exc:                   # noqa: BLE001
                raise ActionError(f"clipboard write failed: {exc}") from exc
        raise ActionError("no clipboard tool found -- sudo apt install xclip")
