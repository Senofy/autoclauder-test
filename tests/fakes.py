"""Stub Quartz + pyautogui so the executor can be exercised headlessly."""
import sys, types, time, math
from types import SimpleNamespace

SLEPT = []
_real_sleep = time.sleep
time.sleep = lambda s: SLEPT.append(s)          # keep the suite fast, record the timing

# ---- fake Quartz ----------------------------------------------------------
Q = types.ModuleType("Quartz")
EVENTS = []
STATE = {"pos": (400.0, 400.0)}

_names = ["kCGEventLeftMouseDown","kCGEventLeftMouseUp","kCGEventLeftMouseDragged",
          "kCGEventRightMouseDown","kCGEventRightMouseUp","kCGEventRightMouseDragged",
          "kCGEventOtherMouseDown","kCGEventOtherMouseUp","kCGEventOtherMouseDragged",
          "kCGEventMouseMoved","kCGMouseButtonLeft","kCGMouseButtonRight",
          "kCGMouseButtonCenter","kCGHIDEventTap","kCGScrollEventUnitLine",
          "kCGMouseEventClickState"]
for i, n in enumerate(_names):
    setattr(Q, n, n)                             # constants are just their names

def CGEventCreateMouseEvent(src, ev, pos, button):
    return {"type": ev, "pos": (float(pos[0]), float(pos[1])), "button": button, "click_state": 0}
def CGEventSetIntegerValueField(ev, field, val):
    ev[field] = val
def CGEventCreateScrollWheelEvent(src, unit, n, v, h):
    return {"type": "scroll", "v": v, "h": h}
def CGEventPost(tap, ev):
    EVENTS.append(ev)
    if "pos" in ev:
        STATE["pos"] = ev["pos"]
def CGEventCreate(src):
    return {"__probe__": True}
def CGEventGetLocation(ev):
    p = STATE["pos"] if ev.get("__probe__") else ev["pos"]
    return SimpleNamespace(x=p[0], y=p[1])

for f in (CGEventCreateMouseEvent, CGEventSetIntegerValueField, CGEventCreateScrollWheelEvent,
          CGEventPost, CGEventCreate, CGEventGetLocation):
    setattr(Q, f.__name__, f)
sys.modules["Quartz"] = Q

# ---- fake pyautogui (keyboard only now) -----------------------------------
KEYS = []
pg = types.ModuleType("pyautogui")
pg.FAILSAFE = True; pg.PAUSE = 0.0; pg.DARWIN_CATCH_UP_TIME = 0.01
def _rec(n):
    def f(*a, **k): KEYS.append((n, a, k))
    return f
for n in ("write", "press", "hotkey", "keyDown", "keyUp"):
    setattr(pg, n, _rec(n))
pg.size = lambda: (1728, 1117)
sys.modules["pyautogui"] = pg

def reset():
    EVENTS.clear(); KEYS.clear(); SLEPT.clear(); STATE["pos"] = (400.0, 400.0)
    set_windows()


# ---- fake window server: display list + on-screen windows ----------------
DISPLAYS = {1: (0.0, 0.0, 1728.0, 1117.0), 2: (1728.0, 0.0, 1440.0, 900.0)}

def _bounds(x, y, w, h):
    return SimpleNamespace(origin=SimpleNamespace(x=x, y=y),
                           size=SimpleNamespace(width=w, height=h))

def CGGetActiveDisplayList(cap, ids, count):
    got = sorted(DISPLAYS)[:cap]
    return (0, got, len(got))

def CGDisplayBounds(did):
    return _bounds(*DISPLAYS[did])

Q.CGGetActiveDisplayList = CGGetActiveDisplayList
Q.CGDisplayBounds = CGDisplayBounds
Q.kCGWindowListOptionOnScreenOnly = 1
Q.kCGWindowListExcludeDesktopElements = 16
Q.kCGNullWindowID = 0

def win(app, pid, layer, x, y, w, h, name="", alpha=1.0):
    return {"kCGWindowOwnerName": app, "kCGWindowOwnerPID": pid,
            "kCGWindowLayer": layer, "kCGWindowName": name, "kCGWindowAlpha": alpha,
            "kCGWindowBounds": {"X": x, "Y": y, "Width": w, "Height": h}}

