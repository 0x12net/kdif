# kdif

![](assets/image0.png)
![](assets/image1.png)

Generate an **interactive HTML diff of a KiCad board and schematic** between git
revisions. A single self-contained HTML file in the spirit of
[InteractiveHtmlBom](https://github.com/openscopeproject/InteractiveHtmlBom):
dark/light theme, a layer panel, and a canvas with pan/zoom.

The drawing is done by `kicad-cli` (KiCad ≥ 7) and the revisions come from the
project's git repository — the working tree is never switched. Use it as a
command-line tool, or install it into KiCad through the **Plugin and Content
Manager** and press a toolbar button instead. Linux, macOS and Windows.

## Features

* **Comparison modes**: diff overlay (red = only in A, green = only in B,
  yellow = unchanged), a crossfade slider, viewing A/B individually, and
  split-view.
* **Schematic**: the sidebar lists the sheets next to the layers — clicking a
  sheet shows the schematic diff, clicking a layer returns to the board.
  **Multi-sheet (hierarchical) schematics** are supported — each sheet is
  compared separately, changed sheets are marked with a dot, and
  added/removed sheets are shown entirely in green/red.
* **Layers**: toggle each layer on/off, All / None / Changed buttons,
  double-click a layer to solo it. Layers that differ between A and B are
  marked with a dot.
* **Fab layers stay readable**: footprint value fields are not plotted on
  `F.Fab`/`B.Fab` (a value next to every part, dragged along by every part that
  moved, buries the change you are looking for). Reference designators are
  kept, and `--show-fab-values` brings the values back.
* **Comparison points**: any number of revisions in one file — the A/B pair is
  selected with radio buttons right inside the viewer.
* **Title block**: the bottom of the sidebar shows title / rev / date / company
  / comment from the project — separately for the board and the schematic;
  fields that changed between A and B are shown as `old → new`.
* The HTML itself has no dependencies — attach it to a release or send it to a
  colleague.

## Installation

### As a KiCad plugin (any OS)

In KiCad: **Plugin and Content Manager → Manage... → +**, and add the
repository URL

```
https://github.com/0x12net/kdif/releases/download/pcm-repository/repository.json
```

then pick *kdif* and **Apply Pending Changes**. A **kdif** button appears on
the pcbnew toolbar; updates arrive through PCM like any other package. To try
a single build without adding the repository, download `kdif_*.zip` from the
[latest release](../../releases/latest) and use *Install from File...*.

Requires KiCad ≥ 9 with *Preferences → Plugins → Enable KiCad API*, and `git`
on the system. See [plugin/README.md](plugin/README.md) for what the panel
does and [PCM/README.md](PCM/README.md) for how the package is built.

### As a command-line tool

Download the `.deb` from the [latest release](../../releases/latest) and install it:

```Shell
sudo apt install ./kdif_*.deb     # pulls in python3; recommends kicad
```

Or install from source (works on Linux, macOS and Windows — kdif needs only
Python ≥ 3.9, git and KiCad):

```Shell
pipx install /path/to/kdif        # or: pip install -e .
```

Or without installing: run `python3 -m kdif ...` from the project directory.

You can also build the packages yourself: `make deb` (needs `dpkg-deb`) and
`make pcm` / `python3 PCM/create_pcm_archive.py v1.0.0` for the KiCad package.

## Usage

The first argument is always the path to a `.kicad_pro` project file — both the
board and the schematic (whichever exist in the repository) are included in the
diff. To compare a single document, point directly at a `.kicad_pcb` or
`.kicad_sch`. The git repository is located from this path.

```Shell
# last two commits (default)
kdif hardware/main.kicad_pro

# specific commits/tags/branches (comma-separated lists allowed)
kdif -r v1.0 -r v2.0 hardware/main.kicad_pro
kdif -r 41acedf,main hardware/main.kicad_pro

# last N tags / N commits
kdif --tags 5 hardware/main.kicad_pro
kdif --commits 10 hardware/main.kicad_pro

# also include the current (uncommitted) working tree
kdif --worktree hardware/main.kicad_pro

# select layers and the output file name (all layers are exported by default)
kdif -l F.Cu,B.Cu,Edge.Cuts -o diff.html hardware/main.kicad_pro

# schematic only / board only
kdif hardware/main.kicad_sch
kdif hardware/main.kicad_pcb
```

The result is `<board_name>-diff.html`, which opens in any modern browser.

### Main options

| Option                     | Description                                                     |
| -------------------------- | --------------------------------------------------------------- |
| `-r, --ref REF`            | commit/tag/branch; repeatable, comma-separated lists allowed    |
| `--commits N` / `--tags N` | last N commits / tags                                           |
| `--worktree`               | add the uncommitted state as a`worktree` revision               |
| `-l, --layers LIST`        | comma-separated layers (default: all board layers)              |
| `-o, --output FILE`        | output HTML file                                                |
| `--kicad-cli CMD`          | kicad-cli command or path (default: autodetected, see below)    |
| `-j, --jobs N`             | parallel kicad-cli processes (default: 4)                       |
| `--check-zones`            | refill zones before exporting (KiCad ≥ 8)                       |
| `--show-fab-values`        | plot footprint values on the`*.Fab` layers (hidden by default)  |
| `--no-compress`            | do not compress the SVG inside the HTML (for very old browsers) |

### Viewer controls

drag — pan · wheel — zoom · **F** — fit the board · **1–5** — modes ·
**S** — swap A and B · double-click a layer — solo · **PNG** button — save the
current view.

## Finding kicad-cli

kdif looks for `kicad-cli` in this order: `--kicad-cli`, `$KICAD_CLI`, `PATH`,
the standard install location for the platform, a flatpak KiCad. Only Linux
distributions put it on `PATH`, so on the other two the third step is what
normally finds it:

```Shell
# Windows
kdif --kicad-cli "C:\Program Files\KiCad\9.0\bin\kicad-cli.exe" hw\board.kicad_pro
# macOS
kdif --kicad-cli '/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli' hw/board.kicad_pro
# flatpak KiCad (detected automatically, or pass it explicitly)
kdif --kicad-cli 'flatpak run --command=kicad-cli org.kicad.KiCad' hw/board.kicad_pro
```

Temporary files go to the user cache directory (`~/.cache/kdif`,
`~/Library/Caches/kdif`, `%LOCALAPPDATA%\kdif\cache`), which is inside `$HOME`
because that is all the flatpak sandbox can see. Override with `--workdir`.

## Try it on a fixture

[tests/make\_fixture.py](tests/make_fixture.py) builds a deterministic KiCad git
repository (a board and a hierarchical schematic across 3 revisions and 2 tags)
that exercises every diff feature — handy for a quick look or a smoke test:

```Shell
python3 tests/make_fixture.py ~/demo-board
kdif --commits 3 ~/demo-board/demo.kicad_pro
```

[tests/smoke.py](tests/smoke.py) runs that fixture through the whole pipeline
with a stand-in for `kicad-cli`, so it needs no KiCad install — this is what
CI runs on Linux, macOS and Windows on every push.

## How it works

1. Revisions are extracted with `git archive` (the repository is untouched and
   the working tree is never switched). For the schematic, **all** `.kicad_sch`
   files of the revision are extracted with their paths preserved, so
   hierarchical sheets find their files.
2. For every revision and every layer, `kicad-cli pcb export svg --black-and-white --exclude-drawing-sheet --page-size-mode 0` — the full page
   keeps the coordinate anchoring consistent across revisions. The schematic is
   exported with a single `kicad-cli sch export svg` call (all pages at once,
   **with the drawing sheet and title block**); the sheet hierarchy is read from
   the `.kicad_sch` files and pages are matched between revisions by sheet name.
3. A "brightness → alpha" filter is embedded in the SVG, so KiCad's white
   "knocked-out" objects (knockout text, etc.) correctly become transparent.
4. The SVGs are compressed (deflate) and embedded in the HTML; the browser
   decompresses them via `DecompressionStream`, colours them by the alpha
   channel into the layer colour, and blends them additively — matching areas
   turn yellow.

