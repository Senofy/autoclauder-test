"""Which rectangle of the desktop Claude is allowed to see.

The default is one window, not the whole screen: fewer pixels for the model to
reason about, and nothing else on your desktop is sent to the API.

`WindowTarget.resolve()` answers exactly one question -- which rectangle of
logical points, in global screen coordinates, should this screenshot cover --
and `desktop.Desktop` does the cropping and the coordinate bookkeeping. The
window list itself comes from the backend: `CGWindowListCopyWindowInfo` on
macOS, the X11 root window's stacking order on Linux.

Everything here is logical points, origin at the top-left of the primary
display -- the same space the pointer is driven in, on both platforms.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Owners whose windows are desktop furniture rather than something you work in.
# The macOS Dock's window is the whole screen and "Window Server" owns the menu
# bar; on X11 the panel and the desktop-icon window are the same problem.
EXCLUDED_OWNERS = {
    "window server", "dock", "control center", "controlcenter",
    "notification center", "notificationcenter", "systemuiserver",
    "windowmanager", "screenshot", "textinputmenuagent", "universalaccessd",
    "wallpaper", "coreautha", "loginwindow",
    # X11 desktop shells that own the panel, the wallpaper or the icon layer
    "xfdesktop", "plasmashell", "gnome-shell", "cinnamon", "nemo-desktop",
    "xfce4-panel", "mate-panel", "tint2", "polybar", "waybar", "conky",
}

MIN_SIDE = 32        # smaller than this is a shadow or a helper, not a window
MIN_BASE_W = 120     # what it takes to be the *anchor* window of a capture
MIN_BASE_H = 40
OVERLAY_LAYER = 20   # at or above this: a panel drawn over ordinary windows


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    def union(self, o: "Rect") -> "Rect":
        x, y = min(self.x, o.x), min(self.y, o.y)
        return Rect(x, y, max(self.right, o.right) - x, max(self.bottom, o.bottom) - y)

    def intersects(self, o: "Rect") -> bool:
        return (self.x < o.right and o.x < self.right
                and self.y < o.bottom and o.y < self.bottom)

    def clamp(self, o: "Rect") -> "Rect":
        x, y = max(self.x, o.x), max(self.y, o.y)
        return Rect(x, y, max(0.0, min(self.right, o.right) - x),
                    max(0.0, min(self.bottom, o.bottom) - y))

    def expand(self, pad: float) -> "Rect":
        return Rect(self.x - pad, self.y - pad, self.w + 2 * pad, self.h + 2 * pad)

    @property
    def empty(self) -> bool:
        return self.w < 1 or self.h < 1


@dataclass(frozen=True)
class WindowInfo:
    """One on-screen window, however the platform describes it.

    `layer` is normalised: 0 is an ordinary application window, anything at or
    above OVERLAY_LAYER is a panel drawn over the top -- a macOS window level,
    or an override-redirect X11 window (which is what a menu, a tooltip or a
    combo popup is). `pid` is 0 when the platform will not say, which happens
    for plenty of X11 popups.
    """

    app: str
    title: str
    pid: int
    layer: int
    rect: Rect
    # Whatever the platform needs to address this window again: an hwnd, an X
    # window id, nothing at all on macOS (which goes by pid). Out of equality,
    # so two backends describing the same window still compare equal.
    handle: object = field(default=None, compare=False)

    @property
    def label(self) -> str:
        return f"{self.app} - {self.title}" if self.title else self.app

    @property
    def is_overlay(self) -> bool:
        return self.layer >= OVERLAY_LAYER

    @property
    def usable(self) -> bool:
        return (self.app.lower() not in EXCLUDED_OWNERS
                and self.rect.w >= MIN_SIDE and self.rect.h >= MIN_SIDE)


def _target_pid(wins: list[WindowInfo], frontmost: int | None) -> int | None:
    """Whose window is the user looking at?

    Whoever the platform calls frontmost, nearly always. The exception is a
    system panel drawn over everything -- Spotlight is the one that matters,
    since the agent is told to launch apps with it -- which takes the keyboard
    without ever being called frontmost. A panel belonging to a *different*
    known process outranks the frontmost app. Panels the platform will not
    attribute to anyone (X11 menus, mostly) do not: they are almost always the
    focused app's own, and get taken in by the union below anyway.
    """
    if not wins:
        return None
    panel = next((w for w in wins if w.is_overlay and w.pid > 0), None)
    if panel is not None and panel.pid != frontmost:
        return panel.pid
    if frontmost is not None and any(w.pid == frontmost for w in wins):
        return frontmost
    return wins[0].pid          # frontmost app is on another display, or is gone


@dataclass
class WindowTarget:
    """Capture one window instead of the whole display.

    `app` pins the capture to an application by (case-insensitive substring of
    its) name; left as None the crop follows whatever is focused at the moment
    of each screenshot.
    """

    app: str | None = None
    padding: float = 0.0

    def resolve(self, backend, display: Rect) -> tuple[Rect, str] | None:
        """Rect to capture and a label for it, or None to fall back to full screen."""
        on_screen = [w for w in backend.list_windows()
                     if w.usable and w.rect.intersects(display)]

        if self.app:
            needle = self.app.lower()
            mine = [w for w in on_screen if needle in w.app.lower()]
        else:
            pid = _target_pid(on_screen, backend.frontmost_pid())
            mine = [w for w in on_screen if w.pid == pid]

        big = [w for w in mine if w.rect.w >= MIN_BASE_W and w.rect.h >= MIN_BASE_H]
        if not big:
            return None
        # Anchor on the app's frontmost ordinary window. A panel of its own
        # (Spotlight, a menu) has no layer-0 window at all, so fall back to
        # whatever is in front.
        base = next((w for w in big if w.layer == 0), big[0])

        # Sheets, popovers, menus and tooltips are separate windows sitting on
        # top of the one we anchored to. Take in anything that overlaps it,
        # including a popup the platform would not name an owner for. Do not
        # take in the app's *other* windows parked elsewhere on the desktop.
        rect = base.rect
        for w in on_screen:
            if w is base or not w.rect.intersects(base.rect):
                continue
            if w in mine or (w.is_overlay and w.pid <= 0):
                rect = rect.union(w.rect)

        rect = rect.expand(self.padding).clamp(display)
        return None if rect.empty else (rect, base.label)
