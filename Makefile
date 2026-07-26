# Packaging for kdif. It is a pure-Python, architecture-independent tool, so
# the .deb just drops the module into dist-packages and a wrapper into
# /usr/bin -- no compiler or dh-python needed.
#
#   make deb                  # version taken from kdif/__init__.py
#   make deb VERSION=1.2.3    # override (CI passes the git tag here)
#   make pcm                  # KiCad Plugin and Content Manager archive
#
# `make pcm` is a convenience wrapper only: PCM/create_pcm_archive.py is
# plain Python precisely so the KiCad package can also be built where make
# is not a given (Windows, macOS) -- see PCM/README.md.

PACKAGE := kdif
VERSION ?= $(shell python3 -c "import kdif; print(kdif.__version__)")
ARCH    := all
DEB     := $(PACKAGE)_$(VERSION)_$(ARCH).deb
STAGE   := dist/$(PACKAGE)_$(VERSION)
PYDEST  := $(STAGE)/usr/lib/python3/dist-packages/$(PACKAGE)

.PHONY: deb pcm clean

deb:
	rm -rf "$(STAGE)"
	# Python package + bundled HTML template. Only kdif/ -- the KiCad plugin
	# in plugin/ (wxPython GUI) is packaged for KiCad's PCM instead, and must
	# not leak into the .deb, which is a command-line tool with no GUI
	# dependencies at all (see the guard in .github/workflows/tests.yml).
	install -d "$(PYDEST)/template"
	install -m 644 $(PACKAGE)/*.py "$(PYDEST)/"
	install -m 644 $(PACKAGE)/template/*.html "$(PYDEST)/template/"
	# Executable wrapper
	install -d "$(STAGE)/usr/bin"
	install -m 755 packaging/kdif "$(STAGE)/usr/bin/kdif"
	# Documentation
	install -d "$(STAGE)/usr/share/doc/$(PACKAGE)"
	install -m 644 README.md LICENSE "$(STAGE)/usr/share/doc/$(PACKAGE)/"
	# Control metadata (version substituted from $(VERSION))
	install -d "$(STAGE)/DEBIAN"
	sed 's/@VERSION@/$(VERSION)/' packaging/control.in > "$(STAGE)/DEBIAN/control"
	dpkg-deb --root-owner-group --build "$(STAGE)" "$(DEB)"
	@echo "built $(DEB)"

pcm:
	python3 PCM/create_pcm_archive.py "v$(VERSION)"
	python3 PCM/check_archive.py "PCM/$(PACKAGE)_v$(VERSION).zip"

clean:
	rm -rf dist *.deb PCM/archive PCM/*.zip pcm-repository
