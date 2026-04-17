#!/usr/bin/env bash
# Scribe installer.
#
# Does six things:
#   1. Creates a local venv and pip-installs dependencies.
#   2. Asks for your Groq API key (if not already set) and writes .env.
#   3. Builds a signed Scribe.app bundle via py2app.
#   4. Installs the bundle at ~/Applications/Scribe.app.
#   5. Installs a LaunchAgent that auto-starts Scribe at login + restarts on exit.
#   6. Opens the three macOS permission panes you need to grant (once).
#
# Run from inside the cloned repo:
#     ./install.sh
#
# Re-running is safe — it upserts everything.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_BUNDLE="$HOME/Applications/Scribe.app"
LAUNCH_AGENT="$HOME/Library/LaunchAgents/com.avi.scribe.plist"
LOG_FILE="$REPO_DIR/menubar.log"

cd "$REPO_DIR"

# --- 1. find Python ------------------------------------------------------

PYTHON_BIN="${PYTHON:-$(command -v python3.12 || command -v python3.11 || command -v python3 || true)}"
if [[ -z "$PYTHON_BIN" ]]; then
    echo "error: no python3 found. install via 'brew install python@3.12'." >&2
    exit 1
fi
echo "==> using $PYTHON_BIN"

# --- 2. venv + deps -----------------------------------------------------

if [[ ! -x "$REPO_DIR/venv/bin/python" ]]; then
    echo "==> creating venv"
    "$PYTHON_BIN" -m venv "$REPO_DIR/venv"
fi
echo "==> installing dependencies"
"$REPO_DIR/venv/bin/pip" install --upgrade pip >/dev/null
"$REPO_DIR/venv/bin/pip" install -r "$REPO_DIR/requirements.txt" py2app

# --- 3. Groq API key ----------------------------------------------------

if [[ ! -s "$REPO_DIR/.env" ]] || ! grep -q "^GROQ_API_KEY=" "$REPO_DIR/.env"; then
    echo
    echo "Scribe uses Groq's free Whisper API for dictation."
    echo "Get a free key at https://console.groq.com/keys"
    read -r -p "Paste your Groq API key: " GROQ_KEY
    if [[ -z "$GROQ_KEY" ]]; then
        echo "error: empty key — aborting." >&2
        exit 1
    fi
    printf "GROQ_API_KEY=%s\n" "$GROQ_KEY" > "$REPO_DIR/.env"
    chmod 600 "$REPO_DIR/.env"
    echo "==> wrote $REPO_DIR/.env (chmod 600, gitignored)"
fi

# --- 4. build .app bundle ----------------------------------------------

echo "==> building Scribe.app (py2app alias mode)"
rm -rf "$REPO_DIR/build" "$REPO_DIR/dist"
"$REPO_DIR/venv/bin/python" setup.py py2app -A >/dev/null

# Replace any old bundle at ~/Applications/Scribe.app
mkdir -p "$HOME/Applications"
rm -rf "$APP_BUNDLE"
cp -R "$REPO_DIR/dist/Scribe.app" "$APP_BUNDLE"

# Re-sign with our stable identifier so TCC grants attach to com.avi.scribe
# (and persist across rebuilds as long as the identifier is unchanged).
codesign --force --sign - --identifier com.avi.scribe --deep "$APP_BUNDLE"

# Register with Launch Services so macOS recognises the bundle immediately.
/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister -f "$APP_BUNDLE"

# --- 5. LaunchAgent -----------------------------------------------------

echo "==> installing LaunchAgent at $LAUNCH_AGENT"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$LAUNCH_AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.avi.scribe</string>
    <key>ProgramArguments</key>
    <array>
        <string>$APP_BUNDLE/Contents/MacOS/Scribe</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$REPO_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>LimitLoadToSessionType</key>
    <string>Aqua</string>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>$LOG_FILE</string>
    <key>StandardErrorPath</key>
    <string>$LOG_FILE</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/com.avi.scribe" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENT"

# --- 6. permissions -----------------------------------------------------

cat <<'EOF'

Scribe is now running. Look for 🎙 in your menubar.

One-time permission grants — grant each once, they persist forever:

  1. Input Monitoring  (to observe your dictation hotkey)
  2. Accessibility     (to synthesize ⌘V and paste the transcript)
  3. Microphone        (to record — will prompt automatically on first use)

Opening System Settings at Input Monitoring now. Add Scribe.app and toggle
ON, then switch to the Accessibility pane (sidebar) and do the same.

EOF

open "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent" || true

echo "done."
