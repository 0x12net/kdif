"""Updates the self-hosted PCM repository index (`pcm-repository/packages.json`
+ `repository.json` + `resources.zip`) with one release's info - see
PCM/README.md, "Self-hosted PCM repository", for what these files are and why
they live on a rolling GitHub release instead of in git.

    python3 PCM/pcm_repository.py --version v1.2.3 \
        --download-sha256 <...> --download-size <...> \
        --download-url <...> --install-size <...>

The four download_*/install_size values come from PCM/create_pcm_archive.py,
which computed them for the archive it just built; in CI they cross a job
boundary as GitHub Actions job outputs (see
.github/workflows/release-kicad-pcm.yml).

packages.json accumulates *every* released version in each package's
`versions` array - PCM offers users the upgrade path from whatever they have
installed, so history matters. Since a GitHub release asset is overwritten
rather than merged, the workflow downloads the existing packages.json into
this same path before running this script, and `_load_packages()` merges into
it. repository.json is regenerated wholesale each time; only the sha256 and
timestamps in it actually change between releases.

resources.zip is a different thing from the `resources/icon.png` that
create_pcm_archive.py puts *inside* the installable archive: that copy is only
seen after a package is downloaded, while the PCM browse list reads icons from
the repository-level resources.zip referenced by repository.json. Rebuilt from
PCM/icon.png every run - unlike packages.json it has no history to preserve.

Pure local file I/O, no network, no git: usable by hand to preview exactly
what CI would publish.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "PCM" / "metadata.template.json"
ICON_PATH = REPO_ROOT / "PCM" / "icon.png"
REPOSITORY_DIR = REPO_ROOT / "pcm-repository"
PACKAGES_PATH = REPOSITORY_DIR / "packages.json"
REPOSITORY_PATH = REPOSITORY_DIR / "repository.json"
RESOURCES_ZIP_PATH = REPOSITORY_DIR / "resources.zip"

# Same defaulting as PCM/create_pcm_archive.py: hardcoded repository, unless
# GitHub Actions says otherwise (so a fork publishes its own URLs untouched).
DEFAULT_REPOSITORY = "0x12net/kdif"
MAINTAINER = {"name": "0x12net", "contact": {"github": "https://github.com/0x12net"}}


def _asset_url(name: str) -> str:
    """URL of an asset on the rolling "pcm-repository" release.

    This is where the files this script writes are actually served from - a
    fixed release tag used as a stable, non-versioned carrier, re-uploaded
    with `gh release upload --clobber` on every tag push.
    """
    repository = os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPOSITORY
    return f"https://github.com/{repository}/releases/download/pcm-repository/{name}"


def _build_version_entry(args: argparse.Namespace):
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = json.load(f)
    # Same "v1.2.3" -> "1.2.3" strip create_pcm_archive.py applies to the
    # archive-internal metadata.json; here the download_*/install_size fields
    # are kept rather than dropped - a packages.json entry is exactly where
    # they belong.
    version = args.version[1:] if args.version.startswith("v") else args.version
    entry = dict(template["versions"][0])
    entry.update({
        "version": version,
        "download_sha256": args.download_sha256,
        "download_size": args.download_size,
        "download_url": args.download_url,
        "install_size": args.install_size,
    })
    return template, entry


def _load_packages() -> dict:
    if PACKAGES_PATH.exists():
        with open(PACKAGES_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"packages": []}


def _upsert_package_version(packages: dict, template: dict, entry: dict) -> None:
    identifier = template["identifier"]
    package = next((p for p in packages["packages"] if p["identifier"] == identifier), None)
    if package is None:
        # First release published through this repository: everything but
        # `versions` comes straight from the template.
        package = {k: v for k, v in template.items() if k != "versions"}
        package["versions"] = []
        packages["packages"].append(package)
    else:
        # Keep the top-level fields in sync with the template on every release,
        # so an edited description/icon/license reaches already-published
        # repositories on the next tag instead of only fresh installs.
        for key, value in template.items():
            if key != "versions":
                package[key] = value

    existing = next((v for v in package["versions"] if v["version"] == entry["version"]), None)
    if existing is not None:
        # Re-publishing the same version (a deleted and recreated tag)
        # overwrites in place instead of appending a duplicate.
        existing.clear()
        existing.update(entry)
    else:
        package["versions"].append(entry)


def _write_packages(packages: dict) -> None:
    REPOSITORY_DIR.mkdir(parents=True, exist_ok=True)
    with open(PACKAGES_PATH, "w", encoding="utf-8") as f:
        json.dump(packages, f, indent=4)
        f.write("\n")


def _write_resources(template: dict) -> bool:
    if not ICON_PATH.is_file():
        return False  # same "optional, ship without one" rule as the archive
    # Layout taken from real third-party repositories: one "<identifier>/icon.png"
    # entry per package, not "<identifier>.png" at the zip root.
    with zipfile.ZipFile(RESOURCES_ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(ICON_PATH, f"{template['identifier']}/icon.png")
    return True


def _write_repository(include_resources: bool) -> None:
    now = datetime.now(timezone.utc)

    def stamped(path: Path, name: str) -> dict:
        return {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "update_timestamp": int(now.timestamp()),
            "update_time_utc": now.strftime("%Y-%m-%d %H:%M:%S"),
            "url": _asset_url(name),
        }

    repository = {
        "$schema": "https://gitlab.com/kicad/code/kicad/-/raw/master/kicad/pcm/"
                   "schemas/pcm.v1.schema.json#/definitions/Repository",
        "maintainer": MAINTAINER,
        "name": "kdif PCM repository",
        "packages": stamped(PACKAGES_PATH, "packages.json"),
    }
    if include_resources:
        repository["resources"] = stamped(RESOURCES_ZIP_PATH, "resources.zip")
    with open(REPOSITORY_PATH, "w", encoding="utf-8") as f:
        json.dump(repository, f, indent=4)
        f.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--download-sha256", required=True)
    parser.add_argument("--download-size", required=True, type=int)
    parser.add_argument("--download-url", required=True)
    parser.add_argument("--install-size", required=True, type=int)
    args = parser.parse_args()

    template, entry = _build_version_entry(args)
    packages = _load_packages()
    _upsert_package_version(packages, template, entry)
    _write_packages(packages)
    _write_repository(_write_resources(template))

    print(f"Updated {PACKAGES_PATH.relative_to(REPO_ROOT)} and "
          f"{REPOSITORY_PATH.relative_to(REPO_ROOT)} for version {args.version}")


if __name__ == "__main__":
    main()