# Front to back, the way the real call returns it: overlays first, then layer 0.
SCENE = [
    win("Control Center", 804, 25, 1400, 0, 48, 30),
    win("Dock", 801, 20, 0, 0, 1728, 1117),
    win("Notes", 100, 3, 900, 420, 260, 200, "tooltip"),      # popover over Notes
    win("Notes", 100, 0, 200, 100, 900, 700, "Grocery list"), # the focused window
    win("Notes", 100, 0, 1300, 950, 300, 140, "Scratch"),     # parked elsewhere
    win("Mail", 200, 0, 40, 40, 600, 400, "Inbox"),
    win("Notes", 100, 0, 300, 300, 20, 20, "shadow"),         # too small to count
    win("Window Server", 421, 2147483630, 0, 0, 1728, 25),
]
WINDOWS = list(SCENE)
FRONT_PID = [100]

def CGWindowListCopyWindowInfo(opts, wid):
    return list(WINDOWS)

Q.CGWindowListCopyWindowInfo = CGWindowListCopyWindowInfo

def set_windows(wins=None, front_pid=100):
    WINDOWS[:] = list(SCENE if wins is None else wins)
    FRONT_PID[0] = front_pid

# ---- fake AppKit (frontmost application) ---------------------------------
ak = types.ModuleType("AppKit")
class _App:
    def processIdentifier(self): return FRONT_PID[0]
class _WS:
    def frontmostApplication(self): return _App() if FRONT_PID[0] else None
ak.NSWorkspace = SimpleNamespace(sharedWorkspace=lambda: _WS())
sys.modules["AppKit"] = ak


# ==========================================================================
# fake X11: enough Xlib for x11.py to run headlessly
# ==========================================================================
os_environ_display = "1"
import os as _os
_os.environ.setdefault("DISPLAY", ":99")

XI = types.ModuleType("Xlib")
XX = types.ModuleType("Xlib.X")
for _n, _v in dict(IsViewable=2, IsUnmapped=0, InputOutput=1, InputOnly=2,
                   AnyPropertyType=0, ZPixmap=2, MotionNotify=6, ButtonPress=4,
                   ButtonRelease=5, CurrentTime=0, NONE=0).items():
    setattr(XX, _n, _v)

XE = types.ModuleType("Xlib.error")
class XError(Exception): pass
class BadWindow(XError): pass
XE.XError, XE.BadWindow = XError, BadWindow

XTEST = types.ModuleType("Xlib.ext.xtest")
def fake_input(disp, event_type, detail=0, time=0, root=0, x=0, y=0):
    if event_type == XX.MotionNotify:
        STATE["pos"] = (float(x), float(y))
        EVENTS.append({"type": "x11.motion", "pos": (float(x), float(y))})
    elif event_type == XX.ButtonPress:
        EVENTS.append({"type": "x11.press", "button": detail, "pos": STATE["pos"]})
    elif event_type == XX.ButtonRelease:
        EVENTS.append({"type": "x11.release", "button": detail, "pos": STATE["pos"]})
XTEST.fake_input = fake_input

# ---- atoms ----
_ATOMS, _ATOM_NAMES = {}, {}
def _atom(name):
    if name not in _ATOMS:
        i = len(_ATOMS) + 100
        _ATOMS[name] = i; _ATOM_NAMES[i] = name
    return _ATOMS[name]

