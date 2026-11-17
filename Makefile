# SPDX-FileCopyrightText: 2026 erastmorgan
# SPDX-License-Identifier: MIT
#
# vpn-tray is pure Python + QML: nothing to compile. "Building" means packaging.

VERSION := $(shell python3 -c 'import vpntray; print(vpntray.__version__)')
NAME    := vpn-tray-$(VERSION)

.PHONY: help test e2e lint install uninstall plasmoid dist clean

help:
	@echo "make test       unit tests (no root, no network)"
	@echo "make e2e        LIVE end-to-end test — really switches your VPNs (see README)"
	@echo "make lint       syntax checks for Python, shell and the applet metadata"
	@echo "make install    install for the current user (asks for sudo once)"
	@echo "make uninstall  remove everything (kill switch is turned off first)"
	@echo "make plasmoid   build dist/$(NAME).plasmoid (KDE Plasma 6 applet package)"
	@echo "make dist       build dist/$(NAME).tar.gz (source release)"

test:
	python3 -m unittest discover -s tests -p 'test_*.py' -v

e2e:
	python3 tests/e2e_live.py

lint:
	python3 -m py_compile vpntray/*.py vpntray/backends/*.py tests/*.py
	bash -n helper/vpn-tray-helper install.sh uninstall.sh
	python3 -c 'import json; json.load(open("plasmoid/metadata.json"))'

install:
	./install.sh

uninstall:
	./uninstall.sh

plasmoid:
	mkdir -p dist
	rm -f dist/$(NAME).plasmoid
	cd plasmoid && python3 -m zipfile -c ../dist/$(NAME).plasmoid metadata.json contents
	@echo "dist/$(NAME).plasmoid  (kpackagetool6 -t Plasma/Applet -i dist/$(NAME).plasmoid)"

dist:
	mkdir -p dist
	git archive --format=tar.gz --prefix=$(NAME)/ -o dist/$(NAME).tar.gz HEAD
	@echo "dist/$(NAME).tar.gz"

clean:
	rm -rf dist
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
