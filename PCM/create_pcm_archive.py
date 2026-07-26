"""Builds the KiCad PCM (Plugin and Content Manager) installable archive for
kdif - see PCM/README.md for what a "PCM archive" is and how it is published.

    python3 PCM/create_pcm_archive.py v1.2.3

kico's equivalent is a POSIX shell script; this one is Python (stdlib only,
same interpreter kdif itself needs) so the archive can be built on Windows and
macOS too, not just where sh/zip/sha256sum/unzip happen to exist. Idempotent -
PCM/archive/ and PCM/*.zip are wiped on every run.

The archive carries the whole `kdif` package, not just the plugin: PCM
extracts this one zip and nothing else, so `plugin/panel.py` (which runs
`python -m kdif` as a subprocess) would have nothing to import on an
end-user's machine otherwise. That is cheap here because kdif is pure stdlib -
the bundle is a few Python files plus the HTML template.

Prints, and exports to $GITHUB_OUTPUT/$GITHUB_ENV when running under GitHub
Actions, the four values a packages.json entry needs (see PCM/pcm_repository.py):
download_sha256, download_size, download_url, install_size.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = REPO_ROOT / "PCM" / "archive"
TEMPLATE_PATH = REPO_ROOT / "PCM" / "metadata.template.json"
ICON_PATH = REPO_ROOT / "PCM" / "icon.png"

# Where the release assets end up. GITHUB_REPOSITORY is set by Actions, so a
# fork publishes its own URLs without editing anything here.
DEFAULT_REPOSITORY = "0x12net/kdif"

# Fixed timestamp for every zip entry: two builds of the same commit then
# produce byte-identical archives (so does a rebuild after a failed upload),
# which keeps download_sha256 verifiable. 1980-01-01 is the zip format's own
# epoch - the earliest value it can store.
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def repository() -> str:
    return os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPOSITORY


def _stage(version: str) -> Path:
    """Lay out the archive contents under PCM/archive/ and return that path."""
    if ARCHIVE_DIR.exists():
        shutil.rmtree(ARCHIVE_DIR)
    for stale in (REPO_ROOT / "PCM").glob("*.zip"):
        stale.unlink()

    plugin_dir = ARCHIVE_DIR / "plugins" / "kdif"
    plugin_dir.mkdir(parents=True)

    print("Copy the plugin entrypoint, manifest and icons")
    for name in ("plugin.json", "panel.py", "requirements.txt"):
        shutil.copy2(REPO_ROOT / "plugin" / name, plugin_dir / name)
    shutil.copytree(REPO_ROOT / "plugin" / "icons", plugin_dir / "icons")

    print("Bundle the kdif package itself (panel.py runs `python -m kdif`)")
    shutil.copytree(
        REPO_ROOT / "kdif", plugin_dir / "kdif",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    print("Write metadata.json from the template")
    _write_metadata(version, ARCHIVE_DIR / "metadata.json")

    if ICON_PATH.is_file():
        resources = ARCHIVE_DIR / "resources"
        resources.mkdir()
        shutil.copy2(ICON_PATH, resources / "icon.png")
    else:
        print("No PCM/icon.png (optional, 64x64) - shipping without one")
    return ARCHIVE_DIR


def _write_metadata(version: str, dest: Path) -> None:
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        metadata = json.load(f)
    entry = metadata["versions"][0]
    entry["version"] = strip_v(version)
    # download_*/install_size only mean something in a packages.json entry
    # (PCM/pcm_repository.py fills them in there), never inside the archive.
    for field in ("download_sha256", "download_size", "download_url", "install_size"):
        entry.pop(field, None)
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=4)
        f.write("\n")


def strip_v(version: str) -> str:
    """`v1.2.3` -> `1.2.3`.

    Git tags, release names and the zip filename keep the leading "v"; the PCM
    schema's versions[].version is pattern-constrained to digits and dots and
    rejects it.
    """
    return version[1:] if version.startswith("v") else version


def _zip(stage: Path, zip_path: Path) -> List[zipfile.ZipInfo]:
    files = sorted(p for p in stage.rglob("*") if p.is_file())
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(stage).as_posix(), ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            # 0644 as a unix mode: a Windows build would otherwise produce an
            # archive whose files unpack without read permission bits on Linux.
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
        return zf.infolist()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="release version, e.g. v1.2.3 (or 1.2.3)")
    args = parser.parse_args()

    stage = _stage(args.version)
    zip_path = REPO_ROOT / "PCM" / f"kdif_{args.version}.zip"
    print(f"Zip PCM archive -> {zip_path.name}")
    infos = _zip(stage, zip_path)

    data = zip_path.read_bytes()
    values = {
        "version": args.version,
        "download_sha256": hashlib.sha256(data).hexdigest(),
        "download_size": str(len(data)),
        "download_url": (f"https://github.com/{repository()}/releases/download/"
                         f"{args.version}/{zip_path.name}"),
        "install_size": str(sum(info.file_size for info in infos)),
    }

    for github_file in ("GITHUB_OUTPUT", "GITHUB_ENV"):
        target = os.environ.get(github_file)
        if target:
            with open(target, "a", encoding="utf-8") as f:
                for key, value in values.items():
                    f.write(f"{key}={value}\n")

    for key, value in values.items():
        print(f"{key}={value}")
    print(f"Archive: {zip_path}")


if __name__ == "__main__":
    main()
