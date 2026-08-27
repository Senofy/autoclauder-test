"""Compile a task once, then replay it without the model in the loop.

A program is a list of steps. Each step that touches a point on screen carries
an **anchor** -- an offset from a corner of a named application's window -- and
optionally a **fingerprint**, a hash of the pixels around that point when the
step was known to work.

    {"action": "click",
     "anchor": {"app": "Discord", "corner": "bl", "dx": 231, "dy": 40,
                "window": [2690, 1855]},
     "fingerprint": "b0e4c3a91f2d5e70"}

Anchors survive the window moving, and a resize as far as the nearest corner
allows. Fingerprints are what survives everything else, because the danger in a
replay is not that it fails -- it is that it succeeds at the wrong thing, and a
click where Send used to be is worse than an error. So every step with a
fingerprint is checked before it runs, and a mismatch is a decision:

    abort   stop and say which step diverged (the default)
    repair  hand that one step to the model, then rewrite it in the program
    force   go anyway; for when the pixels are noisy and the geometry is right

`--learn` fills in the fingerprints of a hand-written program on a run you have
watched, so you can author the geometry and let the machine record the pixels.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

import motion as mo
from window import Rect, WindowTarget

PATCH = 64             # logical points around a target that make its fingerprint
TOLERANCE = 10         # differing bits of a 64-bit hash still counted as a match
FLAT = 8               # grey range below which a patch has nothing to fingerprint
CORNERS = ("tl", "tr", "bl", "br")

# Program action -> the Desktop action it becomes. Anything not here is refused
# at load time rather than part way through a run.
POINT_ACTIONS = {"click": ("left", 1), "right_click": ("right", 1),
                 "middle_click": ("middle", 1), "double_click": ("left", 2),
                 "triple_click": ("left", 3)}
PLAIN_ACTIONS = {"type", "key", "wait", "hold_key"}


class ProgramError(Exception):
    """The program is not valid. Raised at load, before anything moves."""


class ReplayMiss(Exception):
    """A step's fingerprint did not match and the policy was to stop."""


# --------------------------------------------------------------------------
# fingerprints: a difference hash, which cares about structure and not
# brightness. 64 bits, so a Hamming distance is a small integer.
# --------------------------------------------------------------------------

def fingerprint(img: Image.Image) -> str:
    grey = img.convert("L").resize((9, 8), Image.LANCZOS)
    px = list(grey.getdata())
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | int(px[base + col] < px[base + col + 1])
    return f"{bits:016x}"


