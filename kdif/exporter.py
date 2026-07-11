"""kicad-cli discovery/invocation, board layer parsing, SVG post-processing."""

from __future__ import annotations

import base64
import hashlib
import re
import shlex
import shutil
import subprocess
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

FLATPAK_APP = "org.kicad.KiCad"

# KiCad-ish default colors per canonical layer name.
LAYER_COLORS = {
    "F.Cu": "#C83434",
    "B.Cu": "#4D7FC4",
    "In1.Cu": "#7FC87F",
    "In2.Cu": "#CE7D2C",
    "In3.Cu": "#4D4D4D",
    "In4.Cu": "#DE4FBD",
    "In5.Cu": "#A0A0C8",
    "In6.Cu": "#22B379",
    "F.SilkS": "#F2EDA1",
    "B.SilkS": "#E8B2A7",
    "F.Mask": "#B078D8",
    "B.Mask": "#77BFAF",
    "F.Paste": "#B4949B",
    "B.Paste": "#00B3B3",
    "F.Fab": "#AFAFAF",
    "B.Fab": "#585D84",
    "F.CrtYd": "#FF26E2",
    "B.CrtYd": "#26E9FF",
    "Edge.Cuts": "#D0D2CD",
    "Dwgs.User": "#C2C2C2",
    "Cmts.User": "#7897C3",
    "Margin": "#FF26E2",
    "F.Adhes": "#A45BAC",
    "B.Adhes": "#3545A8",
}

# Visual stacking order in the viewer (top of the list = drawn on top).
LAYER_ORDER_HINT = [
    "Edge.Cuts",
    "Dwgs.User", "Cmts.User", "Margin",
    "F.Fab", "F.CrtYd", "F.SilkS", "F.Paste", "F.Mask", "F.Adhes",
    "F.Cu",
    # inner copper handled dynamically
    "B.Cu",
    "B.Adhes", "B.Mask", "B.Paste", "B.SilkS", "B.CrtYd", "B.Fab",
]


class ExportError(RuntimeError):
    pass


@dataclass
class Layer:
    canonical: str   # name as written in the board file, e.g. "F.SilkS"
    display: str     # user-facing name, e.g. "F.Silkscreen"
    color: str


@dataclass
class Sheet:
    """One schematic page (root or hierarchical sub-sheet)."""

    path_names: Tuple[str, ...]  # () for root, ("Power", "LDO") for nested sheets
    display: str                 # "Root" or "Power / LDO"
    svg_name: str                # basename kicad-cli gives the plotted SVG


@dataclass
class SvgResult:
    data_b64: str            # deflate-raw + base64 (or plain base64 if no compression)
    sha: str                 # hash of cleaned svg, for change detection
    viewbox: Tuple[float, float, float, float]
    mm_per_unit: float       # physical mm per viewBox user unit
    raw_size: int


def find_kicad_cli(user_cmd: Optional[str]) -> List[str]:
    """Return the kicad-cli command as an argv prefix."""
    if user_cmd:
        return shlex.split(user_cmd)
    if shutil.which("kicad-cli"):
        return ["kicad-cli"]
    raise ExportError(
        "kicad-cli not found in PATH. Install KiCad (>= 7) or pass --kicad-cli, e.g.\n"
        f"  --kicad-cli 'flatpak run --command=kicad-cli {FLATPAK_APP}'"
    )


def kicad_cli_version(cmd: Sequence[str]) -> Tuple[int, str]:
    """Return (major, full_version_string). Falls back to (0, '') on failure."""
    try:
        proc = subprocess.run([*cmd, "version"], capture_output=True, text=True, timeout=120)
        ver = proc.stdout.strip().splitlines()[-1].strip() if proc.stdout.strip() else ""
        m = re.match(r"(\d+)\.", ver)
        return (int(m.group(1)) if m else 0, ver)
    except Exception:
        return (0, "")


def is_flatpak_cmd(cmd: Sequence[str]) -> bool:
    return Path(cmd[0]).name == "flatpak"


# ---------------------------------------------------------------- board file

def parse_board_layers(board_path: Path) -> List[Layer]:
    """Parse the (layers ...) block of a .kicad_pcb file."""
    text = board_path.read_text(errors="replace")
    idx = text.find("(layers")
    if idx < 0:
        raise ExportError(f"no (layers ...) block found in {board_path.name}")
    # scan balanced parens
    depth = 0
    end = idx
    for i in range(idx, len(text)):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    block = text[idx:end]
    layers: List[Layer] = []
    for m in re.finditer(r'\(\s*\d+\s+"([^"]+)"\s+\w+(?:\s+"([^"]+)")?\s*\)', block):
        canonical = m.group(1)
        display = m.group(2) or canonical
        layers.append(Layer(canonical=canonical, display=display, color=""))
    if not layers:
        raise ExportError(f"could not parse layers from {board_path.name}")
    return layers


