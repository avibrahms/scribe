#!/usr/bin/env bash
# Installs (or updates) what Read aloud runs outside the app: the helpers in
# ~/bin and the three Quick Actions the hotkeys are bound to. Called by
# install.sh; run it alone after changing anything in bin/.
#
# Re-running is safe: helpers are replaced, an existing Quick Action is kept.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/bin"
SERVICES_DIR="$HOME/Library/Services"
EDGE_PYTHON="$HOME/.local/pipx/venvs/edge-tts/bin/python"

# --- the Edge voices' library, in its own environment ---------------------
if [[ ! -x "$EDGE_PYTHON" ]]; then
    if ! command -v pipx >/dev/null; then
        echo "error: pipx not found. install via 'brew install pipx'." >&2
        exit 1
    fi
    echo "==> installing edge-tts (pipx)"
    pipx install edge-tts >/dev/null
fi

# --- the audio player ------------------------------------------------------
if [[ ! -x /opt/homebrew/bin/play ]]; then
    echo "warning: /opt/homebrew/bin/play not found; read aloud needs it:" >&2
    echo "         brew install sox" >&2
fi

# --- helpers ---------------------------------------------------------------
echo "==> installing read-aloud helpers in $BIN_DIR"
mkdir -p "$BIN_DIR"
for f in edge-tts-stream speechify-tts-stream speak-selection.sh \
         tts_proc.py tts_lang.py tts_text.py tts_fallback.py; do
    cp "$REPO_DIR/bin/$f" "$BIN_DIR/$f.new"
    mv -f "$BIN_DIR/$f.new" "$BIN_DIR/$f"   # never a half-written helper
done
# The helper of the OpenAI engine, which Speechify replaced.
rm -f "$BIN_DIR/openai-tts-stream"
# The Edge helper runs with the Python that has edge-tts.
sed -i '' "1s|.*|#!$EDGE_PYTHON|" "$BIN_DIR/edge-tts-stream"
chmod +x "$BIN_DIR/edge-tts-stream" "$BIN_DIR/speechify-tts-stream" \
         "$BIN_DIR/speak-selection.sh"
# A standby still holding the previous helper steps aside by itself; this
# only saves it the wait.
pkill -f "edge-tts-stream --standby" 2>/dev/null || true

# --- Quick Actions ---------------------------------------------------------
mkdir -p "$SERVICES_DIR"
installed_one=""
for w in "$REPO_DIR"/services/*.workflow; do
    name="$(basename "$w")"
    if [[ ! -e "$SERVICES_DIR/$name" ]]; then
        cp -R "$w" "$SERVICES_DIR/$name"
        installed_one=1
        echo "==> installed Quick Action: ${name%.workflow}"
    fi
done
if [[ -n "$installed_one" ]]; then
    /System/Library/CoreServices/pbs -update 2>/dev/null || true
    cat <<'MSG'

Give the Quick Actions their shortcuts, once: System Settings → Keyboard →
Keyboard Shortcuts → Services → General:
    Speak Selection Edge TTS   ⌃D
    Speak Clipboard Edge TTS   ⌃X
    Stop Speaking              ⌃S
MSG
fi