class FakeWin:
    def __init__(self, wid, x=0, y=0, w=0, h=0, app="", title="", pid=0,
                 override=False, types_=(), states=(), viewable=True,
                 win_class=None, children=(), client=False):
        self.id = wid
        self.geom = (x, y, w, h)
        self.app, self.title, self.pid = app, title, pid
        self.override = override
        self.types, self.states = list(types_), list(states)
        self.viewable = viewable
        self.win_class = XX.InputOutput if win_class is None else win_class
        self.children = list(children)
        self.client = client

    # -- attributes / geometry / tree --
    def get_attributes(self):
        return SimpleNamespace(map_state=XX.IsViewable if self.viewable else XX.IsUnmapped,
                               win_class=self.win_class,
                               override_redirect=self.override)
    def get_geometry(self):
        x, y, w, h = self.geom
        return SimpleNamespace(x=x, y=y, width=w, height=h, depth=24)
    def query_tree(self):
        return SimpleNamespace(children=list(self.children))

    # -- properties --
    def _props(self):
        p = {}
        if self.client:
            p["WM_STATE"] = [1, 0]
        if self.pid:
            p["_NET_WM_PID"] = [self.pid]
        if self.title:
            p["_NET_WM_NAME"] = self.title.encode()
        if self.types:
            p["_NET_WM_WINDOW_TYPE"] = [_atom(t) for t in self.types]
        if self.states:
            p["_NET_WM_STATE"] = [_atom(s) for s in self.states]
        return p
    def get_full_property(self, atom, kind):
        name = _ATOM_NAMES.get(atom)
        v = self._props().get(name)
        return SimpleNamespace(value=v) if v is not None else None
    def get_wm_class(self):
        return (self.app.lower(), self.app) if self.app else None

    # -- pixels --
    def get_image(self, x, y, w, h, fmt, mask):
        # BGRX, with the blue and green channels encoding the absolute screen
        # coordinate so a test can prove which rectangle came back.
        buf = bytearray()
        for j in range(h):
            for i in range(w):
                buf += bytes(((x + i) % 256, (y + j) % 256, 0, 255))
        return SimpleNamespace(data=bytes(buf))

    def query_pointer(self):
        return SimpleNamespace(root_x=STATE["pos"][0], root_y=STATE["pos"][1])

    def xrandr_get_monitors(self):
        return SimpleNamespace(monitors=[
            SimpleNamespace(x=0, y=0, width_in_pixels=1728, height_in_pixels=1117, primary=True),
            SimpleNamespace(x=1728, y=0, width_in_pixels=1440, height_in_pixels=900, primary=False),
        ])

def x11_scene():
    """Front-to-back the same desktop the macOS fake describes, X11-flavoured."""
    notes = FakeWin(11, 200, 100, 900, 700, "Notes", "Grocery list", pid=100, client=True)
    frame = FakeWin(10, 200, 100, 900, 700, children=[notes])   # reparenting WM frame
    return [                                                     # bottom to top
        FakeWin(1, 0, 0, 1728, 1117, "Xfdesktop", types_=("_NET_WM_WINDOW_TYPE_DESKTOP",), client=True),
        FakeWin(2, 0, 1080, 1728, 37, "Xfce4-panel", types_=("_NET_WM_WINDOW_TYPE_DOCK",), client=True),
        FakeWin(3, 40, 40, 600, 400, "Mail", "Inbox", pid=200, client=True),
        FakeWin(4, 1300, 950, 300, 140, "Notes", "Scratch", pid=100, client=True),
        frame,
        FakeWin(6, 300, 300, 20, 20, "Notes", "shadow", pid=100, client=True),
        FakeWin(7, 900, 420, 260, 200, "Notes", "", override=True,      # a menu: no pid
                types_=("_NET_WM_WINDOW_TYPE_POPUP_MENU",), client=True),
    ]

class FakeDisplay:
    def __init__(self, *a, **k):
        self.root = FakeWin(0, 0, 0, 1728, 1117, children=x11_scene())
        self.root.client = False
        self.active = 11
    def screen(self):
        return SimpleNamespace(root=self.root)
    def sync(self): pass
    def flush(self): pass
    def query_extension(self, name): return True
    def intern_atom(self, name): return _atom(name)
    def get_atom_name(self, atom): return _ATOM_NAMES.get(atom, "")
    def create_resource_object(self, kind, wid):
        return _find(self.root, wid) or FakeWin(wid)

def _find(w, wid):
    if w.id == wid: return w
    for c in w.children:
        got = _find(c, wid)
        if got: return got
    return None

# The root answers _NET_ACTIVE_WINDOW with whichever window X11_ACTIVE names.
X11_ACTIVE = [11]
_root_props = FakeWin._props
def _props_with_active(self):
    p = _root_props(self)
    if self.id == 0:
        p["_NET_ACTIVE_WINDOW"] = [X11_ACTIVE[0]]
    return p
FakeWin._props = _props_with_active

XD = types.ModuleType("Xlib.display")
XD.Display = FakeDisplay
XEXT = types.ModuleType("Xlib.ext")
XEXT.xtest = XTEST
XI.X, XI.display, XI.error, XI.ext = XX, XD, XE, XEXT
for _name, _mod in [("Xlib", XI), ("Xlib.X", XX), ("Xlib.display", XD),
                    ("Xlib.error", XE), ("Xlib.ext", XEXT), ("Xlib.ext.xtest", XTEST)]:
    sys.modules[_name] = _mod
