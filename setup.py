"""
py2app build recipe for Scribe.

Build alias-mode bundle (dev-friendly: scribe.py edits take effect without rebuild):

    ./venv/bin/python setup.py py2app -A

Build full standalone bundle (ships Python + deps inside the .app):

    ./venv/bin/python setup.py py2app

After building, re-sign with our stable identifier so TCC grants persist:

    codesign --force --sign - --identifier com.avi.scribe dist/Scribe.app

Why py2app: the running process's TCC identity is the Mach-O currently
executing. Our old shell-script launcher exec'd Homebrew Python, so TCC
attributed every request to `org.python.python`, not `com.avi.scribe` —
which made the Accessibility grant under "Scribe" a no-op. py2app
generates a proper Mach-O launcher inside the bundle, signed as part of
the bundle. The launcher loads Python (dlopen, not exec), so TCC sees
com.avi.scribe for the lifetime of the process.
"""
from setuptools import setup

APP = ["scribe.py"]
DATA_FILES: list[str] = []

PLIST = {
    "CFBundleIdentifier":         "com.avi.scribe",
    "CFBundleName":               "Scribe",
    "CFBundleDisplayName":        "Scribe",
    "CFBundleExecutable":         "Scribe",
    "CFBundleShortVersionString": "1.0",
    "CFBundleVersion":            "1",
    "LSMinimumSystemVersion":     "12.0",
    # Menubar-only: stays out of Dock + app switcher.
    "LSUIElement": True,
    # TCC prompt strings — so dialogs say "Scribe wants …" not "Python wants …".
    "NSMicrophoneUsageDescription":
        "Scribe records your voice so it can transcribe it with Whisper.",
    "NSAppleEventsUsageDescription":
        "Scribe posts ⌘V into the focused text field to paste the transcript.",
    "NSSystemAdministrationUsageDescription":
        "Scribe observes your dictation hotkey so it knows when to record.",
}

OPTIONS = {
    "plist": PLIST,
    # argv emulation feeds command-line args via AppleEvents; a menubar
    # app never needs it and enabling it just delays startup.
    "argv_emulation": False,
    # Explicitly list the pyobjc submodules we use — py2app's modulegraph
    # misses a few without hints, and the bundle fails at import time.
    "includes": [
        "rumps",
        "edge_tts",
        "sounddevice",
        "numpy",
        "httpx",
        "AppKit",
        "Foundation",
        "Quartz",
        "PyObjCTools.AppHelper",
    ],
}

setup(
    name="Scribe",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
