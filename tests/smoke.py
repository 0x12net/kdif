#!/usr/bin/env python3
"""End-to-end smoke test: fixture repository -> kdif -> HTML, no KiCad needed.

    python3 tests/smoke.py

Runs the real CLI over the real fixture (tests/make_fixture.py) with
tests/fake_kicad_cli.py standing in for kicad-cli, then checks the generated
HTML actually carries the revisions, layers and sheets it should. CI runs this
on Linux, macOS and Windows - the parts of kdif most likely to break on one
platform only (process spawning, UTF-8 vs. locale encoding, path handling,
--kicad-cli command splitting) all sit on this path.

The working directory deliberately has a non-ASCII name: that is what catches
a read/write that decodes with the locale codepage rather than UTF-8.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_CLI = REPO_ROOT / "tests" / "fake_kicad_cli.py"


def run(argv, **kwargs) -> subprocess.CompletedProcess:
    print("$ " + " ".join(str(a) for a in argv))
    result = subprocess.run([str(a) for a in argv], capture_output=True,
                            encoding="utf-8", errors="replace", **kwargs)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise SystemExit(f"command failed with exit {result.returncode}")
    return result


def check(condition: bool, what: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {what}")
    if not condition:
        raise SystemExit(1)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        # Non-ASCII on purpose - see the module docstring.
        workdir = Path(tmp) / "проект"
        fixture = workdir / "плата"   # the repo name reaches the HTML payload
        workdir.mkdir()

        run([sys.executable, REPO_ROOT / "tests" / "make_fixture.py", fixture])

        out = workdir / "diff.html"
        # kicad-cli passed as a *command with arguments*, which is also how a
        # flatpak KiCad is passed - exercises the argv splitting in
        # kdif/proc.py's split_command() (backslash-heavy on Windows).
        result = run([sys.executable, "-m", "kdif",
                      "--commits", "3", "--worktree",
                      "--kicad-cli", f'"{sys.executable}" "{FAKE_CLI}"',
                      "-o", out, fixture / "demo.kicad_pro"],
                     cwd=REPO_ROOT)

        html = out.read_text(encoding="utf-8")
        print(f"generated {out} ({len(html) / 1024:.0f} KiB)")
        check(out.stat().st_size > 20_000, "HTML is a plausible size")
        check('"worktree":true' in html.replace(" ", ""), "working tree revision present")
        check(html.count('"name":') >= 4, "all four revisions present")
        check("Edge.Cuts" in html and "F.Cu" in html, "board layers present")
        check("F.Fab" in html, "fab layers present")
        check("footprint value field" in result.stderr,
              "footprint values on fab layers hidden by default")
        check('"Power"' in html or "Power" in html, "hierarchical sheet present")
        check("Demo Widget" in html, "title block present")
        # Two different halves of the encoding story: the repository name
        # travelled through git/subprocess/JSON (json.dumps escapes it, so it
        # is the \\uXXXX form that lands in the file), while the template's own
        # "→"/"●" are written out literally - those are what a locale-encoded
        # write (cp1251 & co. cannot represent them) would blow up on.
        check("\\u043f\\u043b\\u0430\\u0442\\u0430" in html, "non-ASCII repository name reached the payload")
        check("→" in html and "●" in html, "template's non-ASCII characters survived the round trip")

        # A single document (schematic only) takes a different code path.
        sch_out = workdir / "sch.html"
        run([sys.executable, "-m", "kdif", "--tags", "2",
             "--kicad-cli", f'"{sys.executable}" "{FAKE_CLI}"',
             "-o", sch_out, fixture / "demo.kicad_sch"],
            cwd=REPO_ROOT)
        check(sch_out.is_file(), "schematic-only diff generated")

    print("smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
