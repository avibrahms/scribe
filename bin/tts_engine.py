#!/usr/bin/env python3
"""
Print the engine ("edge" or "speechify") that reads the text given on stdin:
the one chosen in Scribe's menu for the language of the text, else the
default engine. Used by speak-selection.sh, so the hotkeys pick the same
engine as the app for the same text.

Prints "edge", the free engine, whenever anything is missing or unreadable.
"""
import json
import os
import sys

SETTINGS_FILE = os.path.expanduser("~/.config/speak-selection/settings.json")
ENGINES = ("edge", "speechify")


def pick_engine(text, settings):
    default = settings.get("engine")
    if default not in ENGINES:
        default = "edge"
    mapping = settings.get("engine_by_lang")
    if not mapping or not settings.get("tts_autodetect", True):
        return default
    try:
        import tts_lang  # deployed next to this file
        engine = tts_lang.engine_for_lang(
            mapping, tts_lang.detect_language(text), default)
    except Exception:
        return default
    return engine if engine in ENGINES else default


def main():
    try:
        with open(SETTINGS_FILE) as f:
            settings = json.load(f)
    except Exception:
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    text = sys.stdin.buffer.read().decode("utf-8", "replace")
    print(pick_engine(text, settings))


if __name__ == "__main__":
    main()
