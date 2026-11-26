#!/bin/bash
# Speak selected / clipboard text using the engine selected in Scribe.
#
# Reads the engines from ~/.config/speak-selection/settings.json ("engine":
# the default, "engine_by_lang": one per language; "speechify" | "edge") so
# the global hotkey matches whatever you picked in the Scribe menu for the
# language of the text. Edge, the free one, speaks unless Speechify was
# picked there, and whenever Speechify is unavailable.
#
# Triggered via the "Speak Selection" Quick Action (⌃D) and the clipboard
# one (⌃X). Accepts the text as $1, on stdin (--stdin), or from the clipboard.

KEY_FILE="$HOME/.config/speak-selection/speechify_key"
EDGE="$HOME/bin/edge-tts-stream"
SPEECHIFY="$HOME/bin/speechify-tts-stream"
PICK_ENGINE="$HOME/bin/tts_engine.py"

# --- gather text -----------------------------------------------------------
if [ "$1" = "--stdin" ]; then
    TEXT="$(cat)"
elif [ -n "$1" ]; then
    TEXT="$1"
else
    TEXT="$(pbpaste)"
fi
[ -z "$TEXT" ] && exit 0

# --- pick engine (default edge) -------------------------------------------
# The engine chosen in the Scribe menu for the language of this text, else
# the default one. Edge, the free one, whenever the picker says nothing.
ENGINE="$(printf '%s' "$TEXT" | "$PICK_ENGINE" 2>/dev/null)"

if [ "$ENGINE" != "speechify" ]; then
    HELPER="$EDGE"
else
    HELPER="$SPEECHIFY"
    # Speechify unusable (no helper or no key anywhere)? fall back to Edge so
    # the hotkey still speaks instead of silently doing nothing.
    if [ ! -x "$HELPER" ] || { [ -z "$SPEECHIFY_API_KEY" ] && [ ! -s "$KEY_FILE" ]; }; then
        HELPER="$EDGE"
    fi
fi

# The helpers stop any current playback themselves, read their voice
# from settings.json, and clean up. When synthesis fails they exit 1 with a
# "[…-tts] reason" line on stderr (Stop kills them by signal instead), so say
# why rather than leaving the hotkey silently dead.
ERR="$(printf '%s' "$TEXT" | "$HELPER" --stdin 2>&1 >/dev/null)"
if [ $? -eq 1 ]; then
    REASON="$(printf '%s\n' "$ERR" | sed -n 's/^\[[a-z]*-tts\] //p' | tail -1)"
    # Passed as an argument, never spliced into the AppleScript source.
    osascript -e 'on run argv' \
        -e 'display notification (item 1 of argv) with title "Read aloud failed"' \
        -e 'end run' "${REASON:-The speech helper failed.}" >/dev/null 2>&1
fi
exit 0
