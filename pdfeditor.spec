# PyInstaller: сборка PDF Editor.app. Запускается из build_mac.sh.
# -*- mode: python ; coding: utf-8 -*-

import os

VERSION = "0.1.0"
MIN_MACOS = os.environ.get("PDFEDITOR_MIN_MACOS", "12.0")  # задаёт build_mac.sh

a = Analysis(
    ["main.py"],
    excludes=["tkinter", "unittest", "pydoc", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PDF Editor",
    console=False,
    argv_emulation=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="PDF Editor")

app = BUNDLE(
    coll,
    name="PDF Editor.app",
    icon="assets/icon.icns",
    bundle_identifier="com.school21.pdfeditor",
    version=VERSION,
    info_plist={
        "CFBundleDisplayName": "PDF Editor",
        "CFBundleShortVersionString": VERSION,
        "LSMinimumSystemVersion": MIN_MACOS,
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        # «Открыть с помощью → PDF Editor» в Finder
        "CFBundleDocumentTypes": [{
            "CFBundleTypeName": "PDF Document",
            "CFBundleTypeRole": "Editor",
            "LSHandlerRank": "Alternate",
            "LSItemContentTypes": ["com.adobe.pdf"],
        }],
    },
)
