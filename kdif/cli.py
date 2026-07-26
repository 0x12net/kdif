"""Command-line entry point."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

from . import __version__
from . import gitrepo
from . import proc
from .gitrepo import GitError, Rev, WORKTREE_REF
from .exporter import (
    ExportError, KicadExporter, Sheet, find_kicad_cli, hide_fab_values,
    is_flatpak_cmd, kicad_cli_version, match_sheet_svgs, parse_board_layers,
    parse_sheet_tree, parse_title_block, select_layers,
)
from .htmlgen import build_html


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="kdif",
        description="Generate an interactive single-file HTML diff of a KiCad PCB "
                    "and schematic between git revisions (drives kicad-cli; the same "
                    "tool is also installable into KiCad through the PCM).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  kdif hw/board.kicad_pro                        # last two commits
  kdif -r v1.0 -r v2.0 hw/board.kicad_pro        # two tags
  kdif -r 41acedf,main hw/board.kicad_pro        # commit vs branch
  kdif --tags 5 hw/board.kicad_pro               # last 5 tags
  kdif --commits 10 --worktree hw/board.kicad_pro
  kdif -l F.Cu,B.Cu,Edge.Cuts -o diff.html hw/board.kicad_pro
  kdif hw/board.kicad_sch                        # schematic only
""",
    )
    p.add_argument("project", metavar="PROJECT",
                   help="path to the KiCad project (.kicad_pro diffs both the board "
                        "and the schematic; .kicad_pcb / .kicad_sch limit the diff "
                        "to that document); the git repository is located from it")

    sel = p.add_argument_group("revision selection")
    sel.add_argument("-r", "--ref", dest="refs", action="append", metavar="REF",
                     help="commit/tag/branch to include; repeatable, comma lists allowed")
    sel.add_argument("--commits", type=int, metavar="N", help="use the last N commits of HEAD")
    sel.add_argument("--tags", type=int, metavar="N", help="use the last N tags")
    sel.add_argument("--worktree", action="store_true",
                     help="also include the current (possibly uncommitted) working tree")

    out = p.add_argument_group("output")
    out.add_argument("-o", "--output", metavar="FILE", help="output HTML file "
                     "(default: <board>-diff.html in the current directory)")
    out.add_argument("-l", "--layers", metavar="LIST",
                     help="comma-separated PCB layer names to export "
                          "(default: all layers of the board)")
    out.add_argument("--no-compress", action="store_true",
                     help="embed raw SVG instead of deflate (bigger file, works on very old browsers)")
    out.add_argument("--show-fab-values", action="store_true",
                     help="plot the footprint value fields on the *.Fab layers "
                          "(hidden by default: a value next to every part buries "
                          "the actual change)")

    kc = p.add_argument_group("kicad-cli")
    kc.add_argument("--kicad-cli", metavar="CMD",
                    help="kicad-cli command (default: $KICAD_CLI, then PATH, then the "
                         "standard KiCad install location of this platform); for flatpak "
                         "KiCad pass 'flatpak run --command=kicad-cli org.kicad.KiCad'")
    kc.add_argument("--page-size-mode", type=int, choices=(0, 1, 2), default=0,
                    help="kicad-cli page sizing mode; 0=full page keeps revisions aligned (default: 0)")
    kc.add_argument("--check-zones", action="store_true",
                    help="let kicad-cli refill zones before plotting (KiCad >= 8)")
    kc.add_argument("-j", "--jobs", type=int, default=4, help="parallel kicad-cli processes (default: 4)")

    misc = p.add_argument_group("misc")
    misc.add_argument("--workdir", metavar="DIR",
                      help="working directory for temporary files (default: the user "
                           "cache directory of this platform, e.g. ~/.cache/kdif; must "
                           "be inside $HOME for flatpak KiCad)")
    misc.add_argument("--keep-workdir", action="store_true", help="do not delete temporary files")
    misc.add_argument("-q", "--quiet", action="store_true")
    misc.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def _collect_refs(args, repo: Path) -> List[str]:
    refs: List[str] = []
    if args.refs:
        for chunk in args.refs:
            refs.extend(t.strip() for t in chunk.split(",") if t.strip())
    if args.tags:
        refs.extend(gitrepo.last_tags(repo, args.tags))
    if args.commits:
        refs.extend(gitrepo.last_commits(repo, args.commits))
    if not refs:
        refs = gitrepo.last_commits(repo, 2)
    if args.worktree:
        refs.append(WORKTREE_REF)
    return refs


def _resolve_revs(repo: Path, refs: List[str]) -> List[Rev]:
    revs: List[Rev] = []
    seen: dict = {}

    def is_hash_name(r: Rev) -> bool:
        return r.name in (r.sha, r.short)

    for ref in refs:
        rev = gitrepo.resolve_rev(repo, ref)
        key = rev.sha or "@worktree"
        if key in seen:
            # same commit under two names: a symbolic name (tag/branch)
            # always wins over a bare commit hash
            prev = seen[key]
            if not rev.is_worktree and is_hash_name(prev) and not is_hash_name(rev):
                prev.name = rev.name
            continue
        if not rev.is_worktree and rev.name == rev.sha:
            rev.name = rev.short
        seen[key] = rev
        revs.append(rev)

    # chronological order (worktree always last)
    def key(rev: Rev):
        if rev.is_worktree:
            return (1, 0)
        return (0, gitrepo.commit_timestamp(repo, rev.sha))

    revs.sort(key=key)
    if len(revs) < 2:
        raise GitError("need at least two distinct revisions to diff "
                       "(after deduplication got "
                       f"{len(revs)}: {', '.join(r.name for r in revs)})")
    return revs


