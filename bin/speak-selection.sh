#!/bin/bash
# Speak selected / clipboard text using the engine selected in Scribe.
#
# Reads the active engine from ~/.config/speak-selection/settings.json
# ("engine": "openai" | "edge") so the global hotkey matches whatever you
# picked in the Scribe menu. Falls back to Edge if OpenAI is unavailable.
#
# Triggered via the "Speak Selection" Quick Action (⌃D) and the clipboard
# one (⌃X). Accepts the text as $1, on stdin (--stdin), or from the clipboard.

SETTINGS="$HOME/.config/speak-selection/settings.json"
KEY_FILE="$HOME/.config/speak-selection/openai_key"
EDGE="$HOME/bin/edge-tts-stream"
OPENAI="$HOME/bin/openai-tts-stream"

# --- pick engine (default openai) -----------------------------------------
ENGINE="openai"
if [ -f "$SETTINGS" ]; then
    E=$(sed -n 's/.*"engine"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$SETTINGS" | head -1)
    [ -n "$E" ] && ENGINE="$E"
fi

if [ "$ENGINE" = "edge" ]; then
    HELPER="$EDGE"
else
    HELPER="$OPENAI"
    # OpenAI unusable (no helper or no key anywhere)? fall back to Edge so
    # the hotkey still speaks instead of silently doing nothing.
    if [ ! -x "$HELPER" ] || { [ -z "$OPENAI_API_KEY" ] && [ ! -s "$KEY_FILE" ]; }; then
        HELPER="$EDGE"
    fi
fi

# --- gather text -----------------------------------------------------------
if [ "$1" = "--stdin" ]; then
    TEXT="$(cat)"
elif [ -n "$1" ]; then
    TEXT="$1"
else
    TEXT="$(pbpaste)"
fi
[ -z "$TEXT" ] && exit 0

# The helpers stop any current playback themselves, read voice/instructions
# from settings.json, and clean up.
printf '%s' "$TEXT" | "$HELPER" --stdin 2>/dev/null
exit 0
