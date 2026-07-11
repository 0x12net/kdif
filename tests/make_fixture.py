#!/usr/bin/env python3
"""Build a deterministic KiCad fixture repository for exercising kdif.

Generates a git repo whose board and (hierarchical) schematic change across
three tagged revisions, covering every diff feature: tracks/vias added, moved
and deleted, board-outline growth, silkscreen and title-block changes, and a
schematic sheet that appears only in the last revision. UUIDs are derived from a
fixed namespace so the output is byte-reproducible.

Usage: python3 tests/make_fixture.py [output_dir]   (default: ./fixture)
"""

import subprocess
import sys
import uuid
from pathlib import Path

NS = uuid.UUID("12345678-1234-5678-1234-567812345678")


def u(tag: str) -> str:
    return str(uuid.uuid5(NS, tag))


def title_block(rev: int, doc: str) -> str:
    """A KiCad title block that changes across revisions and differs per doc."""
    date = {1: "2025-01-10", 2: "2025-03-22", 3: "2025-06-05"}[rev]
    who = "Board: Alice" if doc == "pcb" else "Schematic: Bob"
    n = 1 if doc == "pcb" else 2
    return (f'(title_block\n'
            f'    (title "Demo Widget")\n'
            f'    (date "{date}")\n'
            f'    (rev "v0.{rev}.0")\n'
            f'    (company "Demo Co")\n'
            f'    (comment {n} "{who}")\n'
            f'  )')


def board(rev: int) -> str:
    items = []

    def add(s):
        items.append("  " + s)

    # board outline (grows in rev 3)
    right = 160 if rev < 3 else 175
    add(f'(gr_rect (start 100 80) (end {right} 120) '
        f'(stroke (width 0.15) (type default)) (fill none) (layer "Edge.Cuts") (uuid "{u("edge")}"))')

    # F.Cu tracks
    add(f'(segment (start 105 85) (end 150 85) (width 0.3) (layer "F.Cu") (net 0) (uuid "{u("t1")}"))')
    add(f'(segment (start 105 90) (end 150 90) (width 0.3) (layer "F.Cu") (net 0) (uuid "{u("t2")}"))')
    if rev >= 2:  # extra route appears in rev 2, then moves in rev 3
        y = 95 if rev == 2 else 97
        add(f'(segment (start 105 {y}) (end 150 {y}) (width 0.5) (layer "F.Cu") (net 0) (uuid "{u("t3")}"))')
        add(f'(segment (start 150 {y}) (end 150 110) (width 0.5) (layer "F.Cu") (net 0) (uuid "{u("t4")}"))')
    if rev == 1:  # this one is deleted after rev 1
        add(f'(segment (start 105 100) (end 120 115) (width 0.25) (layer "F.Cu") (net 0) (uuid "{u("t5")}"))')

    # B.Cu
    add(f'(segment (start 110 115) (end 155 115) (width 0.4) (layer "B.Cu") (net 0) (uuid "{u("b1")}"))')
    if rev >= 3:
        add(f'(segment (start 165 85) (end 165 115) (width 0.4) (layer "B.Cu") (net 0) (uuid "{u("b2")}"))')

    # vias
    add(f'(via (at 150 85) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 0) (uuid "{u("v1")}"))')
    if rev >= 2:
        add(f'(via (at 150 110) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 0) (uuid "{u("v2")}"))')

    # silkscreen
    label = {1: "DEMO r1", 2: "DEMO r2", 3: "DEMO r3"}[rev]
    add(f'(gr_text "{label}" (at 128 105 0) (layer "F.SilkS") (uuid "{u("txt")}") '
        f'(effects (font (size 2 2) (thickness 0.35))))')
    add(f'(gr_circle (center 110 110) (end 113 110) '
        f'(stroke (width 0.2) (type default)) (fill none) (layer "F.SilkS") (uuid "{u("c1")}"))')

    # mask openings
    add(f'(gr_rect (start 104 84) (end 108 88) '
        f'(stroke (width 0.05) (type default)) (fill yes) (layer "F.Mask") (uuid "{u("m1")}"))')

    body = "\n".join(items)
    return f"""(kicad_pcb
  (version 20240108)
  (generator "pcbnew")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  {title_block(rev, "pcb")}
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
{body}
)
"""


