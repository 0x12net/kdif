# Build a .deb for kdif. It is a pure-Python, architecture-independent tool,
# so the package just drops the module into dist-packages and a wrapper into
# /usr/bin -- no compiler or dh-python needed.
#
#   make deb                 # version taken from kdif/__init__.py
#   make deb VERSION=1.2.3    # override (CI passes the git tag here)

PACKAGE := kdif
VERSION ?= $(shell python3 -c "import kdif; print(kdif.__version__)")
ARCH    := all
DEB     := $(PACKAGE)_$(VERSION)_$(ARCH).deb
STAGE   := dist/$(PACKAGE)_$(VERSION)
PYDEST  := $(STAGE)/usr/lib/python3/dist-packages/$(PACKAGE)

.PHONY: deb clean

deb:
	rm -rf "$(STAGE)"
	# Python package + bundled HTML template
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

clean:
	rm -rf dist *.deb