def distance(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def contrast(img: Image.Image) -> int:
    """How much there is to fingerprint here, 0-255.

    A difference hash of a featureless patch is all zeroes, and all zeroes
    matches every other featureless patch -- so a step whose target sits on
    blank canvas gets a fingerprint that verifies nothing. Worth saying out
    loud when it is recorded rather than discovering it the day it matters.
    """
    grey = img.convert("L")
    lo, hi = grey.getextrema()
    return hi - lo


# --------------------------------------------------------------------------
# anchors
# --------------------------------------------------------------------------

@dataclass
class Anchor:
    """A point, described relative to a window rather than to the screen."""

    app: str
    corner: str
    dx: float
    dy: float
    window: tuple[float, float] | None = None      # window size when compiled
    title: str = ""

    @classmethod
    def of(cls, point, rect: Rect, app: str, title: str = "") -> "Anchor":
        """Describe `point` against whichever corner of `rect` it is nearest.

        Nearest, rather than always the top-left, is what makes a resize
        survivable: a sidebar item stays put relative to the top-left, a Send
        button relative to the bottom-right.
        """
        x, y = float(point[0]), float(point[1])
        left, top = x - rect.x, y - rect.y
        right, bottom = rect.right - x, rect.bottom - y
        corner = ("t" if top <= bottom else "b") + ("l" if left <= right else "r")
        corner = {"tl": "tl", "tr": "tr", "bl": "bl", "br": "br"}[corner[0] + corner[1]]
        return cls(app=app, corner=corner,
                   dx=left if corner[1] == "l" else right,
                   dy=top if corner[0] == "t" else bottom,
                   window=(rect.w, rect.h), title=title)

    def point(self, rect: Rect) -> tuple[float, float]:
        x = rect.x + self.dx if self.corner[1] == "l" else rect.right - self.dx
        y = rect.y + self.dy if self.corner[0] == "t" else rect.bottom - self.dy
        # A window that shrank can put the offset outside it; clamp rather than
        # click the desktop behind.
        return (min(max(x, rect.x), rect.right - 1),
                min(max(y, rect.y), rect.bottom - 1))

    def resized(self, rect: Rect) -> tuple[float, float]:
        """How much the window has changed size since this was compiled."""
        if not self.window:
            return (0.0, 0.0)
        return (rect.w - self.window[0], rect.h - self.window[1])

    def to_json(self) -> dict:
        d = {"app": self.app, "corner": self.corner,
             "dx": round(self.dx, 1), "dy": round(self.dy, 1)}
        if self.window:
            d["window"] = [round(self.window[0]), round(self.window[1])]
        if self.title:
            d["title"] = self.title
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Anchor":
        if not isinstance(d, dict) or "app" not in d:
            raise ProgramError(f"an anchor needs an app: {d!r}")
        corner = str(d.get("corner", "tl")).lower()
        if corner not in CORNERS:
            raise ProgramError(f"corner must be one of {CORNERS}, not {corner!r}")
        w = d.get("window")
        return cls(app=str(d["app"]), corner=corner,
                   dx=float(d.get("dx", 0)), dy=float(d.get("dy", 0)),
                   window=(float(w[0]), float(w[1])) if w else None,
                   title=str(d.get("title", "")))


# --------------------------------------------------------------------------
# steps and programs
# --------------------------------------------------------------------------

@dataclass
class Step:
    action: str
    args: dict = field(default_factory=dict)
    anchor: Anchor | None = None
    fingerprint: str | None = None
    note: str = ""

    @property
    def needs_point(self) -> bool:
        return self.action in POINT_ACTIONS or self.action in ("move", "scroll", "drag")

    def describe(self) -> str:
        bits = [self.action]
        if self.anchor:
            bits.append(f"{self.anchor.app}[{self.anchor.corner}"
                        f"+{self.anchor.dx:.0f},{self.anchor.dy:.0f}]")
        if self.args.get("text"):
            bits.append(json.dumps(self.args["text"])[:40])
        if self.note:
            bits.append(f"-- {self.note}")
        return " ".join(bits)

    def to_json(self) -> dict:
        d: dict = {"action": self.action}
        d.update({k: v for k, v in self.args.items()})
        if self.anchor:
            d["anchor"] = self.anchor.to_json()
        if self.fingerprint:
            d["fingerprint"] = self.fingerprint
        if self.note:
            d["note"] = self.note
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Step":
        if not isinstance(d, dict) or "action" not in d:
            raise ProgramError(f"every step needs an action: {d!r}")
        action = str(d["action"])
        known = set(POINT_ACTIONS) | PLAIN_ACTIONS | {"move", "scroll", "drag"}
        if action not in known:
            raise ProgramError(f"unknown action {action!r}; try one of {sorted(known)}")
        args = {k: v for k, v in d.items()
                if k not in ("action", "anchor", "fingerprint", "note", "to")}
        if "to" in d:
            args["to"] = Anchor.from_json(d["to"])
        step = cls(action=action, args=args,
                   anchor=Anchor.from_json(d["anchor"]) if d.get("anchor") else None,
                   fingerprint=d.get("fingerprint"), note=str(d.get("note", "")))
        if step.needs_point and step.anchor is None:
            raise ProgramError(f"{action!r} needs an anchor saying where to act")
        if action == "drag" and not isinstance(args.get("to"), Anchor):
            raise ProgramError("drag needs a `to` anchor as well as an anchor")
        if action == "type" and "text" not in args:
            raise ProgramError("type needs text")
        if action == "key" and "text" not in args:
            raise ProgramError("key needs text, e.g. \"super+space\"")
        if action == "scroll" and "scroll_direction" not in args:
            raise ProgramError("scroll needs a scroll_direction")
        return step


@dataclass
class Program:
    task: str = ""
    steps: list[Step] = field(default_factory=list)

    @classmethod
    def load(cls, path) -> "Program":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ProgramError(f"no program at {path}") from None
        except json.JSONDecodeError as exc:
            raise ProgramError(f"{path} is not valid JSON: {exc}") from None
        if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list):
            raise ProgramError(f"{path} needs a top-level \"steps\" list")
        if not raw["steps"]:
            raise ProgramError(f"{path} has no steps")
        return cls(task=str(raw.get("task", "")),
                   steps=[Step.from_json(s) for s in raw["steps"]])

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(
            {"task": self.task, "steps": [s.to_json() for s in self.steps]},
            indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# running one
# --------------------------------------------------------------------------

class Runner:
    """Executes a Program against a Desktop. No API calls unless a step misses
    and the policy is `repair`, which spends exactly one."""

    def __init__(self, desk, program: Program, policy: str = "abort",
                 tolerance: int = TOLERANCE, learn: bool = False,
                 dry_run: bool = False, repair=None, log=print) -> None:
        if policy not in ("abort", "repair", "force"):
            raise ProgramError(f"policy must be abort, repair or force, not {policy!r}")
        if policy == "repair" and repair is None:
            raise ProgramError("the repair policy needs something to repair with")
        self.desk, self.program = desk, program
        self.policy, self.tolerance = policy, tolerance
        self.learn, self.dry_run = learn, dry_run
        self.repair, self.log = repair, log
        self.changed = False        # a learn or a repair rewrote the program

    # ---------------- locating ----------------

    def window(self, anchor: Anchor) -> Rect:
        found = WindowTarget(app=anchor.app).resolve(self.desk.backend,
                                                     self.desk.display_rect())
        if found is None:
            raise ReplayMiss(f"no window on screen for app {anchor.app!r}")
        return found[0]

    def patch(self, point) -> Image.Image:
        half = PATCH / 2
        region = Rect(point[0] - half, point[1] - half, PATCH, PATCH)
        img, _covered = self.desk.backend.capture(self.desk.display, region)
        return img

    # ---------------- checking ----------------

    def verify(self, step: Step, point, index: int):
        got = fingerprint(self.patch(point))
        if not step.fingerprint:
            if self.learn:
                step.fingerprint = got
                self.changed = True
                self.log(f"      learned fingerprint {got}")
                if contrast(self.patch(point)) < FLAT:
                    self.log("      \033[33mwarning:\033[0m those pixels are nearly "
                             "blank, so this fingerprint will match almost anything. "
                             "Anchor the step to something with an edge in it.")
            return point
        gap = distance(step.fingerprint, got)
        if gap <= self.tolerance:
            return point
        return self.miss(step, index, point,
                         f"the pixels there differ by {gap} bits "
                         f"(tolerance {self.tolerance}); expected {step.fingerprint}, "
                         f"found {got}")

    def miss(self, step: Step, index: int, point, why: str):
        where = f"step {index + 1} ({step.describe()})"
        if self.policy == "force":
            self.log(f"      \033[33mforcing past:\033[0m {why}")
            return point
        if self.policy == "repair":
            self.log(f"      \033[33mrepairing:\033[0m {why}")
            anchor = self.repair(step, index, self.desk)
            if anchor is None:
                raise ReplayMiss(f"{where}: could not be repaired -- {why}")
            step.anchor = anchor
            self.changed = True
            point = anchor.point(self.window(anchor))
            step.fingerprint = fingerprint(self.patch(point))
            self.log(f"      repaired to {anchor.app}[{anchor.corner}"
                     f"+{anchor.dx:.0f},{anchor.dy:.0f}]")
            return point
        raise ReplayMiss(f"{where}: {why}")

    # ---------------- doing ----------------

    def execute(self, step: Step, point) -> None:
        a = step.args
        mods = self.desk.combo(a["modifiers"]) if a.get("modifiers") else []
        if step.action in POINT_ACTIONS:
            button, clicks = POINT_ACTIONS[step.action]
            self.desk.click_at(point, button, clicks, mods)
        elif step.action == "move":
            self.desk.glide(point)
        elif step.action == "drag":
            end = a["to"].point(self.window(a["to"]))
            self.desk.drag_at(point, end, mods)
        elif step.action == "scroll":
            self.desk.glide(point)
            self.desk.run("scroll", {k: v for k, v in a.items()
                                     if k in ("scroll_direction", "scroll_amount")})
        else:
            self.desk.run(step.action, {k: v for k, v in a.items() if k != "modifiers"})

    def run(self) -> int:
        total = len(self.program.steps)
        for index, step in enumerate(self.program.steps):
            point = None
            if step.needs_point:
                rect = self.window(step.anchor)
                dw, dh = step.anchor.resized(rect)
                if abs(dw) > 1 or abs(dh) > 1:
                    self.log(f"      note: {step.anchor.app} is {dw:+.0f}x{dh:+.0f} "
                             "points different from when this was compiled")
                point = step.anchor.point(rect)
                point = self.verify(step, point, index)
            self.log(f"[{index + 1}/{total}] {step.describe()}")
            if not self.dry_run:
                self.execute(step, point)
                time.sleep(mo.settle(self.desk.motion, self.desk.rng))
        return total


# --------------------------------------------------------------------------
# compiling one, from a live agent run
# --------------------------------------------------------------------------

# What Claude's member actions become in a program. Anything absent is not
# replayable and is skipped: a screenshot is how the model looks around, and a
# program does not need to look around.
RECORDABLE = {
    "left_click": "click", "right_click": "right_click",
    "middle_click": "middle_click", "double_click": "double_click",
    "triple_click": "triple_click", "mouse_move": "move",
    "left_click_drag": "drag", "scroll": "scroll",
    "type": "type", "key": "key", "wait": "wait", "hold_key": "hold_key",
}


def anchor_for(backend, display: Rect, point, app_hint: str = "") -> Anchor | None:
    """Anchor `point` to whichever window it landed in.

    The window list is front to back, so the first one containing the point is
    the one on top -- the one the user would say was clicked.
    """
    for w in backend.list_windows():
        if not w.usable:
            continue
        r = w.rect
        if r.x <= point[0] < r.right and r.y <= point[1] < r.bottom:
            if app_hint and app_hint.lower() not in w.app.lower():
                continue
            return Anchor.of(point, r, w.app, w.title)
    return None


class Recorder:
    """Turns a live agent run into a Program.

    `observe()` is called just before each action is executed, because the
    fingerprint has to be of the screen the model decided against, not of
    whatever the screen looks like afterwards.
    """

    def __init__(self, desk, task: str = "", log=print) -> None:
        self.desk, self.log = desk, log
        self.program = Program(task=task)
        self.skipped: list[str] = []

    def observe(self, name: str, args: dict) -> None:
        action = RECORDABLE.get(name)
        if action is None:
            self.skipped.append(name)
            return
        args = dict(args or {})
        anchor = fp = None
        if args.get("coordinate"):
            point = self.desk.to_logical(args["coordinate"])
            anchor = anchor_for(self.desk.backend, self.desk.display_rect(), point)
            if anchor is None:
                self.log(f"      not recorded: {name} landed outside every window")
                return
            region = Rect(point[0] - PATCH / 2, point[1] - PATCH / 2, PATCH, PATCH)
            try:
                patch, _ = self.desk.backend.capture(self.desk.display, region)
                fp = fingerprint(patch)
                if contrast(patch) < FLAT:
                    self.log("      recorded on nearly blank pixels; that step will "
                             "verify almost nothing")
            except Exception:                          # noqa: BLE001
                fp = None
        step = Step(action=action, anchor=anchor, fingerprint=fp,
                    args={k: v for k, v in args.items()
                          if k not in ("coordinate", "start_coordinate")})
        if action == "drag":
            start = self.desk.to_logical(args["start_coordinate"])
            step.anchor = anchor_for(self.desk.backend, self.desk.display_rect(), start)
            step.args["to"] = anchor
            if step.anchor is None:
                self.log("      not recorded: drag started outside every window")
                return
        if step.args.get("text") and action in POINT_ACTIONS:
            step.args["modifiers"] = step.args.pop("text")   # clicks carry modifiers
        self.program.steps.append(step)

    def save(self, path) -> None:
        if not self.program.steps:
            self.log(f"nothing recordable happened; {path} not written")
            return
        self.program.save(path)
        note = f" ({len(set(self.skipped))} kind(s) of step skipped)" if self.skipped else ""
        self.log(f"recorded {len(self.program.steps)} step(s) to {path}{note}")
