"""kdif: interactive HTML diff viewer for KiCad PCBs and schematics from git history."""

# The single place the version is written down: pyproject.toml reads it as a
# dynamic attribute and the Makefile takes the .deb version from it. 0.0.0
# means "built from a checkout, not from a release" - the release workflows
# replace this line with the pushed git tag (packaging/set_version.py).
__version__ = "0.0.0"
