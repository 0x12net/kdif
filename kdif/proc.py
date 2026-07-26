"""Platform-dependent process/filesystem plumbing shared by the rest of kdif.

Three things differ enough between Linux, macOS and Windows to be worth
keeping in one place instead of spreading `sys.platform` checks around:

* **Console windows.** Every kicad-cli/git call is a console program. Started
  from a terminal that is invisible; started from the KiCad plugin (see
  ``plugin/panel.py``), which is a GUI process, Windows pops up and tears down
  a console window *per call* - dozens of black flashes for one diff. The
  ``CREATE_NO_WINDOW`` flag added by :func:`run` suppresses them, and is a
  no-op everywhere else.
* **Text decoding.** ``subprocess(text=True)`` and ``Path.read_text()`` decode
  with the locale encoding, which on a non-English Windows is a legacy
  codepage (cp1251, cp932, ...). KiCad files, kicad-cli output and git output
  are UTF-8 regardless of locale, so every read in kdif goes through
  :func:`run`/:func:`read_text` here, which pin UTF-8 explicitly.
* **Cache location.** ``~/.cache`` is a Linux convention; macOS and Windows
  have their own. All three candidates stay inside ``$HOME``, which flatpak
  KiCad requires to be able to read the temporary files at all (see
  ``cli.py``'s workdir handling).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence

IS_WINDOWS = sys.platform == "win32"

# CREATE_NO_WINDOW only exists on Windows builds of Python.
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}


def run(argv: Sequence[str], *, binary: bool = False,
        timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    """``subprocess.run`` with output captured, no console window and UTF-8.

    With ``binary=True`` stdout is left as bytes (``git archive`` tarballs);
    stderr is still decoded, since it is only ever shown in messages.
    """
    text_kwargs = {} if binary else {"encoding": "utf-8", "errors": "replace"}
    result = subprocess.run(argv, capture_output=True, timeout=timeout,
                            **text_kwargs, **_NO_WINDOW)
    if binary and isinstance(result.stderr, bytes):
        # stdout stays bytes (it is a tarball), but stderr only ever ends up
        # inside an error message, so decode it here once.
        result = subprocess.CompletedProcess(
            result.args, result.returncode, result.stdout,
            result.stderr.decode("utf-8", errors="replace"))
    return result


def read_text(path: Path) -> str:
    """UTF-8 read that never raises on malformed bytes (KiCad files are UTF-8,
    but a corrupt/partial one should surface as a parse error, not a traceback)."""
    return path.read_text(encoding="utf-8", errors="replace")


def write_text(path: Path, text: str) -> None:
    """UTF-8 write - the generated HTML carries whatever the title block says
    (Cyrillic, CJK, ...), which the Windows locale codepage cannot encode."""
    path.write_text(text, encoding="utf-8")


def split_command(cmd: str) -> List[str]:
    """Split a user-supplied command string into an argv list.

    POSIX ``shlex`` treats backslashes as escapes, which mangles the single
    most common Windows value (``C:\\Program Files\\KiCad\\9.0\\bin\\kicad-cli.exe``)
    into an unusable string. On Windows an existing file path is therefore
    taken verbatim, and anything else is split in non-POSIX mode, which keeps
    backslashes but leaves the quotes it split on in place - hence the strip.
    """
    if IS_WINDOWS:
        if cmd and Path(cmd.strip('"')).is_file():
            return [cmd.strip('"')]
        return [part.strip('"') for part in shlex.split(cmd, posix=False)]
    return shlex.split(cmd)


def default_workdir() -> Path:
    """Base directory for kdif's temporary export trees (see ``cli.py``)."""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "kdif" / "cache"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "kdif"
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        if xdg:
            return Path(xdg) / "kdif"
    return Path.home() / ".cache" / "kdif"