def _inner_cu_key(name: str) -> Optional[int]:
    m = re.match(r"In(\d+)\.Cu$", name)
    return int(m.group(1)) if m else None


def sort_layers(layers: List[Layer]) -> List[Layer]:
    """Order layers for the UI list / draw stacking."""
    hint = {name: i for i, name in enumerate(LAYER_ORDER_HINT)}
    f_cu = hint["F.Cu"]

    def key(layer: Layer):
        n = layer.canonical
        inner = _inner_cu_key(n)
        if inner is not None:
            return (f_cu + 0.001 * inner, n)
        if n in hint:
            return (float(hint[n]), n)
        return (1000.0, n)  # unknown/user layers at the bottom

    return sorted(layers, key=key)


def _auto_color(name: str, index: int) -> str:
    if name in LAYER_COLORS:
        return LAYER_COLORS[name]
    inner = _inner_cu_key(name)
    if inner is not None:
        # golden-angle hue walk for many inner layers
        hue = (60 + inner * 137.5) % 360
        return _hsl_hex(hue, 0.55, 0.55)
    hue = (200 + index * 137.5) % 360
    return _hsl_hex(hue, 0.45, 0.6)


def _hsl_hex(h: float, s: float, lightness: float) -> str:
    import colorsys
    r, g, b = colorsys.hls_to_rgb(h / 360.0, lightness, s)
    return "#{:02X}{:02X}{:02X}".format(int(r * 255), int(g * 255), int(b * 255))


def select_layers(all_layers: List[Layer], requested: Optional[str]) -> List[Layer]:
    """Filter board layers by --layers argument (comma list; default/'all' = every layer)."""
    by_name: Dict[str, Layer] = {}
    for layer in all_layers:
        by_name[layer.canonical] = layer
        by_name[layer.display] = layer

    if requested and requested.strip().lower() != "all":
        picked: List[Layer] = []
        missing: List[str] = []
        for token in requested.split(","):
            token = token.strip()
            if not token:
                continue
            layer = by_name.get(token)
            if layer is None:
                missing.append(token)
            elif layer not in picked:
                picked.append(layer)
        if missing:
            known = ", ".join(l.canonical for l in all_layers)
            raise ExportError(
                f"unknown layer(s): {', '.join(missing)}\navailable: {known}"
            )
        chosen = picked
    else:
        chosen = list(all_layers)

    chosen = sort_layers(chosen)
    for i, layer in enumerate(chosen):
        layer.color = _auto_color(layer.canonical, i)
    return chosen


# ---------------------------------------------------------------- schematic

_RE_SHEET_OPEN = re.compile(r"\(sheet[\s(]")
# KiCad 6 wrote "Sheet name"/"Sheet file", KiCad 7+ writes "Sheetname"/"Sheetfile"
_RE_SHEET_NAME = re.compile(r'\(property\s+"Sheet ?name"\s+"((?:[^"\\]|\\.)*)"')
_RE_SHEET_FILE = re.compile(r'\(property\s+"Sheet ?file"\s+"((?:[^"\\]|\\.)*)"')


def _sexp_block(text: str, start: int) -> str:
    """Balanced-paren block starting at text[start] == '(', quote-aware."""
    depth = 0
    in_str = False
    i = start
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return text[start:]


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\")


def parse_sheet_tree(root_sch: Path) -> List[Sheet]:
    """Walk the sheet hierarchy of a schematic, document order (depth first).

    kicad-cli plots page SVGs named <root>-<Name1>-<Name2>....svg; the root
    page is just <root>.svg."""
    root_stem = root_sch.stem
    sheets: List[Sheet] = []

    def walk(file: Path, names: Tuple[str, ...], stack: Tuple[Path, ...]) -> None:
        sheets.append(Sheet(
            path_names=names,
            display=" / ".join(names) if names else "Root",
            svg_name=root_stem + "".join("-" + n for n in names) + ".svg",
        ))
        try:
            text = file.read_text(errors="replace")
        except OSError:
            return  # sheet file absent in this revision: page will be missing
        for m in _RE_SHEET_OPEN.finditer(text):
            block = _sexp_block(text, m.start())
            nm = _RE_SHEET_NAME.search(block)
            fm = _RE_SHEET_FILE.search(block)
            if not fm:
                continue
            name = _unescape(nm.group(1)) if nm else "Sheet"
            try:
                child = (file.parent / _unescape(fm.group(1))).resolve()
            except OSError:
                continue
            if child in stack:  # cycle guard (re-use of a file elsewhere is fine)
                continue
            walk(child, names + (name,), stack + (child,))

    root = root_sch.resolve()
    walk(root, (), (root,))
    return sheets


