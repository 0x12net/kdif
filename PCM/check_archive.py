"""Validates a built PCM archive before it is attached to a release.

    python3 PCM/check_archive.py PCM/kdif_v1.2.3.zip

Everything checked here has one thing in common: KiCad only reports it at
install time, in a dialog, on the user's machine - a malformed metadata.json
fails with "instance not found in required enum", a missing bundled module
fails as an ImportError the moment the toolbar button is pressed. Running this
in CI moves all of that to before the release exists.

Deliberately stdlib-only (no jsonschema, no network): CI on three platforms
should not depend on a schema fetch from gitlab.com to publish a release. The
enums/patterns below are transcribed from
kicad/pcm/schemas/pcm.v1.schema.json and go.kicad.org/api/schemas/v1.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from typing import List

PACKAGE_REQUIRED = ["name", "description", "description_full", "identifier",
                    "type", "author", "license", "versions"]
VERSION_REQUIRED = ["version", "status", "kicad_version"]
VERSION_PATTERN = re.compile(r"^\d{1,4}(\.\d{1,4}(\.\d{1,6})?)?$")
KICAD_VERSION_PATTERN = re.compile(r"^\d{1,2}(\.\d{1,2}(\.\d{1,2})?)?$")
IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9]*(\.[a-z0-9]+)*$")
STATUSES = {"stable", "testing", "development", "deprecated"}
RUNTIMES = {"swig", "ipc"}
PLATFORMS = {"windows", "macos", "linux"}
TYPES = {"plugin", "library", "colortheme"}
# Not the SPDX list in full - just the values this project may reasonably use.
# The schema's enum is closed, which is the trap kico hit with "GPL-3.0-or-later".
LICENSES = {"MIT", "GPL-3.0", "GPL-2.0", "LGPL-3.0", "Apache-2.0", "BSD-3-Clause",
            "BSD-2-Clause", "CC-BY-4.0", "CC-BY-SA-4.0", "CC0-1.0", "MPL-2.0"}

# The plugin cannot run without these: panel.py imports the bundled kdif
# package, kdif renders through the template, plugin.json points at the icons.
REQUIRED_ENTRIES = [
    "metadata.json",
    "plugins/kdif/plugin.json",
    "plugins/kdif/panel.py",
    "plugins/kdif/requirements.txt",
    "plugins/kdif/kdif/__init__.py",
    "plugins/kdif/kdif/__main__.py",
    "plugins/kdif/kdif/cli.py",
    "plugins/kdif/kdif/template/viewer.html",
]


def check_metadata(metadata: dict, problems: List[str]) -> None:
    for field in PACKAGE_REQUIRED:
        if field not in metadata:
            problems.append(f"metadata.json: missing required field {field!r}")
    if metadata.get("type") not in TYPES:
        problems.append(f"metadata.json: type {metadata.get('type')!r} not in {sorted(TYPES)}")
    if metadata.get("license") not in LICENSES:
        problems.append(f"metadata.json: license {metadata.get('license')!r} is not a "
                        "value from the PCM schema's closed License enum")
    identifier = metadata.get("identifier", "")
    if not IDENTIFIER_PATTERN.match(identifier):
        problems.append(f"metadata.json: identifier {identifier!r} is not reverse-DNS")

    versions = metadata.get("versions") or []
    if len(versions) != 1:
        problems.append(f"metadata.json: expected exactly one version entry, got {len(versions)}")
    for entry in versions:
        for field in VERSION_REQUIRED:
            if field not in entry:
                problems.append(f"metadata.json: version entry missing {field!r}")
        version = entry.get("version", "")
        if not VERSION_PATTERN.match(version):
            problems.append(f"metadata.json: version {version!r} must be digits and dots "
                            "only (a leading 'v' is stripped by create_pcm_archive.py)")
        if not KICAD_VERSION_PATTERN.match(entry.get("kicad_version", "")):
            problems.append(f"metadata.json: bad kicad_version {entry.get('kicad_version')!r}")
        if entry.get("status") not in STATUSES:
            problems.append(f"metadata.json: status {entry.get('status')!r} not in {sorted(STATUSES)}")
        if "runtime" in entry and entry["runtime"] not in RUNTIMES:
            problems.append(f"metadata.json: runtime {entry['runtime']!r} not in {sorted(RUNTIMES)}")
        for platform in entry.get("platforms", []):
            if platform not in PLATFORMS:
                problems.append(f"metadata.json: platform {platform!r} not in {sorted(PLATFORMS)}")
        # These describe the download and only belong in a packages.json entry;
        # inside the archive they are stale by construction.
        for field in ("download_sha256", "download_size", "download_url", "install_size"):
            if field in entry:
                problems.append(f"metadata.json: {field!r} must not be present inside the archive")


def check_plugin(plugin: dict, names: List[str], problems: List[str]) -> None:
    for field in ("identifier", "name", "description", "runtime", "actions"):
        if field not in plugin:
            problems.append(f"plugin.json: missing required field {field!r}")
    if plugin.get("runtime", {}).get("type") != "python":
        problems.append("plugin.json: runtime.type must be 'python' for this plugin")
    actions = plugin.get("actions") or []
    if not actions:
        problems.append("plugin.json: no actions - the toolbar button would never appear")
    for action in actions:
        for field in ("identifier", "name", "description", "entrypoint"):
            if field not in action:
                problems.append(f"plugin.json: action missing {field!r}")
        referenced = [action.get("entrypoint", "")]
        referenced += action.get("icons-light", []) + action.get("icons-dark", [])
        for rel in referenced:
            if rel and f"plugins/kdif/{rel}" not in names:
                problems.append(f"plugin.json references {rel!r}, which is not in the archive")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", help="path to the built kdif_<version>.zip")
    args = parser.parse_args()

    problems: List[str] = []
    with zipfile.ZipFile(args.archive) as zf:
        names = zf.namelist()
        for entry in REQUIRED_ENTRIES:
            if entry not in names:
                problems.append(f"archive is missing {entry}")
        if "metadata.json" in names:
            check_metadata(json.loads(zf.read("metadata.json")), problems)
        if "plugins/kdif/plugin.json" in names:
            check_plugin(json.loads(zf.read("plugins/kdif/plugin.json")), names, problems)
        if any(name.endswith(".pyc") or "__pycache__" in name for name in names):
            problems.append("archive contains __pycache__/*.pyc - build artefacts should not ship")

    if problems:
        print(f"{args.archive}: {len(problems)} problem(s)")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"{args.archive}: ok ({len(names)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
