Headless regression suite. Stubs Quartz, AppKit, Xlib, `ctypes.windll` and
pyautogui, so it runs anywhere and touches nothing -- including the two
platforms it is not running on.

    cd tests && python3 harness.py

Covers path geometry (arc, easing, exact landing, bounds), determinism under
--seed, the Quartz event stream (click states, drag events, scroll notches),
typing rhythm and clipboard restore, the failsafe, the env file, window
selection and cropping, the model/logical coordinate round trip, and the agent
loop's batch-halt and pruning behaviour on every backend.

Per platform it also covers what only that platform gets wrong: on X11, window
enumeration through a reparenting frame, override-redirect popups, button
numbering and wheel buttons; on Windows, DPI awareness, cloaked windows, the
`GetWindowRect` border lie, and 0-65535 absolute coordinates.

`fakes.py` describes one imaginary desktop three times -- as Quartz reports it,
as X11 does, and as `EnumWindows` does -- so all three backends can be held to
the same crop, the same label and the same coordinates.
