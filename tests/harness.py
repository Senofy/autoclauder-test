import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fakes                                   # must import first: installs the stubs
import tempfile, pathlib
import sys, json, math, random
from types import SimpleNamespace  # noqa: F401  -- used by the fakes above
from PIL import Image

import motion as mo
from motion import MotionProfile, path
import desktop
import mac
import win32
import x11
import window as wn
from desktop import Desktop, FailSafeAbort
from window import Rect, WindowTarget

# A stand-in for `screencapture`: a 2x Retina grab of a 1728x1117 display, with
# two marker pixels so a test can prove which rectangle was cropped out of it.
RED, GREEN = (255, 0, 0), (0, 255, 0)
NATIVE = Image.new("RGB", (3456, 2234), (30, 30, 40))
NATIVE.paste(RED, (400, 200, 402, 202))        # logical (200, 100): the Notes corner
NATIVE.paste(GREEN, (2318, 1598, 2320, 1600))  # logical (1159, 799): its far corner
mac.Backend._grab = lambda self, i=1: NATIVE
win32.ImageGrab = SimpleNamespace(grab=fakes.win_grab)   # stands in for PIL's
BOUNDS = (1728, 1117)

fails = []
def ck(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond: fails.append(msg)

BACKENDS = {"mac": mac.Backend, "x11": x11.Backend, "win": win32.Backend}

def newdesk(seed=5, window=None, backend="mac", **kw):
    fakes.reset()
    be = BACKENDS[backend]()
    d = Desktop(motion=MotionProfile(**kw), rng=random.Random(seed), window=window,
                backend=be)
    d.screenshot_b64()
    return d

def png_size(b64):
    import base64, io as _io
    return Image.open(_io.BytesIO(base64.b64decode(b64))).size

def moves(kinds=("kCGEventMouseMoved", "x11.motion")):
    return [e for e in fakes.EVENTS if e.get("type") in kinds]

def kinds(*names):
    return [e for e in fakes.EVENTS if e.get("type") in names]

# ===========================================================================
print("\n== motion geometry ==")
p = path((100.0, 100.0), (1200.0, 700.0), MotionProfile(), random.Random(3), bounds=BOUNDS)
ck(len(p) > 20, f"long move is interpolated, not a teleport ({len(p)} samples)")
ck(p[-1][0] == 1200.0 and p[-1][1] == 700.0, "lands exactly on the target")

chord = math.hypot(1100.0, 600.0)
arc = sum(math.hypot(p[i][0]-p[i-1][0], p[i][1]-p[i-1][1]) for i in range(1, len(p)))
ck(1.001 < arc/chord < 1.15, f"arcs off the straight line but not wildly (arc/chord {arc/chord:.4f})")

dev = max(abs((1200.0-100.0)*(y-100.0) - (x-100.0)*(700.0-100.0))/chord for x, y, _ in p)
ck(20 < dev < 0.20*chord, f"peak perpendicular bow {dev:.1f}pt on a {chord:.0f}pt throw")

gaps = [math.hypot(p[i][0]-p[i-1][0], p[i][1]-p[i-1][1]) for i in range(1, len(p))]
third = len(gaps)//3
mid = sum(gaps[third:2*third])/third
ends = (sum(gaps[:4]) + sum(gaps[-4:]))/8
ck(mid > ends*1.8, f"accelerates then decelerates (mid step {mid:.1f} vs ends {ends:.1f})")

ck(all(math.isfinite(x) and math.isfinite(y) for x, y, _ in p), "no NaN/inf samples")
ck(all(dt > 0 for *_ , dt in p), "every sample has a positive delay")
dur = sum(s[2] for s in p)
ck(0.15 < dur < 1.6, f"a screen-crossing move takes {dur*1000:.0f}ms")

print("\n== randomness and determinism ==")
a = path((0.,0.), (900.,500.), MotionProfile(), random.Random(42), bounds=BOUNDS)
b = path((0.,0.), (900.,500.), MotionProfile(), random.Random(42), bounds=BOUNDS)
c = path((0.,0.), (900.,500.), MotionProfile(), random.Random(43), bounds=BOUNDS)
ck(a == b, "same seed reproduces the same path")
ck(a != c, "different seed gives a different path")
straight = path((0.,0.), (900.,500.), MotionProfile(curvature=0, tremor=0), random.Random(1), bounds=BOUNDS)
off = max(abs(500.0*x - 900.0*y)/math.hypot(900.,500.) for x, y, _ in straight)
ck(off < 0.01, f"--curvature 0 --tremor 0 gives a dead straight line ({off:.4f}pt off)")
inst = path((0.,0.), (900.,500.), MotionProfile(enabled=False), random.Random(1))
ck(inst == [(900.0, 500.0, 0.0)], "--motion instant is a single teleport sample")

seen = [len(path((0.,0.), (900.,500.), MotionProfile(), random.Random(s), bounds=BOUNDS))
        for s in range(60)]
ck(len(set(seen)) > 10, f"path length varies run to run ({len(set(seen))} distinct)")

print("\n== bounds / hot corners ==")
esc = 0
for s in range(200):
    q = path((1700.,1100.), (20.,20.), MotionProfile(curvature=6.0), random.Random(s), bounds=BOUNDS)
    if any(x < 0 or y < 0 or x > 1727 or y > 1116 for x, y, _ in q):
        esc += 1
    if q and (q[-1][0], q[-1][1]) != (20.0, 20.0):
        esc += 100
ck(esc == 0, f"200 extreme-curvature corner-bound moves never leave the screen ({esc})")

print("\n== event stream ==")
d = newdesk()
d.run("left_click", {"coordinate": [1288, 800]})
mv = moves()
ck(len(mv) > 15, f"a click glides there ({len(mv)} move events)")
downs = [e for e in fakes.EVENTS if e.get("type") == "kCGEventLeftMouseDown"]
ups = [e for e in fakes.EVENTS if e.get("type") == "kCGEventLeftMouseUp"]
ck(len(downs) == 1 and len(ups) == 1, "single click -> one down, one up")
ck(downs[0]["kCGMouseEventClickState"] == 1, "click state 1")
tgt = d.to_logical([1288, 800])
ck(math.hypot(mv[-1]["pos"][0]-tgt[0], mv[-1]["pos"][1]-tgt[1]) < 0.01, "final move lands on target")

d = newdesk()
d.run("double_click", {"coordinate": [600, 400]})
downs = [e for e in fakes.EVENTS if e.get("type") == "kCGEventLeftMouseDown"]
ck([e["kCGMouseEventClickState"] for e in downs] == [1, 2],
   "double click sets clickState 1 then 2 (pyautogui never does this)")
d = newdesk()
d.run("triple_click", {"coordinate": [600, 400]})
downs = [e for e in fakes.EVENTS if e.get("type") == "kCGEventLeftMouseDown"]
ck([e["kCGMouseEventClickState"] for e in downs] == [1, 2, 3], "triple click reaches state 3")

d = newdesk()
d.run("left_click_drag", {"start_coordinate": [200, 200], "coordinate": [1000, 600]})
seq = [e["type"] for e in fakes.EVENTS]
dragged = [e for e in fakes.EVENTS if e["type"] == "kCGEventLeftMouseDragged"]
di, ui = seq.index("kCGEventLeftMouseDown"), len(seq) - 1 - seq[::-1].index("kCGEventLeftMouseUp")
inner = [t for t in seq[di+1:ui]]
ck(len(dragged) > 15, f"drag emits a stream of Dragged events ({len(dragged)})")
ck("kCGEventMouseMoved" not in inner, "no plain MouseMoved between down and up")

d = newdesk()
d.run("scroll", {"coordinate": [800, 500], "scroll_direction": "down", "scroll_amount": 3})
sc = [e for e in fakes.EVENTS if e.get("type") == "scroll"]
ck(len(sc) == 6 and all(e["v"] == -1 for e in sc), f"scroll down = 6 one-notch events ({len(sc)})")
d = newdesk(); d.run("scroll", {"scroll_direction": "right", "scroll_amount": 1})
sc = [e for e in fakes.EVENTS if e.get("type") == "scroll"]
ck(all(e["h"] == 1 and e["v"] == 0 for e in sc), "horizontal scroll uses the h axis")

print("\n== typing rhythm ==")
d = newdesk()
d.run("type", {"text": "Hello there, friend."})
w = [k for k in fakes.KEYS if k[0] == "write"]
ck(len(w) == 20, f"types character by character ({len(w)})")
delays = [s for s in fakes.SLEPT if 0 < s < 1]
ck(len(set(round(x, 4) for x in delays)) > 10, "inter-key delays are all different")
ck(max(delays) > min(delays) * 2.5, f"rhythm is uneven ({min(delays)*1000:.0f}-{max(delays)*1000:.0f}ms)")
d = newdesk()
CLIP = {"v": "previous clipboard"}                    # no pbcopy on this box
d.backend.clip_read = lambda: CLIP["v"]
d.backend.clip_write = lambda t: CLIP.__setitem__("v", t)
d.run("type", {"text": "x" * 400})
ck(not [k for k in fakes.KEYS if k[0] == "write"] and
   [k for k in fakes.KEYS if k[0] == "hotkey"], "long text still pastes instead of typing")
ck(CLIP["v"] == "previous clipboard", "the user's clipboard is put back after a paste")

print("\n== failsafe ==")
d = newdesk()
d._commanded = (500.0, 500.0)
fakes.STATE["pos"] = (0.0, 0.0)                       # a hand yanked it to the corner
try:
    d._guard(); ck(False, "corner + unexpected position aborts")
except FailSafeAbort:
    ck(True, "corner + unexpected position aborts")
d._commanded = (0.0, 0.0)                             # we put it there on purpose
try:
    d._guard(); ck(True, "a deliberate corner click does not abort")
except FailSafeAbort:
    ck(False, "a deliberate corner click does not abort")

print("\n== env file ==")
import env as envmod
E = envmod.parse("""
# a comment, and a blank line follow

export ANTHROPIC_API_KEY=sk-ant-123   # trailing note
CLAUDE_DISPLAY = 2
QUOTED="  keeps  its  spaces  "
HASH='a#b'
EMPTY=
not a key=skipped
9NOPE=skipped
noequalshere
""")
ck(E["ANTHROPIC_API_KEY"] == "sk-ant-123", "export, comments and trailing notes are stripped")
ck(E["CLAUDE_DISPLAY"] == "2", "space either side of = is fine")
ck(E["QUOTED"] == "  keeps  its  spaces  ", "quotes preserve whitespace")
ck(E["HASH"] == "a#b", "a # inside quotes is not a comment")
ck(E["EMPTY"] == "", "an empty value is a value")
ck(set(E) == {"ANTHROPIC_API_KEY", "CLAUDE_DISPLAY", "QUOTED", "HASH", "EMPTY"},
   f"malformed lines are skipped, not guessed at ({sorted(E)})")

import tempfile, pathlib
tmp = pathlib.Path(tempfile.mkdtemp()) / "x.env"
tmp.write_text("CLAUDE_TEST_A=fromfile\nCLAUDE_TEST_B=fromfile\n")
os.environ["CLAUDE_TEST_A"] = "fromshell"
os.environ.pop("CLAUDE_TEST_B", None)
path, applied = envmod.load(tmp)
ck(path == tmp and applied == 1, f"only the unset variable is applied ({applied})")
ck(os.environ["CLAUDE_TEST_A"] == "fromshell", "the real environment wins over the file")
ck(os.environ["CLAUDE_TEST_B"] == "fromfile", "and the file fills in the rest")
envmod.load(tmp, override=True)
ck(os.environ["CLAUDE_TEST_A"] == "fromfile", "override=True flips that round")
try:
    envmod.load(tmp.parent / "nothing-here.env")
    ck(False, "naming a file that does not exist is an error")
except RuntimeError:
    ck(True, "naming a file that does not exist is an error")
for k in ("CLAUDE_TEST_A", "CLAUDE_TEST_B"):
    os.environ.pop(k, None)

d = newdesk()
ck(d.display == 1, "no CLAUDE_DISPLAY means display 1")
os.environ["CLAUDE_DISPLAY"] = "2"
ck(newdesk().display == 2, "CLAUDE_DISPLAY is read when the Desktop is built, not at import")
os.environ["CLAUDE_DISPLAY"] = "nonsense"
ck(newdesk().display == 1, "and a junk value falls back to 1 rather than crashing")
os.environ.pop("CLAUDE_DISPLAY")

print("\n== image limits ==")
import math as _math
_fit = Desktop._fit
class _JustFit:                      # only _fit is needed, not a whole Desktop
    _fit = _fit
for _w, _h in ((2690, 1855), (7680, 2160), (2576, 1776), (3000, 3000), (5000, 300)):
    _out = _JustFit()._fit(Image.new("RGB", (_w, _h))).size
    _tok = _out[0] * _out[1] / desktop.PIXELS_PER_TOKEN
    ck(max(_out) <= desktop.MAX_EDGE and _tok <= desktop.MAX_IMAGE_TOKENS,
       f"{_w}x{_h} fits both limits as {_out[0]}x{_out[1]} ({_tok:.0f} tokens)")
ck(_JustFit()._fit(Image.new("RGB", (960, 700))).size == (960, 700),
   "and a picture already inside both is left alone")
ck(_JustFit()._fit(Image.new("RGB", (2576, 1776))).size != (2576, 1776),
   "a shot inside the edge limit but over the token limit is still shrunk")

print("\n== capture scope ==")
MAC = mac.Backend()
D = MAC.display_rect(1)
ck(D == Rect(0.0, 0.0, 1728.0, 1117.0), f"display 1 bounds {D}")
ck(MAC.display_rect(2).x == 1728.0, "a second display is offset, not another (0,0)")

usable = [w for w in MAC.list_windows() if w.usable]
apps = [w.app for w in usable]
ck("Dock" not in apps and "Window Server" not in apps and "Control Center" not in apps,
   f"desktop furniture is filtered out ({sorted(set(apps))})")
ck(all(w.rect.w >= 32 and w.rect.h >= 32 for w in usable),
   "shadow-sized helper windows are filtered out")

rect, label = WindowTarget().resolve(MAC, D)
ck(label == "Notes - Grocery list", f"anchors on the focused app's real window ({label})")
ck(rect == Rect(200.0, 100.0, 960.0, 700.0),
   f"takes in the popover overlapping it, not the window parked elsewhere ({rect})")

fakes.set_windows([fakes.win("Spotlight", 900, 23, 500, 300, 680, 66), *fakes.SCENE],
                  front_pid=100)
ck(WindowTarget().resolve(MAC, D)[1] == "Spotlight",
   "a system panel over everything takes the capture (Spotlight)")
fakes.set_windows(front_pid=200)
ck(WindowTarget().resolve(MAC, D)[1] == "Mail - Inbox",
   "an app's own low-layer popover does not outrank the frontmost app")
fakes.set_windows(front_pid=0)
ck(WindowTarget().resolve(MAC, D)[1] == "Notes - Grocery list",
   "no NSWorkspace answer falls back to z-order")
fakes.set_windows()

ck(WindowTarget(app="mail").resolve(MAC, D)[1] == "Mail - Inbox", "--window-app pins to an app")
ck(WindowTarget(app="Xcode").resolve(MAC, D) is None, "an app that is not on screen resolves to nothing")
ck(WindowTarget(app="mail", padding=100).resolve(MAC, D)[0] == Rect(0.0, 0.0, 740.0, 540.0),
   "--window-padding expands the rect and clamps it to the display")

print("\n== window capture ==")
d = newdesk(window=WindowTarget())
f = d._frame
ck(f.origin == (200.0, 100.0), f"frame origin is the window corner {f.origin}")
ck((f.width, f.height) == (1920, 1400), f"sends the window, not the screen ({f.width}x{f.height})")
ck(abs(f.scale - 0.5) < 1e-9, f"scale still maps model px to points ({f.scale})")
ck("Notes" in d.view and "960x700 pt" in d.view, f"view is described for the log ({d.view})")

ck(d.to_logical([0, 0]) == (200.0, 100.0), "model origin is the window's top-left")
ck(d.to_model(250.0, 200.0) == (100, 200), "and back again")
d.run("left_click", {"coordinate": [100, 200]})
last = moves()[-1]["pos"]
ck(abs(last[0] - 250.0) < 0.01 and abs(last[1] - 200.0) < 0.01,
   f"a click inside the crop lands on the real screen point {last}")
fakes.STATE["pos"] = (250.0, 200.0)
ck(d.run("cursor_position", {})[0]["text"] == "[100, 200]", "cursor_position reports window space")
ck(png_size(d.zoom_b64([0, 0, 100, 50])) == (100, 50),
   "zoom crops through the window origin, not the old full-frame ratio")

res = d.run("screenshot", {})
ck(res[0]["type"] == "text" and "Notes" in res[0]["text"] and res[1]["type"] == "image",
   "a window screenshot tells Claude which window it is")
ck(png_size(res[1]["source"]["data"])[0] == 1920, "and carries the cropped image")
import base64 as _b64, io as _io
shot = Image.open(_io.BytesIO(_b64.b64decode(res[1]["source"]["data"])))
ck(shot.getpixel((0, 0)) == RED and shot.getpixel((1919, 1399)) == GREEN,
   "the cropped pixels are the window's, corner to corner")

full = newdesk()
ck(full._frame.origin == (0.0, 0.0), "full-screen mode still starts at the origin")
ck(abs(full._frame.scale - 1728 / full._frame.width) < 1e-9,
   "and its scale still maps the whole display across the image")
ck(full._frame.width * full._frame.height / desktop.PIXELS_PER_TOKEN
   <= desktop.MAX_IMAGE_TOKENS,
   f"a full-screen Retina shot is shrunk to fit the token limit "
   f"({full._frame.width}x{full._frame.height})")
ck([b["type"] for b in full.run("screenshot", {})] == ["image"],
   "full-screen result is still a bare image block")

miss = newdesk(window=WindowTarget(app="Xcode"))
ck(miss._frame.origin == (0.0, 0.0), "an absent app falls back to the whole display")
ck("full screen" in miss.view and "not on screen" in miss.view,
   f"and says why ({miss.view})")

print("\n== linux backend (X11) ==")
fakes.reset()
XB = x11.Backend()
ck(XB.display_rect(1) == Rect(0.0, 0.0, 1728.0, 1117.0), "randr monitors give display 1")
ck(XB.display_rect(2) == Rect(1728.0, 0.0, 1440.0, 900.0), "and display 2, offset")

xwins = [w for w in XB.list_windows() if w.usable]
xapps = [w.app for w in xwins]
ck("Xfce4-panel" not in xapps and "Xfdesktop" not in xapps,
   f"_NET_WM_WINDOW_TYPE_DOCK and _DESKTOP are furniture ({sorted(set(xapps))})")
notes = next(w for w in xwins if w.title == "Grocery list")
ck(notes.pid == 100, "pid is read through the window manager's frame (WM_STATE walk)")
ck(notes.rect == Rect(200.0, 100.0, 900.0, 700.0), "geometry of a root child is already root-relative")
popup = next(w for w in xwins if w.layer >= wn.OVERLAY_LAYER)
ck(popup.pid == 0 and popup.rect == Rect(900.0, 420.0, 260.0, 200.0),
   "an override-redirect popup is an overlay, and X will not say whose")
ck(XB.frontmost_pid() == 100, "_NET_ACTIVE_WINDOW -> _NET_WM_PID")

xrect, xlabel = WindowTarget().resolve(XB, XB.display_rect(1))
ck((xrect, xlabel) == (rect, label),
   f"the same desktop crops the same on both platforms ({xlabel} {xrect})")

d = newdesk(window=WindowTarget(), backend="x11")
f = d._frame
ck(f.origin == (200.0, 100.0) and (f.width, f.height) == (960, 700) and f.scale == 1.0,
   f"X11 has no Retina factor: 1 model px is 1 point ({f.width}x{f.height} @ {f.scale})")
px = Image.open(_io.BytesIO(_b64.b64decode(d.screenshot_b64()))).getpixel((0, 0))
ck(px == (0, 100, 200), f"get_image read the window's rectangle, not the screen's ({px})")

d = newdesk(window=WindowTarget(), backend="x11")
d.run("left_click", {"coordinate": [100, 200]})
ck(len(kinds("x11.motion")) > 15, "a click glides there on X11 too")
ck([e["button"] for e in kinds("x11.press", "x11.release")] == [1, 1],
   "left click is button 1, pressed once")
ck(kinds("x11.press")[0]["pos"] == (300.0, 300.0), "and it happens at the window-relative point")

d = newdesk(window=WindowTarget(), backend="x11")
d.run("right_click", {"coordinate": [100, 200]})
ck([e["button"] for e in kinds("x11.press")] == [3],
   "right click is button 3 (X numbers them the other way round from Quartz)")

d = newdesk(window=WindowTarget(), backend="x11")
d.run("double_click", {"coordinate": [100, 200]})
ck([e["type"] for e in kinds("x11.press", "x11.release")]
   == ["x11.press", "x11.release", "x11.press", "x11.release"],
   "double click is two press/release pairs -- X has no click-count field")
ck(max(fakes.SLEPT) < 0.4,
   f"every gap stays under the toolkit double-click threshold ({max(fakes.SLEPT)*1000:.0f}ms)")

d = newdesk(window=WindowTarget(), backend="x11")
d.run("left_click_drag", {"start_coordinate": [10, 10], "coordinate": [400, 300]})
seq = [e["type"] for e in kinds("x11.press", "x11.release", "x11.motion")]
lo, hi = seq.index("x11.press"), len(seq) - 1 - seq[::-1].index("x11.release")
ck("x11.release" not in seq[lo:hi], "the button stays down for the whole drag")
ck(seq[lo:hi].count("x11.motion") > 15, f"and it drags through {seq[lo:hi].count('x11.motion')} positions")

d = newdesk(window=WindowTarget(), backend="x11")
d.run("scroll", {"scroll_direction": "down", "scroll_amount": 3})
ck([e["button"] for e in kinds("x11.press")] == [5] * 6, "scroll down is six button-5 clicks")
d = newdesk(window=WindowTarget(), backend="x11")
d.run("scroll", {"scroll_direction": "right", "scroll_amount": 1})
ck({e["button"] for e in kinds("x11.press")} == {7}, "horizontal scroll is button 7")

ck(d.combo("super+c") == ["winleft", "c"], "on Linux `super` is the Super key, not Command")
ck(d.combo("alt+Tab") == ["alt", "tab"], "and `alt` is alt, not option")
ck(d.backend.paste_combo == ("ctrl", "v"), "long text pastes with ctrl+v")
CLIP2 = {"v": "before"}
d.backend.clip_read = lambda: CLIP2["v"]
d.backend.clip_write = lambda t: CLIP2.__setitem__("v", t)
fakes.reset()
d.run("type", {"text": "y" * 400})
ck([k for k in fakes.KEYS if k[0] == "hotkey"][0][1] == ("ctrl", "v"),
   "and it really presses ctrl+v")
ck(CLIP2["v"] == "before", "the clipboard is put back afterwards")

fakes.STATE["pos"] = (300.0, 300.0)
ck(d.run("cursor_position", {})[0]["text"] == "[100, 200]", "cursor_position is window space here too")

print("\n== backend interface ==")
import backend as bemod
for _plat, _cls in (("macOS", mac.Backend), ("X11", x11.Backend), ("Windows", win32.Backend)):
    fakes.reset()
    inst = _cls()
    ck(isinstance(inst, bemod.Backend), f"{_plat} satisfies the Backend protocol")
    ck(bemod.check_interface(inst) == [],
       f"{_plat} signatures match: {bemod.check_interface(inst)}")

class Drifted(win32.Backend):
    def capture(self, display_index):            # a parameter quietly dropped
        raise NotImplementedError
fakes.reset()
ck(any("capture" in p for p in bemod.check_interface(Drifted())),
   "and a signature that drifts on one platform is caught here, not at run time")

print("\n== windows backend ==")
fakes.reset()
WB = win32.Backend()
ck(WB.dpi != "", f"DPI awareness is claimed at startup ({WB.dpi})")
ck(WB.warning == "", "and no warning when it succeeds")
ck(WB.display_rect(1) == Rect(0.0, 0.0, 1728.0, 1117.0), "EnumDisplayMonitors gives display 1")
ck(WB.display_rect(2) == Rect(1728.0, 0.0, 1440.0, 900.0), "and display 2, offset")

wwins = [w for w in WB.list_windows() if w.usable]
wapps = [w.app for w in wwins]
ck("explorer" not in wapps, f"taskbar and Progman are furniture ({sorted(set(wapps))})")
ck("uwp" not in wapps, "a cloaked window on another virtual desktop is skipped")
ck(wapps[0] == "Notes", "app name comes from the process image, without the .exe")
wnotes = next(w for w in wwins if w.title == "Grocery list")
ck(wnotes.rect == Rect(200.0, 100.0, 900.0, 700.0),
   f"DWM extended frame bounds, not GetWindowRect's invisible border ({wnotes.rect})")
wmenu = next(w for w in wwins if w.layer >= wn.OVERLAY_LAYER)
ck(wmenu.rect == Rect(900.0, 420.0, 260.0, 200.0), "a topmost menu class counts as an overlay")
ck(WB.frontmost_pid() == 100, "GetForegroundWindow -> pid")

wrect, wlabel = WindowTarget().resolve(WB, WB.display_rect(1))
ck((wrect, wlabel) == (rect, label),
   f"all three platforms crop the same desktop identically ({wlabel} {wrect})")

d = newdesk(window=WindowTarget(), backend="win")
f = d._frame
ck(f.origin == (200.0, 100.0) and (f.width, f.height) == (960, 700) and f.scale == 1.0,
   f"DPI-aware, so 1 model px is 1 point ({f.width}x{f.height} @ {f.scale})")
px = Image.open(_io.BytesIO(_b64.b64decode(d.screenshot_b64()))).getpixel((0, 0))
ck(px == (0, 100, 200), f"the grab covered the window's rectangle ({px})")

d = newdesk(window=WindowTarget(), backend="win")
d.run("left_click", {"coordinate": [100, 200]})
ck(len(kinds("win.move")) > 15, "a click glides there on Windows too")
ck([e["button"] for e in kinds("win.press", "win.release")] == ["left", "left"],
   "left click is one press and one release")
land = kinds("win.press")[0]["pos"]
ck(abs(land[0] - 300.0) < 0.05 and abs(land[1] - 300.0) < 0.05,
   f"absolute 0-65535 coordinates round-trip to the right point {land}")

d = newdesk(window=WindowTarget(), backend="win")
d.run("right_click", {"coordinate": [100, 200]})
ck([e["button"] for e in kinds("win.press")] == ["right"], "right click uses the right button")

d = newdesk(window=WindowTarget(), backend="win")
d.run("double_click", {"coordinate": [100, 200]})
ck([e["type"] for e in kinds("win.press", "win.release")]
   == ["win.press", "win.release", "win.press", "win.release"],
   "double click is two press/release pairs -- Windows infers it from timing")
ck(max(fakes.SLEPT) < 0.5,
   f"and the gap stays under GetDoubleClickTime's 500ms default ({max(fakes.SLEPT)*1000:.0f}ms)")

d = newdesk(window=WindowTarget(), backend="win")
d.run("left_click_drag", {"start_coordinate": [10, 10], "coordinate": [400, 300]})
wseq = [e["type"] for e in kinds("win.press", "win.release", "win.move")]
lo, hi = wseq.index("win.press"), len(wseq) - 1 - wseq[::-1].index("win.release")
ck("win.release" not in wseq[lo:hi], "the button stays down for the whole drag")
ck(wseq[lo:hi].count("win.move") > 15, f"dragging through {wseq[lo:hi].count('win.move')} positions")

d = newdesk(window=WindowTarget(), backend="win")
d.run("scroll", {"scroll_direction": "down", "scroll_amount": 3})
ck([e["delta"] for e in kinds("win.wheel")] == [-120] * 6,
   "scroll down is six notches of -WHEEL_DELTA")
d = newdesk(window=WindowTarget(), backend="win")
d.run("scroll", {"scroll_direction": "right", "scroll_amount": 1})
ck({(e["axis"], e["delta"]) for e in kinds("win.wheel")} == {("h", 120)},
   "horizontal scroll uses the h axis")

ck(d.combo("super+c") == ["win", "c"], "on Windows `super` is the Windows key")
ck(d.backend.paste_combo == ("ctrl", "v"), "and paste is ctrl+v")
d.backend.clip_write("before \u00fcnicode")
ck(d.backend.clip_read() == "before \u00fcnicode",
   "clipboard round-trips through GlobalAlloc and CF_UNICODETEXT")
fakes.reset()
d.run("type", {"text": "z" * 400})
ck([k for k in fakes.KEYS if k[0] == "hotkey"][0][1] == ("ctrl", "v"),
   "long text pastes rather than types")
ck(d.backend.clip_read() == "before \u00fcnicode", "and the clipboard is put back")

print("\n== compiled programs ==")
import program as prog
from program import Anchor, Program, ProgramError, ReplayMiss, Runner, Step

W = Rect(200.0, 100.0, 900.0, 700.0)          # the Notes window in every fake
ck(Anchor.of((230, 140), W, "Notes").corner == "tl", "a point near the top-left anchors tl")
ck(Anchor.of((1070, 140), W, "Notes").corner == "tr", "near the top-right, tr")
ck(Anchor.of((230, 770), W, "Notes").corner == "bl", "near the bottom-left, bl")
ck(Anchor.of((1070, 770), W, "Notes").corner == "br", "near the bottom-right, br")

send = Anchor.of((1070, 770), W, "Notes")     # a Send button, bottom right
ck(send.point(W) == (1070.0, 770.0), "an anchor round-trips exactly")
ck(send.point(Rect(500.0, 300.0, 900.0, 700.0)) == (1370.0, 970.0),
   "a moved window carries the point with it")
ck(send.point(Rect(200.0, 100.0, 700.0, 500.0)) == (870.0, 570.0),
   "a resized window keeps a bottom-right point against the bottom-right")
side = Anchor.of((230, 140), W, "Notes")
ck(side.point(Rect(200.0, 100.0, 700.0, 500.0)) == (230.0, 140.0),
   "while a top-left point stays where it was")
tiny = Rect(200.0, 100.0, 40.0, 40.0)
px, py = side.point(tiny)
ck(200 <= px < 240 and 100 <= py < 140, f"a window smaller than the offset clamps inside it ({px},{py})")
ck(send.resized(Rect(200.0, 100.0, 700.0, 500.0)) == (-200.0, -200.0), "resize is reported")

base = Image.new("RGB", (64, 64))
base.putdata([(i * 3 % 256, j * 5 % 256, 0) for j in range(64) for i in range(64)])
same = base.copy()
other = base.transpose(Image.FLIP_LEFT_RIGHT)
ck(prog.distance(prog.fingerprint(base), prog.fingerprint(same)) == 0,
   "the same pixels fingerprint identically")
ck(prog.distance(prog.fingerprint(base), prog.fingerprint(other)) > prog.TOLERANCE,
   f"different pixels do not ({prog.distance(prog.fingerprint(base), prog.fingerprint(other))} bits)")

def load_raw(steps, **kw):
    tmp2 = pathlib.Path(tempfile.mkdtemp()) / "p.json"
    tmp2.write_text(json.dumps({"task": "t", "steps": steps}))
    return Program.load(tmp2), tmp2

for bad, why in (
    ([{"action": "fly"}], "an unknown action"),
    ([{"action": "click"}], "a click with no anchor"),
    ([{"action": "type"}], "type with no text"),
    ([{"action": "scroll", "anchor": {"app": "Notes"}}], "scroll with no direction"),
    ([{"action": "click", "anchor": {"app": "Notes", "corner": "middle"}}], "a bad corner"),
):
    try:
        load_raw(bad); ck(False, f"{why} is refused at load")
    except ProgramError:
        ck(True, f"{why} is refused at load")
try:
    load_raw([]); ck(False, "an empty program is refused")
except ProgramError:
    ck(True, "an empty program is refused")

PROG = [
    {"action": "click", "anchor": {"app": "Notes", "corner": "tl", "dx": 100, "dy": 200},
     "note": "the message box"},
    {"action": "type", "text": "test is a blueberry"},
    {"action": "key", "text": "Return"},
]
p1, path1 = load_raw(PROG)
ck([s.action for s in p1.steps] == ["click", "type", "key"], "a valid program loads")
ck(p1.steps[0].anchor.app == "Notes" and p1.steps[1].anchor is None,
   "only the steps that touch a point carry an anchor")

d = newdesk(window=WindowTarget())
r = Runner(d, p1, learn=True, log=lambda *a: None)
ck(r.run() == 3, "a learning run executes every step")
ck(p1.steps[0].fingerprint and r.changed, "and records the fingerprint it saw")
moved = [e for e in kinds("kCGEventMouseMoved")]
ck(abs(moved[-1]["pos"][0] - 300.0) < 0.01 and abs(moved[-1]["pos"][1] - 300.0) < 0.01,
   f"the click landed at the window-relative point {moved[-1]['pos']}")
ck([k[0] for k in fakes.KEYS if k[0] in ("write", "press")][:1] == ["write"],
   "the type step typed")

p1.save(path1)
p2 = Program.load(path1)
ck(p2.steps[0].fingerprint == p1.steps[0].fingerprint, "a learned program round-trips to disk")

p2.steps[0].fingerprint = "5a5a5a5a5a5a5a5a"          # nothing on screen looks like this
d = newdesk(window=WindowTarget())
try:
    Runner(d, p2, policy="abort", log=lambda *a: None).run()
    ck(False, "abort stops on a mismatch")
except ReplayMiss as exc:
    ck("step 1" in str(exc) and "differ" in str(exc), f"abort stops on a mismatch: {exc}")
ck(not kinds("kCGEventLeftMouseDown"), "and nothing was clicked before it stopped")

d = newdesk(window=WindowTarget())
n = Runner(d, p2, policy="force", log=lambda *a: None).run()
ck(n == 3 and len(kinds("kCGEventLeftMouseDown")) == 1, "force clicks anyway")

d = newdesk(window=WindowTarget())
calls = []
def fake_repair(step, index, desk):
    calls.append(index)
    return Anchor.of((250.0, 250.0), Rect(200.0, 100.0, 900.0, 700.0), "Notes")
rp = Runner(d, p2, policy="repair", repair=fake_repair, log=lambda *a: None)
ck(rp.run() == 3 and calls == [0], "repair is asked about the failing step only")
ck(rp.changed and p2.steps[0].anchor.dx == 50.0,
   "and the step is rewritten with what it found")
ck(p2.steps[0].fingerprint != "5a5a5a5a5a5a5a5a", "with a fresh fingerprint")

flat = Image.new("RGB", (64, 64), (30, 30, 40))
ck(prog.contrast(flat) < prog.FLAT, "a blank patch has nothing to fingerprint")
ck(prog.fingerprint(flat) == "0000000000000000",
   "which is why it hashes to zero -- and would match any other blank patch")
said = []
d = newdesk(window=WindowTarget())
p5, _ = load_raw(PROG)
Runner(d, p5, learn=True, log=said.append).run()
ck(any("nearly blank" in m for m in said),
   "so learning one says so out loud")

d = newdesk(window=WindowTarget())
p3, _ = load_raw(PROG)
ck(Runner(d, p3, dry_run=True, log=lambda *a: None).run() == 3, "a dry run walks every step")
ck(not fakes.EVENTS, "and moves nothing at all")

try:
    Runner(newdesk(), p3, policy="repair", log=lambda *a: None)
    ck(False, "the repair policy needs a repairer")
except ProgramError:
    ck(True, "the repair policy needs a repairer")

d = newdesk(window=WindowTarget())
p4, _ = load_raw([{"action": "click", "anchor": {"app": "Xcode", "corner": "tl",
                                                 "dx": 10, "dy": 10}}])
try:
    Runner(d, p4, log=lambda *a: None).run()
    ck(False, "a program naming an app that is not open stops")
except ReplayMiss as exc:
    ck("Xcode" in str(exc), f"a program naming an app that is not open stops: {exc}")

print("\n== fitting the window back ==")
SHRUNK = [dict(w) for w in fakes.WIN_SCENE]
for _w in SHRUNK:
    if _w["hwnd"] == 102:
        _w["rect"] = (200, 100, 700, 500)          # the user made it smaller

# A step compiled against the window at 900x700, anchored bottom-left -- the
# shape of the Discord message-box click.
BOX = [{"action": "click", "anchor": {"app": "Notes", "corner": "bl",
                                      "dx": 700, "dy": 40, "window": [900, 700]}}]

d = newdesk(window=WindowTarget(), backend="win")
fakes.set_windows_scene(SHRUNK)
pf, _ = load_raw(BOX)
Runner(d, pf, log=lambda *x: None).run()
drift = kinds("win.press")[0]["pos"]
ck(abs(drift[1] - 560.0) < 0.1,
   f"without fitting, a bottom-left anchor follows the shrunken window ({drift})")

d = newdesk(window=WindowTarget(), backend="win")
fakes.set_windows_scene(SHRUNK)
pf2, _ = load_raw(BOX)
said = []
Runner(d, pf2, fit_windows=True, log=said.append).run()
after = next(w for w in d.backend.list_windows() if w.title == "Grocery list").rect
ck(after == Rect(200.0, 100.0, 900.0, 700.0),
   f"--fit-windows puts the window back to the size it was compiled at ({after})")
landed = kinds("win.press")[0]["pos"]
ck(abs(landed[0] - 900.0) < 0.1 and abs(landed[1] - 760.0) < 0.1,
   f"so the offset means what it meant and the click lands where recorded ({landed})")
ck(any("resized" in m for m in said), "and it says that it did so")
fakes.set_windows_scene()

d = newdesk(window=WindowTarget(), backend="win")
fakes.set_windows_scene(SHRUNK)
d.backend.set_window_rect = lambda win, rect: False        # a fixed-size dialog
pf3, _ = load_raw(BOX)
said = []
ck(Runner(d, pf3, fit_windows=True, log=said.append).run() == 1,
   "a window that refuses to resize does not stop the run")
ck(any("could not resize" in m for m in said), "it is reported and the anchor is used as is")
fakes.set_windows_scene()

print("\n== recording a run ==")
from program import Recorder, anchor_for

d = newdesk(window=WindowTarget())
a = anchor_for(d.backend, d.display_rect(), (300.0, 300.0))
ck(a is not None and a.app == "Notes", f"a point is anchored to the window it landed in ({a})")
ck(anchor_for(d.backend, d.display_rect(), (1700.0, 1100.0)) is None,
   "a point on bare desktop anchors to nothing")

d = newdesk(window=WindowTarget())
rec = Recorder(d, task="send a message", log=lambda *x: None)
for name, args in (("screenshot", {}),
                   ("left_click", {"coordinate": [100, 200]}),
                   ("type", {"text": "aaaa"}),
                   ("key", {"text": "Return"}),
                   ("zoom", {"region": [0, 0, 10, 10]}),
                   ("cursor_position", {})):
    rec.observe(name, args)
steps = rec.program.steps
ck([s.action for s in steps] == ["click", "type", "key"],
   f"screenshots, zooms and cursor reads are not replayable steps ({[s.action for s in steps]})")
ck(steps[0].anchor.app == "Notes" and steps[0].fingerprint,
   "the click is anchored to its window and fingerprinted")
ck(steps[0].anchor.point(Rect(200.0, 100.0, 900.0, 700.0)) == d.to_logical([100, 200]),
   "and the anchor resolves back to the exact point Claude clicked")
ck(steps[1].args == {"text": "aaaa"}, "the typed text is kept verbatim")
ck(set(rec.skipped) == {"screenshot", "zoom", "cursor_position"}, "and the rest is noted")

out = pathlib.Path(tempfile.mkdtemp()) / "rec.json"
rec.save(out)
back = Program.load(out)
ck(len(back.steps) == 3 and back.task == "send a message",
   "a recorded program saves and loads")
d2 = newdesk(window=WindowTarget())
ck(Runner(d2, back, log=lambda *x: None).run() == 3,
   "and replays against the same desktop without a miss")
ck(len(kinds("kCGEventLeftMouseDown")) == 1, "clicking once, where it was recorded")

d = newdesk(window=WindowTarget())
rec = Recorder(d, log=lambda *x: None)
rec.observe("left_click", {"coordinate": [100, 200], "text": "shift"})
ck(rec.program.steps[0].args.get("modifiers") == "shift",
   "a modifier on a click is recorded as a modifier, not as text to type")

d = newdesk(window=WindowTarget())
rec = Recorder(d, log=lambda *x: None)
rec.observe("left_click_drag", {"start_coordinate": [10, 10], "coordinate": [400, 300]})
drag = rec.program.steps[0]
ck(drag.action == "drag" and drag.anchor is not None and isinstance(drag.args["to"], Anchor),
   "a drag records both ends")

# ===========================================================================
print("\n== agent loop ==")
class Blk(SimpleNamespace):
    def model_dump(self, exclude_none=True):
        return {k: v for k, v in self.__dict__.items() if v is not None}
def tu(i, n, inp): return Blk(type="tool_use", id=i, name=n, input=inp, toolset_name="computer")
def tx(s): return Blk(type="text", text=s)

def run_agent(script, extra=()):
    import types as _t
    sent, systems = [], []
    class Msgs:
        def create(self, **kw):
            systems.append(kw.get("system", ""))
            sent.append(json.loads(json.dumps(kw["messages"], default=str)))
            return script[len(sent)-1]
    class Anthropic:
        def __init__(self, *a, **k): self.messages = Msgs()
    m = _t.ModuleType("anthropic"); m.Anthropic = Anthropic
    sys.modules["anthropic"] = m
    for k in ("agent",):
        sys.modules.pop(k, None)
    import agent
    sys.argv = ["agent.py", "do", "it", "--log", "/tmp/vt.jsonl", "--keep-images", "2", *extra]
    fakes.reset()
    return agent.main(), sent, systems

SCRIPT = [
    SimpleNamespace(stop_reason="tool_use", content=[tx("Looking."), tu("t1","screenshot",{})]),
    SimpleNamespace(stop_reason="tool_use", content=[
        tu("t2","left_click",{"coordinate":[100,200]}),
        tu("t3","bogus_action",{}),
        tu("t4","type",{"text":"hello"}),
        tu("t5","screenshot",{})]),
    SimpleNamespace(stop_reason="tool_use", content=[
        tu("t6","key",{"text":"super+space"}), tu("t7","screenshot",{})]),
    SimpleNamespace(stop_reason="tool_use", content=[
        tu("t8","zoom",{"region":[10,10,300,200]}), tu("t9","cursor_position",{})]),
    SimpleNamespace(stop_reason="end_turn", content=[tx("TASK COMPLETE")]),
]
rc, sent, systems = run_agent(SCRIPT, extra=("--full-screen",))
ck(rc == 0, "exits 0 on end_turn")
ck("What you can see:" not in systems[0], "--full-screen leaves the system prompt alone")
res = sent[2][-1]["content"]
HALT = "Not executed: an earlier computer action in this turn failed."
ck([r["tool_use_id"] for r in res] == ["t2","t3","t4","t5"], "results in request order")
ck(all(r["toolset_name"] == "computer" for r in res), "every tool_result carries toolset_name")
ck(res[0]["content"] == [{"type":"text","text":"OK"}], "successful click -> OK")
ck(res[1]["is_error"] and "unsupported action" in res[1]["content"], "bad action -> is_error")
ck(res[2]["content"] == HALT and res[3]["content"] == HALT, "everything after a failure -> halt text")
img = sent[1][-1]["content"][0]["content"][0]
import base64, io
w, h = Image.open(io.BytesIO(base64.b64decode(img["source"]["data"]))).size
ck(img["type"] == "image" and max(w, h) <= 2576, f"screenshot png <=2576 long edge ({w}x{h})")
imgs = [b for m in sent[4] if isinstance(m.get("content"), list) for b in m["content"]
        if b.get("type")=="tool_result" and isinstance(b.get("content"), list)
        and any(c.get("type")=="image" for c in b["content"])]
ck(len(imgs) == 2, f"image pruning keeps 2 ({len(imgs)})")
asst = [b for m in sent[4] if m["role"]=="assistant" for b in m["content"] if b.get("type")=="tool_use"]
ck(all(b.get("toolset_name")=="computer" for b in asst), "assistant tool_use keeps toolset_name")

# failsafe must kill the run, never be reported back to Claude as a tool error
FS = [SimpleNamespace(stop_reason="tool_use", content=[tu("f1","left_click",{"coordinate":[10,10]})]),
      SimpleNamespace(stop_reason="end_turn", content=[tx("should never get here")])]
orig = Desktop.run
Desktop.run = lambda self, n, a: (_ for _ in ()).throw(FailSafeAbort("corner"))
rc2, sent2, _ = run_agent(FS)
Desktop.run = orig
ck(rc2 == 130, f"failsafe exits 130 ({rc2})")
ck(len(sent2) == 1, "failsafe stops the loop instead of asking Claude to retry")

# window capture is the default: no flag needed
WIN = [SimpleNamespace(stop_reason="tool_use", content=[tu("w1", "screenshot", {})]),
       SimpleNamespace(stop_reason="end_turn", content=[tx("TASK COMPLETE")])]
rc3, sent3, systems3 = run_agent(WIN)
blocks = sent3[1][-1]["content"][0]["content"]
ck(blocks[0]["type"] == "text" and "Notes" in blocks[0]["text"],
   "agent.py captures one window by default")
ck(png_size(blocks[1]["source"]["data"]) == (1920, 1400), "and sends the cropped png")
ck("What you can see:" in systems3[0], "window mode explains the crop in the system prompt")

os.environ["CLAUDE_BACKEND"] = "x11"
LIN = [SimpleNamespace(stop_reason="tool_use", content=[tu("l1", "screenshot", {})]),
       SimpleNamespace(stop_reason="end_turn", content=[tx("TASK COMPLETE")])]
rc4, sent4, systems4 = run_agent(LIN)
os.environ.pop("CLAUDE_BACKEND")
ck(rc4 == 0, "agent.py runs on the X11 backend")
ck("Linux desktop" in systems4[0] and "copy is `ctrl+c`" in systems4[0],
   "and the system prompt stops claiming the Command key exists")
ck("Notes" in sent4[1][-1]["content"][0]["content"][0]["text"],
   "window capture is the default there as well")

os.environ["CLAUDE_BACKEND"] = "windows"
WIN = [SimpleNamespace(stop_reason="tool_use", content=[tu("n1", "screenshot", {})]),
       SimpleNamespace(stop_reason="end_turn", content=[tx("TASK COMPLETE")])]
rc5, sent5, systems5 = run_agent(WIN)
os.environ.pop("CLAUDE_BACKEND")
ck(rc5 == 0, "agent.py runs on the Windows backend")
ck("Windows desktop" in systems5[0] and "copy is `ctrl+c`" in systems5[0],
   "and its system prompt talks about Windows, not Command or Super")
ck("Notes" in sent5[1][-1]["content"][0]["content"][0]["text"],
   "window capture is the default there too")

print(f"\n{len(fails)} FAILURE(S): {fails}" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