def _resolve_project(project_arg: str) -> tuple:
    """(repo_root, pcb_rel | None, sch_rel | None, came_from_kicad_pro)."""
    project = Path(project_arg).expanduser().resolve()
    if project.suffix not in (".kicad_pro", ".kicad_pcb", ".kicad_sch"):
        raise GitError(
            f"'{project_arg}' is not a .kicad_pro, .kicad_pcb or .kicad_sch file")
    if not project.parent.is_dir():
        raise GitError(f"directory '{project.parent}' does not exist")
    repo = gitrepo.repo_root(project.parent)

    def rel(p: Path) -> str:
        try:
            return p.relative_to(repo).as_posix()
        except ValueError:
            raise GitError(f"'{p}' is outside the git repository {repo}") from None

    if project.suffix == ".kicad_pcb":
        return repo, rel(project), None, False
    if project.suffix == ".kicad_sch":
        return repo, None, rel(project), False
    return (repo, rel(project.with_suffix(".kicad_pcb")),
            rel(project.with_suffix(".kicad_sch")), True)


def _select_docs(repo: Path, revs: List[Rev],
                 pcb_rel: Optional[str], sch_rel: Optional[str],
                 from_pro: bool) -> tuple:
    """Check the newest revision; for .kicad_pro keep whichever documents exist."""
    newest = revs[-1]
    files = set(gitrepo.ls_files(repo, newest))

    def hint(suffix: str) -> str:
        found = sorted(f for f in files if f.endswith(suffix))
        return ("\ncandidates in that revision:\n  " + "\n  ".join(found)) if found else ""

    if from_pro:
        pcb = pcb_rel if pcb_rel in files else None
        sch = sch_rel if sch_rel in files else None
        if not pcb and not sch:
            raise GitError(
                f"neither '{pcb_rel}' nor '{sch_rel}' found in revision "
                f"{newest.name}{hint('.kicad_pcb')}{hint('.kicad_sch')}")
        return pcb, sch
    if pcb_rel and pcb_rel not in files:
        raise GitError(f"'{pcb_rel}' not found in revision {newest.name}{hint('.kicad_pcb')}")
    if sch_rel and sch_rel not in files:
        raise GitError(f"'{sch_rel}' not found in revision {newest.name}{hint('.kicad_sch')}")
    return pcb_rel, sch_rel


def _sheet_union(trees: List[List[Sheet]]) -> List[Sheet]:
    """Union of sheets over all revisions; the newest revision dictates order,
    pages that only exist in older revisions are appended."""
    order: List[Sheet] = []
    seen = set()
    for tree in reversed(trees):
        for sh in tree:
            if sh.path_names not in seen:
                seen.add(sh.path_names)
                order.append(sh)
    return order


