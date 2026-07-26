"""Git helpers: ref resolution, metadata, file extraction (via git archive)."""

from __future__ import annotations

import tarfile
import io
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from . import proc

WORKTREE_REF = "@worktree"


class GitError(RuntimeError):
    pass


class GitMissing(GitError):
    """git itself is not installed, as opposed to a git command failing.

    A class of its own because every other GitError says something about *this*
    repository: without it the callers that turn a failure into a message for
    the user (cli.py, the KiCad panel) report a missing git as "not inside a
    git repository", which sends people looking in entirely the wrong place.
    """


GIT_MISSING_MSG = (
    "git not found - install it and make sure it is on PATH.\n"
    "  Windows: https://git-scm.com/download/win\n"
    "  macOS:   xcode-select --install\n"
    "  Linux:   your package manager, e.g. 'apt install git' or 'pacman -S git'"
)


@dataclass
class Rev:
    """One point of comparison."""

    name: str        # display name: tag, given ref, short hash or "worktree"
    sha: str         # full commit sha ("" for worktree)
    short: str       # abbreviated sha ("" for worktree)
    date: str        # ISO committer date
    author: str
    subject: str     # commit subject line
    is_worktree: bool = False


def _git(repo: Path, *args: str, binary: bool = False):
    try:
        result = proc.run(["git", "-C", str(repo), *args], binary=binary)
    except FileNotFoundError:
        # Nothing is passed as cwd, so the only file subprocess can fail to
        # find here is the git executable itself.
        raise GitMissing(GIT_MISSING_MSG) from None
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout


def repo_root(path: Path) -> Path:
    """Root of the git repository containing `path` (a directory)."""
    try:
        out = _git(path, "rev-parse", "--show-toplevel")
    except GitMissing:
        raise  # no git at all: keep that message, it is not about `path`
    except GitError:
        raise GitError(f"'{path}' is not inside a git repository") from None
    return Path(out.strip()).resolve()


def resolve_rev(repo: Path, ref: str) -> Rev:
    if ref == WORKTREE_REF:
        return Rev(
            name="worktree", sha="", short="", date="",
            author="", subject="(uncommitted working tree)", is_worktree=True,
        )
    sha = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    meta = _git(repo, "show", "-s", "--format=%h%x00%cI%x00%an%x00%s", sha).strip("\n")
    short, date, author, subject = meta.split("\x00", 3)
    return Rev(name=ref, sha=sha, short=short, date=date, author=author, subject=subject)


def last_tags(repo: Path, n: int) -> List[str]:
    """Tags sorted oldest -> newest; last n of them."""
    out = _git(repo, "tag", "--sort=creatordate", "--merged", "HEAD")
    tags = [t for t in out.splitlines() if t.strip()]
    if not tags:
        # fall back to unmerged tags too
        out = _git(repo, "tag", "--sort=creatordate")
        tags = [t for t in out.splitlines() if t.strip()]
    return tags[-n:]


def last_commits(repo: Path, n: int) -> List[str]:
    """Last n commits of HEAD, oldest first."""
    out = _git(repo, "log", "-n", str(n), "--format=%H")
    shas = [s for s in out.splitlines() if s.strip()]
    return list(reversed(shas))


def commit_timestamp(repo: Path, sha: str) -> int:
    return int(_git(repo, "show", "-s", "--format=%ct", sha).strip())


def commit_log(repo: Path, n: int, paths: Optional[List[str]] = None) -> List[Rev]:
    """Last n commits, newest first, optionally only those touching `paths`.

    One git call for the whole list, unlike resolve_rev() per ref - this feeds
    the revision picker in the KiCad plugin, which needs the subject line of
    every candidate commit up front.
    """
    args = ["log", "-n", str(n), "--format=%H%x00%h%x00%cI%x00%an%x00%s"]
    if paths:
        args += ["--", *paths]
    revs: List[Rev] = []
    for line in _git(repo, *args).splitlines():
        if not line.strip():
            continue
        sha, short, date, author, subject = line.split("\x00", 4)
        revs.append(Rev(name=short, sha=sha, short=short, date=date,
                        author=author, subject=subject))
    return revs


def tags_by_commit(repo: Path) -> Dict[str, List[str]]:
    """Commit sha -> tag names pointing at it.

    An annotated tag's own object is the tag, not the commit, so %(*objectname)
    (the dereferenced target, empty for lightweight tags) is what has to be
    keyed on when it is set.
    """
    out = _git(repo, "for-each-ref", "--sort=creatordate",
               "--format=%(objectname)%00%(*objectname)%00%(refname:short)", "refs/tags")
    mapping: Dict[str, List[str]] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        obj, deref, name = line.split("\x00", 2)
        mapping.setdefault(deref or obj, []).append(name)
    return mapping


def ls_files(repo: Path, rev: Rev) -> List[str]:
    if rev.is_worktree:
        out = _git(repo, "ls-files", "--cached", "--others", "--exclude-standard")
    else:
        out = _git(repo, "ls-tree", "-r", "--name-only", rev.sha)
    return [line for line in out.splitlines() if line.strip()]


def extract_tree(repo: Path, rev: Rev, rel_paths: List[str], dest_dir: Path) -> None:
    """Extract several files from a revision into dest_dir, preserving relative paths."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not rel_paths:
        return
    if rev.is_worktree:
        for rel in rel_paths:
            src = repo / rel
            if not src.is_file():
                continue
            dest = dest_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        return
    data = _git(repo, "archive", rev.sha, "--", *rel_paths, binary=True)
    wanted = set(rel_paths)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for m in tar.getmembers():
            if not m.isfile() or m.name not in wanted:
                continue
            dest = dest_dir / m.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            fobj = tar.extractfile(m)
            assert fobj is not None
            dest.write_bytes(fobj.read())


def extract_file(repo: Path, rev: Rev, rel_path: str, dest_dir: Path) -> Path:
    """Extract one file from a revision into dest_dir, keeping its basename."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(rel_path).name
    if rev.is_worktree:
        src = repo / rel_path
        if not src.is_file():
            raise GitError(f"'{rel_path}' not found in working tree")
        shutil.copy2(src, dest)
        return dest
    data = _git(repo, "archive", rev.sha, "--", rel_path, binary=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        member = None
        for m in tar.getmembers():
            if m.isfile() and m.name == rel_path:
                member = m
                break
        if member is None:
            raise GitError(f"'{rel_path}' not found in revision {rev.name}")
        fobj = tar.extractfile(member)
        assert fobj is not None
        dest.write_bytes(fobj.read())
    return dest
