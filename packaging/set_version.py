"""Write the version of the release being built into ``kdif/__init__.py``.

    python3 packaging/set_version.py v1.2.3     # (or 1.2.3)

``kdif.__version__`` is the one place the version is written down; everything
else derives from it - ``pyproject.toml`` reads it as a dynamic attribute, the
Makefile takes the ``.deb`` version from it, ``kdif --version`` and the KiCad
panel print it, and both packages ship a copy of the file. Keeping a release
number in the repository would mean bumping it in a commit *before* the tag it
describes exists, so the checked-in value is a ``0.0.0`` placeholder and the
release workflows call this script with ``github.ref_name`` before building.

Idempotent, and loud: an unparseable version or an ``__init__.py`` whose
``__version__`` line does not match exits non-zero rather than leaving the
placeholder in a released package.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = REPO_ROOT / "kdif" / "__init__.py"

# Same shape PCM/check_archive.py enforces on the metadata: digits and dots
# alone, so a tag that would only be rejected later fails here instead.
VERSION_PATTERN = re.compile(r"^\d{1,4}(\.\d{1,4}(\.\d{1,6})?)?$")
ASSIGNMENT = re.compile(r'^__version__ = ".*"$', re.MULTILINE)


def strip_v(version: str) -> str:
    """``v1.2.3`` -> ``1.2.3`` - git tags carry the ``v``, version fields do not."""
    return version[1:] if version.startswith("v") else version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", help="release version, e.g. v1.2.3 (or 1.2.3)")
    args = parser.parse_args()

    version = strip_v(args.version)
    if not VERSION_PATTERN.match(version):
        print(f"error: {args.version!r} is not a digits-and-dots version", file=sys.stderr)
        return 1

    text = INIT_PATH.read_text(encoding="utf-8")
    new_text, count = ASSIGNMENT.subn(f'__version__ = "{version}"', text)
    if count != 1:
        print(f"error: expected exactly one __version__ assignment in {INIT_PATH}, "
              f"found {count}", file=sys.stderr)
        return 1

    INIT_PATH.write_text(new_text, encoding="utf-8")
    print(f"{INIT_PATH.relative_to(REPO_ROOT)}: __version__ = {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
