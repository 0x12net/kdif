"""Git helpers: ref resolution, metadata, file extraction (via git archive)."""

from __future__ import annotations

import subprocess
import tarfile
import io
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

WORKTREE_REF = "@worktree"


class GitError(RuntimeError):
    pass


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
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed:\n{proc.stderr.decode(errors='replace').strip()}"
        )
    return proc.stdout if binary else proc.stdout.decode(errors="replace")


def repo_root(path: Path) -> Path:
    """Root of the git repository containing `path` (a directory)."""
    try:
        out = _git(path, "rev-parse", "--show-toplevel")
    except (GitError, FileNotFoundError):
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
