"""Human-shaped pointer motion and typing rhythm.

Pure math and timing -- no macOS imports -- so it can be tested headless.

A real hand does not teleport and does not travel in a straight line. It bows
slightly off-axis, accelerates out of rest and decelerates into the target
(a minimum-jerk velocity profile), trembles a little in the fast middle
section, and on a long throw it frequently sails past the target and pulls
back. `path()` reproduces all four.

Why bother, practically: many UIs only reveal what you need on hover, and
drag targets, sliders, canvases and menu trees respond to the *stream* of
intermediate positions, not the endpoint. A teleport skips every event in
between and those interfaces simply don't react.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace

Point = tuple[float, float]
Sample = tuple[float, float, float]   # x, y, seconds to sleep afterwards


@dataclass(frozen=True)
class MotionProfile:
    enabled: bool = True        # False -> single-sample teleport, the old behaviour
    speed: float = 1.0          # >1 finishes sooner
    sample_hz: float = 90.0     # positions emitted per second of travel
    curvature: float = 1.0      # 0 -> dead straight
    tremor: float = 1.0         # 0 -> no jitter
    overshoot_chance: float = 0.28
    type_speed: float = 1.0     # >1 types faster


# ---------------------------------------------------------------------------
# curves
# ---------------------------------------------------------------------------

def _ease(t: float) -> float:
    """Minimum-jerk position profile: still, fast, still. The realism lives here."""
    return t * t * t * (10.0 + t * (-15.0 + 6.0 * t))


def _bezier(p0: Point, p1: Point, p2: Point, p3: Point, t: float) -> Point:
    u = 1.0 - t
    a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
    return (a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
            a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1])


def _controls(start: Point, end: Point, rng: random.Random, curvature: float):
    """Two control points that bow the path off the straight line, asymmetrically."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    dist = math.hypot(dx, dy)
    if dist < 1e-9:
        return start, end
    px, py = -dy / dist, dx / dist                     # unit perpendicular
    # Slightly sublinear in distance: short hops stay near-straight, long hauls
    # bow by roughly 4-8% of the throw, which is what hands actually do.
    bow = curvature * (dist ** 0.9) * rng.uniform(0.06, 0.15) * rng.choice((-1.0, 1.0))
    a = rng.uniform(0.18, 0.38)
    b = rng.uniform(0.62, 0.86)
    return ((start[0] + dx * a + px * bow * rng.uniform(0.70, 1.10),
             start[1] + dy * a + py * bow * rng.uniform(0.70, 1.10)),
            (start[0] + dx * b + px * bow * rng.uniform(0.60, 1.05),
             start[1] + dy * b + py * bow * rng.uniform(0.60, 1.05)))


def _duration(dist: float, profile: MotionProfile, rng: random.Random) -> float:
    """Fitts-flavoured: time grows with the log of distance, not linearly."""
    d = 0.09 + 0.075 * math.log2(1.0 + dist / 42.0)
    d *= rng.uniform(0.82, 1.24)
    return max(0.03, d / max(0.05, profile.speed))


def _leg(start: Point, end: Point, profile: MotionProfile,
         rng: random.Random) -> list[Sample]:
    """One uninterrupted sweep."""
    dist = math.hypot(end[0] - start[0], end[1] - start[1])
    if dist < 0.5:
        return []
    dur = _duration(dist, profile, rng)
    steps = max(2, min(240, int(dur * profile.sample_hz)))
    c1, c2 = _controls(start, end, rng, profile.curvature)
    amp = profile.tremor * min(1.8, 0.25 + dist * 0.004)

    out: list[Sample] = []
    for i in range(1, steps + 1):
        tau = i / steps
        x, y = _bezier(start, c1, c2, end, _ease(tau))
        if i < steps:
            # tremor swells mid-flight and vanishes on approach, so we land clean
            k = amp * math.sin(math.pi * tau) ** 0.7
            x += rng.gauss(0.0, k)
            y += rng.gauss(0.0, k)
        else:
            x, y = end                                  # always exact on the last sample
        out.append((x, y, (dur / steps) * rng.uniform(0.72, 1.34)))
    return out