_RE_TB_STR = r'"((?:[^"\\]|\\.)*)"'
_RE_TB_COMMENT = re.compile(r"\(comment\s+(\d+)\s+" + _RE_TB_STR)


def parse_title_block(path: Path) -> Dict[str, object]:
    """Extract the (title_block ...) of a .kicad_pcb / .kicad_sch file.

    Returns a dict with any of title/date/rev/company and a "comments" list;
    empty when the file has no title block (or is absent in this revision)."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {}
    idx = text.find("(title_block")
    if idx < 0:
        return {}
    block = _sexp_block(text, idx)
    out: Dict[str, object] = {}
    for field in ("title", "date", "rev", "company"):
        m = re.search(r"\(" + field + r"\s+" + _RE_TB_STR + r"\s*\)", block)
        if m:
            value = _unescape(m.group(1)).strip()
            if value:
                out[field] = value
    comments = {int(m.group(1)): _unescape(m.group(2)).strip()
                for m in _RE_TB_COMMENT.finditer(block)}
    ordered = [comments[k] for k in sorted(comments) if comments[k]]
    if ordered:
        out["comments"] = ordered
    return out


def _fs_safe(name: str) -> str:
    """Loose normalization to match kicad-cli's output-filename sanitizing."""
    return re.sub(r"[^A-Za-z0-9._-]+", "", name).lower()


def match_sheet_svgs(sheets: List[Sheet], svg_dir: Path) -> List[Optional[Path]]:
    """Map each sheet to its plotted SVG file (None when the page is absent)."""
    files = {f.name: f for f in svg_dir.glob("*.svg")}
    by_safe = {_fs_safe(n): f for n, f in files.items()}
    out: List[Optional[Path]] = []
    for sh in sheets:
        f = files.pop(sh.svg_name, None)
        if f is None:
            f = by_safe.get(_fs_safe(sh.svg_name))
            if f is not None and f.name not in files:
                f = None  # already claimed
            elif f is not None:
                files.pop(f.name, None)
        out.append(f)
    return out


# ---------------------------------------------------------------- svg export

_RE_TITLE = re.compile(r"<title>.*?</title>", re.S)
_RE_DESC = re.compile(r"<desc>.*?</desc>", re.S)
_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_VIEWBOX = re.compile(r'viewBox="([\d.\-eE ]+)"')
_RE_WH = re.compile(r'(<svg[^>]*?)\swidth="[^"]*"\s+height="[^"]*"')
_RE_WIDTH = re.compile(r'<svg[^>]*?\swidth="([\d.eE\-]+)\s*(mm|cm|in|px)?"')
_RE_SVG_OPEN = re.compile(r"<svg\b[^>]*>", re.S)

_UNIT_MM = {"mm": 1.0, "cm": 10.0, "in": 25.4, "px": 25.4 / 96.0, None: 25.4 / 96.0, "": 25.4 / 96.0}

# KiCad B&W plots draw ink in black and "erase" objects (knockout text
# backgrounds etc.) in white.  The viewer colorizes layers through the alpha
# channel, so we bake a luminance->alpha conversion into the SVG itself:
# composite everything over white, then alpha = 1 - luminance, rgb = white.
_MASK_FILTER = (
    '<defs><filter id="khdmask" color-interpolation-filters="sRGB">'
    '<feFlood flood-color="#FFFFFF" result="khdbg"/>'
    '<feComposite in="SourceGraphic" in2="khdbg" operator="over" result="khdflat"/>'
    '<feColorMatrix in="khdflat" type="matrix" values="'
    "0 0 0 0 1  0 0 0 0 1  0 0 0 0 1  -0.35 -0.5 -0.15 0 1"
    '"/></filter></defs><g filter="url(#khdmask)">'
)


