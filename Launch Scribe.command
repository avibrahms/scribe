#!/bin/bash
# Scribe launcher — manual (re)start of the menubar app.
#
# Normally you don't need this: the launchd agent (com.avi.scribe) starts
# Scribe at login and auto-restarts it if it exits. Use this when you
# want a quick kickstart, or during development.
#
# Everything launches through the Scribe.app bundle so macOS TCC
# (microphone / input monitoring / accessibility) attributes permissions
# to com.avi.scribe — not to raw Python.

HERE="$(cd "$(dirname "$0")" && pwd)"
BUNDLE="/Users/avi/Applications/Scribe.app"

# If the launchd agent is loaded, ask it to kickstart — that's the right
# thing to do, since it also supervises restarts.
if launchctl list com.avi.scribe >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$(id -u)/com.avi.scribe" 2>/dev/null
  osascript -e 'display notification "Scribe (re)started via launchd." with title "Scribe"' 2>/dev/null
  exit 0
fi

# Otherwise launch via `open -g` so macOS treats this as a GUI launch of
# the signed bundle (needed for TCC to attach grants to com.avi.scribe).
if pgrep -f "$HERE/scribe.py" >/dev/null 2>&1; then
  osascript -e 'display notification "Scribe is already running — look for the mic icon in the menubar." with title "Scribe"' 2>/dev/null
  exit 0
fi

open -g "$BUNDLE"
sleep 1

if ! pgrep -f "$HERE/scribe.py" >/dev/null 2>&1; then
  tail -n 40 "$HERE/menubar.log" 2>/dev/null
  osascript -e 'display notification "Scribe failed to start — see menubar.log" with title "Scribe"' 2>/dev/null
  exit 1
fi

osascript -e 'display notification "Scribe started — see menubar for the 🎙 icon." with title "Scribe"' 2>/dev/null
exit 0