def _compose(start: Point, end: Point, profile: MotionProfile,
             rng: random.Random) -> list[Sample]:
    """One move, possibly in two legs if it overshoots."""
    dist = math.hypot(end[0] - start[0], end[1] - start[1])

    if dist > 140.0 and rng.random() < profile.overshoot_chance:
        over = rng.uniform(0.02, 0.06) * dist
        ang = math.atan2(end[1] - start[1], end[0] - start[0]) + rng.gauss(0.0, 0.22)
        past = (end[0] + math.cos(ang) * over, end[1] + math.sin(ang) * over)
        legs = _leg(start, past, profile, rng)
        legs.append((past[0], past[1], rng.uniform(0.02, 0.06)))     # notice, then correct
        snap = replace(profile,
                       speed=profile.speed * rng.uniform(1.5, 2.3),
                       curvature=profile.curvature * 0.4,
                       overshoot_chance=0.0)
        legs += _leg(past, end, snap, rng)
        return legs

    return _leg(start, end, profile, rng)


def _escapes(samples: list[Sample], bounds: tuple[float, float],
             margin: float = 2.0) -> bool:
    w, h = bounds
    return any(x < margin or y < margin or x > w - 1 - margin or y > h - 1 - margin
               for x, y, _ in samples)


def path(start: Point, end: Point, profile: MotionProfile, rng: random.Random,
         bounds: tuple[float, float] | None = None) -> list[Sample]:
    """Full move. `bounds` is (width, height) in the same units as the points.

    A bow that leaves the screen is worse than no bow: macOS clamps the pointer
    to the display, and a path that grazes a corner can fire a hot corner (and
    our own failsafe). So if the arc escapes, redraw it nearly flat and clamp.
    """
    if not profile.enabled:
        return [(end[0], end[1], 0.0)]
    if math.hypot(end[0] - start[0], end[1] - start[1]) < 0.5:
        return []

    samples = _compose(start, end, profile, rng)

    if bounds is not None and _escapes(samples, bounds):
        samples = _compose(start, end,
                           replace(profile, curvature=profile.curvature * 0.3,
                                   overshoot_chance=0.0), rng)
        w, h = bounds
        samples = [(min(max(x, 0.0), w - 1.0), min(max(y, 0.0), h - 1.0), dt)
                   for x, y, dt in samples]
        if samples:                       # the landing must still be exact
            samples[-1] = (end[0], end[1], samples[-1][2])
    return samples


# ---------------------------------------------------------------------------
# discrete timings
# ---------------------------------------------------------------------------

def settle(profile: MotionProfile, rng: random.Random) -> float:
    """Pause between arriving somewhere and pressing the button."""
    return rng.uniform(0.04, 0.13) / max(0.05, profile.speed) if profile.enabled else 0.0


def hold(profile: MotionProfile, rng: random.Random) -> float:
    """How long a button stays down."""
    return rng.uniform(0.045, 0.11) if profile.enabled else 0.008


def between_clicks(profile: MotionProfile, rng: random.Random) -> float:
    """Gap inside a double or triple click -- must stay under the system threshold."""
    return rng.uniform(0.06, 0.13) if profile.enabled else 0.04


def key_intervals(text: str, profile: MotionProfile, rng: random.Random):
    """Yield (char, delay_after). Uneven, with pauses at punctuation."""
    if not profile.enabled:
        for ch in text:
            yield ch, 0.012
        return
    sp = max(0.05, profile.type_speed)
    for ch in text:
        d = rng.gauss(0.055, 0.022)
        if ch == " ":
            d *= rng.uniform(0.8, 1.3)
        if ch in ".,!?;:\n":
            d += rng.uniform(0.05, 0.18)
        if rng.random() < 0.014:
            d += rng.uniform(0.15, 0.45)          # a beat of thought
        yield ch, max(0.008, d / sp)