def sch_text(title: str, body: str, tag: str, tb: str = None) -> str:
    tb = tb or f'(title_block (title "{title}"))'
    return f"""(kicad_sch
  (version 20250114)
  (generator "eeschema")
  (generator_version "9.0")
  (uuid "{u("sch-" + tag)}")
  (paper "A4")
  {tb}
  (lib_symbols)
{body}
)
"""


def sheet_ref(name: str, file: str, x: int, y: int, tag: str) -> str:
    return f"""  (sheet
    (at {x} {y})
    (size 30 20)
    (stroke (width 0.1524) (type solid))
    (fill (color 0 0 0 0.0000))
    (uuid "{u("sheet-" + tag)}")
    (property "Sheetname" "{name}"
      (at {x} {y - 1} 0)
      (effects (font (size 1.27 1.27)) (justify left bottom))
    )
    (property "Sheetfile" "{file}"
      (at {x} {y + 21} 0)
      (effects (font (size 1.27 1.27)) (justify left top))
    )
  )"""


def text_item(text: str, x: int, y: int, tag: str) -> str:
    return (f'  (text "{text}"\n    (at {x} {y} 0)\n'
            f'    (effects (font (size 5 5)))\n    (uuid "{u("txt-" + tag)}")\n  )')


def write_schematic(out: Path, rev: int) -> None:
    """Root sheet + 'Power' sub-sheet; rev 2 moves text, rev 3 adds an 'MCU' page."""
    root = [text_item(f"ROOT r{rev}", 60, 40, "root"),
            sheet_ref("Power", "power.kicad_sch", 50, 60, "pw")]
    if rev >= 3:
        root.append(sheet_ref("MCU", "mcu.kicad_sch", 100, 60, "mcu"))
    root.append('  (sheet_instances (path "/" (page "1")))')
    (out / "demo.kicad_sch").write_text(
        sch_text("Demo root", "\n".join(root), "root", tb=title_block(rev, "sch")))

    y = 120 if rev < 2 else 140
    (out / "power.kicad_sch").write_text(
        sch_text("Power", text_item("POWER RAIL", 100, y, "pwr"), "power"))
    if rev >= 3:
        (out / "mcu.kicad_sch").write_text(
            sch_text("MCU", text_item("MCU PAGE", 100, 100, "mcu"), "mcu"))


def run(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "fixture").resolve()
    if out.exists() and any(out.iterdir()):
        sys.exit(f"error: {out} already exists and is not empty")
    out.mkdir(parents=True, exist_ok=True)

    run(out, "git", "init", "-q", "-b", "main")
    run(out, "git", "config", "user.email", "demo@example.com")
    run(out, "git", "config", "user.name", "Demo")

    pcb = out / "demo.kicad_pcb"
    (out / "demo.kicad_pro").write_text(
        '{"meta": {"filename": "demo.kicad_pro", "version": 3}}\n')
    steps = [
        (1, "Initial board layout", "v1.0"),
        (2, "Add power route and via", "v2.0"),
        (3, "Grow board, move power route, add MCU sheet", None),
    ]
    for rev, msg, tag in steps:
        pcb.write_text(board(rev))
        write_schematic(out, rev)
        run(out, "git", "add", "-A")
        run(out, "git", "commit", "-q", "-m", msg)
        if tag:
            run(out, "git", "tag", tag)

    pro = out / "demo.kicad_pro"
    print(f"fixture repository created at {out}")
    print("try:  kdif " + str(pro))
    print("      kdif --tags 2 " + str(pro))
    print("      kdif --commits 3 " + str(pro))


if __name__ == "__main__":
    main()
