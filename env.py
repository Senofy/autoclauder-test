"""Read a `.env` file into the environment before anything looks at it.

Twenty lines instead of a dependency, in the same spirit as shelling out to
pbcopy rather than taking a clipboard library. Two rules worth knowing:

* A variable already set in the real environment wins. `CLAUDE_DISPLAY=2 python3
  agent.py ...` still overrides the file, which is what you want from a one-off.
* This has to run before `desktop` is imported, because importing pyautogui on
  Linux opens `$DISPLAY` there and then. `agent.py` calls it above its own
  imports for that reason.

Format: `KEY=value`, one per line. `#` comments, blank lines and a leading
`export ` are all fine. Quoted values keep their spaces; unquoted ones are
trimmed and lose any trailing ` # comment`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_PATH = HERE / ".env"

_KEY_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key[0].isdigit() or set(key) - _KEY_OK:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]                       # quoted: keep it verbatim
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()   # unquoted: trailing comment
        out[key] = value
    return out


def find(explicit: str | os.PathLike | None = None) -> Path | None:
    """Which file to read: --env, then $CLAUDE_ENV_FILE, then ./.env next to the code."""
    if explicit:
        return Path(explicit).expanduser()
    if "--env" in sys.argv[1:]:
        i = sys.argv.index("--env")
        if i + 1 < len(sys.argv):
            return Path(sys.argv[i + 1]).expanduser()
    for arg in sys.argv[1:]:
        if arg.startswith("--env="):
            return Path(arg.split("=", 1)[1]).expanduser()
    from_env = os.environ.get("CLAUDE_ENV_FILE")
    if from_env:
        return Path(from_env).expanduser()
    return DEFAULT_PATH if DEFAULT_PATH.exists() else None


def load(path: str | os.PathLike | None = None,
         override: bool = False) -> tuple[Path | None, int]:
    """Apply a .env file. Returns (file, how many variables it set).

    Missing files are not an error unless you named one explicitly -- the whole
    point is that the file is optional.
    """
    target = find(path)
    if target is None:
        return None, 0
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        if path or "--env" in " ".join(sys.argv) or os.environ.get("CLAUDE_ENV_FILE"):
            raise RuntimeError(f"no env file at {target}") from None
        return None, 0
    except OSError as exc:
        raise RuntimeError(f"could not read {target}: {exc}") from exc

    applied = 0
    for key, value in parse(text).items():
        if override or key not in os.environ:
            os.environ[key] = value
            applied += 1
    return target, applied