def main(argv: Optional[List[str]] = None) -> int:
    proc.use_utf8_io()   # paths and title blocks are printed; see kdif/proc.py
    args = _parse_args(argv)
    log = (lambda *a: None) if args.quiet else (lambda *a: print(*a, file=sys.stderr))

    try:
        repo, pcb_rel, sch_rel, from_pro = _resolve_project(args.project)
        log(f"repository: {repo}")

        cmd = find_kicad_cli(args.kicad_cli)
        major, ver = kicad_cli_version(cmd)
        log(f"kicad-cli: {' '.join(cmd)} (version {ver or 'unknown'})")

        refs = _collect_refs(args, repo)
        revs = _resolve_revs(repo, refs)
        log(f"revisions ({len(revs)}): " + ", ".join(r.name for r in revs))

        pcb_rel, sch_rel = _select_docs(repo, revs, pcb_rel, sch_rel, from_pro)
        if pcb_rel:
            log(f"board: {pcb_rel}")
        if sch_rel:
            log(f"schematic: {sch_rel}")

        # workdir: must live inside $HOME when kicad-cli runs in flatpak sandbox
        base = Path(args.workdir).resolve() if args.workdir else proc.default_workdir()
        base.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="kdif-", dir=base))
        if is_flatpak_cmd(cmd) and Path.home() not in workdir.parents:
            log(f"warning: workdir {workdir} is outside $HOME; "
                "flatpak KiCad may not be able to access it")

        try:
            return _run(args, log, repo, cmd, major, ver, revs, pcb_rel, sch_rel, workdir)
        finally:
            if args.keep_workdir:
                log(f"workdir kept: {workdir}")
            else:
                shutil.rmtree(workdir, ignore_errors=True)

    except (GitError, ExportError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _run(args, log, repo: Path, cmd, major: int, ver: str,
         revs: List[Rev], pcb_rel: Optional[str], sch_rel: Optional[str],
         workdir: Path) -> int:
    exporter = KicadExporter(
        cmd, major,
        page_size_mode=args.page_size_mode,
        check_zones=args.check_zones,
        compress=not args.no_compress,
    )

    # ------------------------------------------------------------- board prep
    layers = []
    pcb_jobs = []
    pcb_title_blocks = None
    if pcb_rel:
        boards: List[Path] = []
        for i, rev in enumerate(revs):
            dest = workdir / f"rev{i}"
            boards.append(gitrepo.extract_file(repo, rev, pcb_rel, dest))
        if not args.show_fab_values:
            # Edits the extracted copies in the work directory only
            hidden = sum(hide_fab_values(board) for board in boards)
            if hidden:
                log(f"hid {hidden} footprint value field(s) on fab layers "
                    "(--show-fab-values keeps them)")
        # per-revision title block (title/rev/date/company/comments)
        pcb_title_blocks = [parse_title_block(b) for b in boards]
        # layer list comes from the newest revision
        all_layers = parse_board_layers(boards[-1])
        layers = select_layers(all_layers, args.layers)
        log(f"layers ({len(layers)}): " + ", ".join(l.display for l in layers))
        for i, board in enumerate(boards):
            svg_dir = workdir / f"rev{i}" / "svg"
            svg_dir.mkdir(exist_ok=True)
            for j, layer in enumerate(layers):
                pcb_jobs.append((board, layer.canonical, svg_dir / f"l{j}.svg"))

    # --------------------------------------------------------- schematic prep
    # every .kicad_sch of the revision is extracted (hierarchical sheets may
    # live anywhere in the repo); the tree of pages comes from the root sheet
    sch_roots: List[Optional[Path]] = []
    sch_trees: List[List[Sheet]] = []
    sch_jobs = []
    sheets: List[Sheet] = []
    sch_title_blocks = None
    if sch_rel:
        for i, rev in enumerate(revs):
            tree_dir = workdir / f"rev{i}" / "sch"
            rels = [f for f in gitrepo.ls_files(repo, rev) if f.endswith(".kicad_sch")]
            gitrepo.extract_tree(repo, rev, rels, tree_dir)
            root = tree_dir / sch_rel
            sch_roots.append(root if root.is_file() else None)
            sch_trees.append(parse_sheet_tree(root) if root.is_file() else [])
            sch_jobs.append((sch_roots[-1], workdir / f"rev{i}" / "schsvg"))
        # title block of the root sheet, per revision
        sch_title_blocks = [parse_title_block(r) if r else {} for r in sch_roots]
        sheets = _sheet_union(sch_trees)
        log(f"sheets ({len(sheets)}): " + ", ".join(s.display for s in sheets))

    # ------------------------------------------------------------ run exports
    total = len(pcb_jobs) + sum(1 for r, _ in sch_jobs if r is not None)
    done = [0]
    t0 = time.time()

    def progress(_i, _what):
        done[0] += 1
        if not args.quiet:
            print(f"\r  exporting SVG {done[0]}/{total} ...", end="", file=sys.stderr)

    log(f"running kicad-cli for {total} exports ({args.jobs} parallel jobs)")
    pcb_per_rev = None
    if pcb_jobs:
        results = exporter.export_all(pcb_jobs, workers=args.jobs, progress=progress)
        # results are ordered rev-major: rev0 layers..., rev1 layers...
        pcb_per_rev = [results[i * len(layers):(i + 1) * len(layers)]
                       for i in range(len(revs))]
    sch_per_rev = None
    if sch_rel:
        exporter.export_schematics(sch_jobs, workers=args.jobs, progress=progress)
        sch_per_rev = []
        for i in range(len(revs)):
            by_key = {}
            if sch_roots[i] is not None:
                matched = match_sheet_svgs(sch_trees[i], workdir / f"rev{i}" / "schsvg")
                by_key = {sh.path_names: p for sh, p in zip(sch_trees[i], matched)}
            row = []
            for sh in sheets:
                p = by_key.get(sh.path_names)
                row.append(exporter.process_svg(p) if p else None)
            sch_per_rev.append(row)
    if not args.quiet:
        print(f"\r  exported {total} SVGs in {time.time() - t0:.1f}s      ", file=sys.stderr)

    # ------------------------------------------------------------------ html
    stem = Path(pcb_rel or sch_rel).stem
    out_path = Path(args.output) if args.output else Path.cwd() / (stem + "-diff.html")
    html = build_html(
        title=stem,
        board_rel=pcb_rel or sch_rel,
        repo_name=repo.name,
        revs=revs,
        layers=layers if pcb_rel else None,
        per_rev_svgs=pcb_per_rev,
        pcb_title_blocks=pcb_title_blocks,
        sheets=sheets if sch_rel else None,
        per_rev_sheet_svgs=sch_per_rev,
        sch_title_blocks=sch_title_blocks,
        compressed=not args.no_compress,
        kicad_version=ver,
    )
    proc.write_text(out_path, html)
    log(f"wrote {out_path} ({out_path.stat().st_size / 1024:.0f} KiB)")
    if not args.quiet:
        print(str(out_path))
    return 0
