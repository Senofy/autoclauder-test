#!/usr/bin/env python3
"""Render sample pointer paths to a PNG so you can tune the profile by eye.

    python3 motion_preview.py --seed 7 --out motion.png

Needs nothing but motion.py and Pillow -- no macOS, no API key. Dot spacing
shows velocity: bunched at the ends, stretched through the middle is the
minimum-jerk profile doing its job.
"""

from __future__ import annotations

import argparse
import random

from PIL import Image, ImageDraw

from motion import MotionProfile, path

W, H = 1200, 760
BG, LINE, DOT, MARK = (18, 18, 24), (90, 170, 255), (235, 240, 255), (255, 120, 90)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="motion.png")
    ap.add_argument("--moves", type=int, default=9)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--curvature", type=float, default=1.0)
    ap.add_argument("--tremor", type=float, default=1.0)
    ap.add_argument("--overshoot", type=float, default=0.28)
    a = ap.parse_args()

    rng = random.Random(a.seed)
    prof = MotionProfile(speed=a.speed, curvature=a.curvature,
                         tremor=a.tremor, overshoot_chance=a.overshoot)

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    total_pts = total_time = 0.0

    for _ in range(a.moves):
        p0 = (rng.uniform(60, W - 60), rng.uniform(60, H - 60))
        p1 = (rng.uniform(60, W - 60), rng.uniform(60, H - 60))
        pts = path(p0, p1, prof, rng)
        if not pts:
            continue
        total_pts += len(pts)
        total_time += sum(s[2] for s in pts)

        d.line([p0, p1], fill=(55, 55, 70), width=1)          # the straight line it avoided
        d.line([(x, y) for x, y, _ in pts], fill=LINE, width=2)
        for x, y, _ in pts:                                   # dot spacing == speed
            d.ellipse([x - 1.5, y - 1.5, x + 1.5, y + 1.5], fill=DOT)
        for p, r in ((p0, 5), (p1, 5)):
            d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], outline=MARK, width=2)

    d.text((14, 12), f"{a.moves} moves | {int(total_pts)} samples | "
                     f"{total_time:.2f}s total | seed={a.seed} speed={a.speed} "
                     f"curve={a.curvature} tremor={a.tremor}", fill=(150, 155, 175))
    img.save(a.out)
    print(f"wrote {a.out}  ({int(total_pts)} samples, {total_time:.2f}s of travel)")


if __name__ == "__main__":
    main()
