Headless regression suite. Stubs Quartz, AppKit, Xlib and pyautogui, so it runs
anywhere and touches nothing -- including the platform it is not running on.

    cd tests && python3 harness.py

Covers path geometry (arc, easing, exact landing, bounds), determinism under
--seed, the Quartz event stream (click states, drag events, scroll notches),
typing rhythm and clipboard restore, the failsafe, window selection and
cropping, the model/logical coordinate round trip, the X11 backend (window
enumeration through a reparenting frame, override-redirect popups, button
numbering, wheel buttons, key mapping), and the agent loop's batch-halt and
pruning behaviour on both backends.

`fakes.py` describes one imaginary desktop twice -- once the way Quartz reports
it, once the way X11 does -- so the two backends can be held to the same answer.
