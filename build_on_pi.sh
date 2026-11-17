#!/usr/bin/env bash
# EXPERIMENTAL: build WoWAddonUpdater.exe on a Raspberry Pi 5 (or any Linux box) through Wine.
#
# PyInstaller cannot cross-compile, so the Windows build of Python has to *run* during the
# build. On a Pi that means x86-64 Wine on top of box64 (the Pi-Apps "Wine (x64)" entry sets
# both up). This script then:
#   1. assembles a Windows Python 3.12 + Tkinter + PyInstaller in build/winpython (no installer)
#   2. runs PyInstaller inside Wine to produce dist/WoWAddonUpdater.exe
# Step 1 is tested. Step 2 depends on your Wine/box64 setup and has not been tested on a Pi.
set -euo pipefail
cd "$(dirname "$0")"
command -v wine >/dev/null || { echo "x86-64 Wine not found (on a Pi: install Wine (x64) + box64, e.g. via Pi-Apps)"; exit 1; }
command -v zstd >/dev/null || { echo "zstd not found: sudo apt install zstd"; exit 1; }

[ -x build/winpython/python.exe ] || python3 tools/fetch_winpython.py build/winpython

export WINEPREFIX="$PWD/build/wineprefix" WINEARCH=win64 WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml="
wine build/winpython/python.exe -m PyInstaller --noconfirm --clean --onefile --windowed \
     --name WoWAddonUpdater --distpath dist --workpath build/pyinstaller --specpath build addon_updater.py
ls -l dist/WoWAddonUpdater.exe
