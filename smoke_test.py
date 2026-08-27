#!/usr/bin/env python3
"""Check permissions, coordinate math and pointer motion before the agent runs.

    python3 smoke_test.py

Costs no API tokens. Moves your mouse. Works on both backends; the advice it
gives when something fails is the advice for the platform you are on.
"""

import math
import random
import sys
import time

MAC_CAPTURE_HELP = """   Grant Screen Recording to your terminal in System Settings >
   Privacy & Security > Screen & System Audio Recording, then fully quit
   and reopen the terminal."""
MAC_INPUT_HELP = """   Grant Accessibility to your terminal in System Settings >
   Privacy & Security > Accessibility, then fully quit and reopen it."""
X11_CAPTURE_HELP = """   Check $DISPLAY, and that this is an X11 session rather than Wayland
   (echo $XDG_SESSION_TYPE). Under Wayland only XWayland windows are visible."""
X11_INPUT_HELP = """   The X server needs the XTEST extension, and python3-xlib must be
   installed: sudo apt install python3-xlib xclip."""


def main() -> int:
    print("0. env file ...", end=" ", flush=True)
    try:
        import env
        path, count = env.load()
    except Exception as exc:
        print(f"FAIL\n   {exc}")
        return 1
    print(f"ok, {path} ({count} set)" if path else "none found, using the environment as is")

    print("1. imports ...", end=" ", flush=True)
    try:
        from PIL import Image  # noqa: F401
        import backend as be
        from desktop import Desktop
        from motion import MotionProfile, path
        from window import WindowTarget
    except Exception as exc:
        print(f"FAIL\n   {exc}\n   -> pip install -r requirements.txt")
        return 1
    print("ok")

    print("2. backend ...", end=" ", flush=True)
    try:
        back = be.load()
    except Exception as exc:
        print(f"FAIL\n   {exc}")
        return 1
    print(f"ok, {back.name}")
    if back.warning:
        print(f"   WARNING: {back.warning}")
    capture_help = MAC_CAPTURE_HELP if back.name == "macOS" else X11_CAPTURE_HELP
    input_help = MAC_INPUT_HELP if back.name == "macOS" else X11_INPUT_HELP

    rng = random.Random(1234)
    # Window capture is what agent.py does unless you pass --full-screen, so
    # that is what this checks.
    desk = Desktop(motion=MotionProfile(), rng=rng, window=WindowTarget(), backend=back)
    print(f"3. logical screen ... {desk.logical_w} x {desk.logical_h} pts")

    print("4. reading the screen ...", end=" ", flush=True)
    try:
        d = back.display_rect(desk.display)
        raw, _covered = back.capture(desk.display, d)
    except Exception as exc:
        print(f"FAIL\n   {exc}\n{capture_help}")
        return 1
    print(f"ok, display {d.w:.0f} x {d.h:.0f} pts arrives as {raw.size[0]} x {raw.size[1]} px")

    print("5. capture scope ...", end=" ", flush=True)
    try:
        found = WindowTarget().resolve(back, d)
    except Exception as exc:
        print(f"FAIL\n   {exc}")
        return 1
    if found is None:
        print("no focused window resolved -- the whole display would be sent")
    else:
        r, label = found
        print(f"ok, focused window is {label}\n"
              f"   {r.w:.0f} x {r.h:.0f} pts at ({r.x:.0f}, {r.y:.0f})")
    print("   (agent.py --full-screen sends the whole display instead)")

    print("6. crop + downscale + frame ...", end=" ", flush=True)
    desk.screenshot_b64()
    f = desk._frame
    print(f"ok, sending {f.width} x {f.height} px of {f.label}, scale {f.scale:.4f}")
    if f.scale > 1.01:
        print("   (a downscale, or a Retina display: 1 model pixel > 1 point)")

    print("7. round-trip a centre point ...", end=" ", flush=True)
    mid = (f.width // 2, f.height // 2)
    logical = desk.to_logical(mid)
    back_again = desk.to_model(*logical)
    drift = max(abs(back_again[0] - mid[0]), abs(back_again[1] - mid[1]))
    print(f"{mid} -> ({logical[0]:.0f}, {logical[1]:.0f}) pts -> {back_again}  (drift {drift}px)")
    if drift > 2:
        print("   WARNING: coordinate drift is high; clicks may miss.")

    print("8. pointer control ...", end=" ", flush=True)
    origin = back.cursor_position()
    target = (desk.logical_w * 0.75, desk.logical_h * 0.35)
    t0 = time.perf_counter()
    try:
        desk.glide(target)
    except Exception as exc:
        print(f"FAIL\n   {exc}\n{input_help}")
        return 1
    elapsed = time.perf_counter() - t0
    landed = back.cursor_position()
    err = math.hypot(landed[0] - target[0], landed[1] - target[1])
    if err > 3:
        print(f"FAIL\n   pointer ended {err:.1f}pt from the target (or did not move).\n"
              f"{input_help}")
        return 1
    print(f"ok, travelled in {elapsed * 1000:.0f}ms, landed {err:.2f}pt off")

    print("9. human motion shape ...", end=" ", flush=True)
    pts = path((100.0, 100.0), (1000.0, 600.0), MotionProfile(), random.Random(9),
               bounds=(desk.logical_w, desk.logical_h))
    chord = math.hypot(900.0, 500.0)
    arc = sum(math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
              for i in range(1, len(pts)))
    gaps = [math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
            for i in range(1, len(pts))]
    mid_gap = sum(gaps[len(gaps) // 3: 2 * len(gaps) // 3]) / max(1, len(gaps) // 3)
    end_gap = sum(gaps[-4:]) / 4
    print(f"ok, {len(pts)} samples, arc/chord {arc / chord:.3f}, "
          f"mid step {mid_gap:.1f}pt vs final step {end_gap:.1f}pt")
    if mid_gap <= end_gap:
        print("   WARNING: no deceleration into the target -- easing may be off.")

    print("10. clipboard ...", end=" ", flush=True)
    before = back.clip_read()
    try:
        back.clip_write("claude-smoke-test")
        got = back.clip_read()
    except Exception as exc:
        print(f"FAIL\n   {exc}")
        return 1
    if before is not None:
        back.clip_write(before)
    print("ok" if got == "claude-smoke-test" else f"odd (read back {got!r})")

    desk.glide(origin)
    print("\nAll green. Try:\n"
          '  python3 agent.py "take a screenshot and describe what you see"\n'
          "  python3 motion_preview.py --seed 7   # see the paths without running anything")
    return 0


if __name__ == "__main__":
    sys.exit(main())
