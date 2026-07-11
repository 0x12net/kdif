"""Assemble the final single-file HTML from the template and export results."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import List, Optional

from .exporter import Layer, Sheet, SvgResult
from .gitrepo import Rev

TEMPLATE = Path(__file__).parent / "template" / "viewer.html"


def _entry(s: Optional[SvgResult]):
    if s is None:
        return None
    return {"d": s.data_b64, "h": s.sha, "vb": list(s.viewbox),
            "u": round(s.mm_per_unit, 9)}


def build_html(title: str, board_rel: str, repo_name: str,
               revs: List[Rev],
               layers: Optional[List[Layer]],
               per_rev_svgs: Optional[List[List[SvgResult]]],
               sheets: Optional[List[Sheet]],
               per_rev_sheet_svgs: Optional[List[List[Optional[SvgResult]]]],
               compressed: bool, kicad_version: str,
               pcb_title_blocks: Optional[List[dict]] = None,
               sch_title_blocks: Optional[List[dict]] = None) -> str:
    data = {
        "title": title,
        "board": board_rel,
        "repo": repo_name,
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "kicadVersion": kicad_version,
        "compressed": compressed,
        "revs": [
            {
                "name": r.name,
                "short": r.short,
                "date": (r.date[:10] if r.date else ""),
                "author": r.author,
                "subject": r.subject,
                "worktree": r.is_worktree,
            }
            for r in revs
        ],
        "pcb": None,
        "sch": None,
    }
    if layers is not None and per_rev_svgs is not None:
        data["pcb"] = {
            "layers": [
                {"name": l.canonical, "display": l.display, "color": l.color}
                for l in layers
            ],
            "svgs": [[_entry(s) for s in rev_results] for rev_results in per_rev_svgs],
            "titleBlocks": pcb_title_blocks or [],
        }
    if sheets is not None and per_rev_sheet_svgs is not None:
        data["sch"] = {
            "sheets": [{"display": s.display} for s in sheets],
            "svgs": [[_entry(s) for s in rev_results]
                     for rev_results in per_rev_sheet_svgs],
            "titleBlocks": sch_title_blocks or [],
        }
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    template = TEMPLATE.read_text()
    marker = "\"__KHD_DATA__\""
    if marker not in template:
        raise RuntimeError("template marker not found")
    return template.replace(marker, payload, 1)