def clean_svg(text: str) -> Tuple[str, Tuple[float, float, float, float], float]:
    """Strip volatile metadata, normalize size attrs.

    Returns (svg_text, viewBox, mm_per_unit)."""
    text = _RE_TITLE.sub("", text, count=1)
    text = _RE_DESC.sub("", text, count=1)
    text = _RE_COMMENT.sub("", text)

    m = _RE_VIEWBOX.search(text)
    if not m:
        raise ExportError("SVG output has no viewBox")
    parts = [float(x) for x in m.group(1).split()]
    if len(parts) != 4:
        raise ExportError(f"unexpected viewBox: {m.group(1)!r}")
    vb = (parts[0], parts[1], parts[2], parts[3])

    # physical size before we rewrite it -> mm per user unit
    mm_per_unit = 1.0
    wm = _RE_WIDTH.search(text)
    if wm and vb[2] > 0:
        mm_per_unit = float(wm.group(1)) * _UNIT_MM.get(wm.group(2), 1.0) / vb[2]

    # Replace physical (cm/mm) width/height with px so every browser
    # rasterizes at a usable base resolution.
    scale = min(8.0 * mm_per_unit, 4000.0 / max(vb[2], vb[3], 1e-6))  # px per unit, capped
    w_px = max(1, round(vb[2] * scale))
    h_px = max(1, round(vb[3] * scale))
    text = _RE_WH.sub(rf'\1 width="{w_px}" height="{h_px}"', text, count=1)

    # wrap all drawing in the luminance->alpha filter group
    m = _RE_SVG_OPEN.search(text)
    close = text.rfind("</svg>")
    if m and close > m.end():
        text = text[:m.end()] + _MASK_FILTER + text[m.end():close] + "</g>" + text[close:]
    return text, vb, mm_per_unit


class KicadExporter:
    def __init__(self, cmd: Sequence[str], major: int, page_size_mode: int = 0,
                 check_zones: bool = False, compress: bool = True):
        self.cmd = list(cmd)
        self.major = major
        self.page_size_mode = page_size_mode
        self.check_zones = check_zones
        self.compress = compress

    def _build_argv(self, board: Path, layer: str, out_svg: Path) -> List[str]:
        argv = [*self.cmd, "pcb", "export", "svg",
                "--layers", layer,
                "--black-and-white",
                "--exclude-drawing-sheet",
                "--page-size-mode", str(self.page_size_mode),
                "--output", str(out_svg)]
        if self.major >= 9:
            argv.append("--mode-single")
        if self.check_zones and self.major >= 8:
            argv.append("--check-zones")
        argv.append(str(board))
        return argv

    def process_svg(self, svg_path: Path) -> SvgResult:
        text = svg_path.read_text(errors="replace")
        cleaned, vb, mm_per_unit = clean_svg(text)
        raw = cleaned.encode()
        sha = hashlib.sha1(raw).hexdigest()[:12]
        if self.compress:
            comp = zlib.compressobj(level=9, wbits=-15)
            payload = comp.compress(raw) + comp.flush()
        else:
            payload = raw
        return SvgResult(
            data_b64=base64.b64encode(payload).decode("ascii"),
            sha=sha,
            viewbox=vb,
            mm_per_unit=mm_per_unit,
            raw_size=len(raw),
        )

    def export_layer(self, board: Path, layer: str, out_svg: Path) -> SvgResult:
        argv = self._build_argv(board, layer, out_svg)
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0 or not out_svg.is_file():
            err = (proc.stderr or proc.stdout or "").strip()
            raise ExportError(
                f"kicad-cli failed for layer {layer} of {board.name}:\n{err}"
            )
        return self.process_svg(out_svg)

    def export_all(self, jobs: List[Tuple[Path, str, Path]], workers: int,
                   progress=None) -> List[SvgResult]:
        """jobs: list of (board_path, layer, out_svg). Returns results in order."""
        results: List[Optional[SvgResult]] = [None] * len(jobs)

        def run(i: int):
            board, layer, out = jobs[i]
            results[i] = self.export_layer(board, layer, out)
            if progress:
                progress(i, layer)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(run, range(len(jobs))))
        return results  # type: ignore[return-value]

    def export_schematic(self, root_sch: Path, out_dir: Path) -> None:
        """Plot every page of a schematic (one kicad-cli call) into out_dir.

        The drawing sheet (format frame + title block) is kept — it is part of
        the schematic and stays aligned across revisions (page size is fixed)."""
        out_dir.mkdir(parents=True, exist_ok=True)
        argv = [*self.cmd, "sch", "export", "svg",
                "--black-and-white",
                "--no-background-color",
                "--output", str(out_dir),
                str(root_sch)]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0 or not any(out_dir.glob("*.svg")):
            err = (proc.stderr or proc.stdout or "").strip()
            raise ExportError(f"kicad-cli failed for schematic {root_sch.name}:\n{err}")

    def export_schematics(self, jobs: List[Tuple[Optional[Path], Path]], workers: int,
                          progress=None) -> None:
        """jobs: list of (root_sch or None, out_dir); None roots are skipped."""
        def run(i: int):
            root, out = jobs[i]
            if root is not None:
                self.export_schematic(root, out)
            if progress:
                progress(i, "schematic")

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(run, range(len(jobs))))
