# Scribe

**A free, local, cross-platform menubar/tray app for push-to-talk dictation and text-to-speech. A drop-in replacement for [Wispr Flow](https://wisprflow.ai/).**

Hold a modifier key anywhere on your computer, speak, release → the transcript pastes into whatever text field you're looking at. Miss the text field? Press `⌃⌘V` (macOS) or `Ctrl+Alt+V` (Windows) to paste the last transcript again. Every transcript is kept in a scrollable history you can copy from.

No subscription, no account, no telemetry. The only thing that leaves your computer is a ~3-second audio clip sent to Groq's free Whisper API, and an optional TTS text string sent to Microsoft Edge's free read-aloud endpoint.

**Supported:** macOS 12+ · Windows 10+

## Why I built it

Wispr Flow is great, but it's **$12/month**, proprietary, and sends your audio and transcripts to their servers. Scribe does the same core job — push-to-talk dictation with automatic paste — for $0, in ~1000 lines of Python you can read in one sitting.

| | Wispr Flow | Scribe |
|---|---|---|
| Push-to-talk anywhere | ✓ | ✓ |
| Auto-pastes into focused field | ✓ | ✓ |
| Transcript history | ✓ | ✓ |
| Multi-language dictation | ✓ | ✓ (Whisper-large-v3-turbo) |
| TTS read-aloud | ✗ | ✓ (Microsoft Edge voices) |
| Clipboard preserved during paste | — | ✓ (macOS: full; Windows: text) |
| Open source | ✗ | ✓ (MIT) |
| Cost | $12 / month | **$0** |

## How it works

- **STT (dictation)**: hold a modifier key (default: Right ⌥/Alt) → record via `sounddevice` → send WAV to **Groq's free Whisper API** (`whisper-large-v3-turbo`) → snapshot the clipboard → paste the transcript → restore your clipboard.
- **TTS (read-aloud)**: any selected text → **Microsoft Edge TTS voices** (Ava, Andrew, Brian, Emma, William, Sonia, Natasha, Denise, Elvira, Katja, Elsa, etc.) → no API key required, free, unlimited.
- **Hotkey observation**: a listen-only global key watcher (`CGEventTap` on macOS, `pynput` on Windows) — never intercepts or blocks keystrokes.
- **Persistence**: auto-starts at login (`launchd` on macOS, Startup-folder shortcut on Windows).

## Repo layout

```
scribe_core.py            — platform-agnostic: audio, transcription, history, voices
scribe.py                 — macOS entry (rumps / AppKit / Quartz)
scribe_windows.py         — Windows entry (pystray / pynput / pyperclip)
setup.py                  — macOS .app build recipe (py2app)
install.sh                — macOS installer
install.bat               — Windows installer
Launch Scribe.command     — macOS launcher (alternative to .app)
Scribe.bat                — Windows launcher
requirements.txt          — shared deps
requirements-mac.txt      — macOS-only deps
requirements-windows.txt  — Windows-only deps
```

Both OS entry points import from `scribe_core`, so logic fixes land in one place.

---

## macOS install

**Requirements:**
- macOS 12 (Monterey) or newer — Scribe uses `CGEventTap`, `NSPasteboard`, `NSEvent`.
- Python 3.11+ (tested on 3.12). `brew install python@3.12`.
- A free Groq API key from [console.groq.com/keys](https://console.groq.com/keys).

```bash
git clone https://github.com/avibrahms/scribe.git ~/Applications/Scribe
cd ~/Applications/Scribe
./install.sh
```

`install.sh` will:
1. Create a `venv/` and install `requirements.txt` + `requirements-mac.txt`.
2. Prompt for your Groq API key and write it to `.env` (gitignored, `chmod 600`).
3. Build `Scribe.app` via `py2app`, ad-hoc signed as `com.avi.scribe`, install to `~/Applications/Scribe.app`.
4. Install a `launchd` agent so it auto-starts at login and restarts if it exits.
5. Open the Input Monitoring pane so you can grant the first permission.

After install, look for 🎙 in your menubar.

### macOS permissions (one-time)

Scribe needs three standard permissions under **System Settings → Privacy & Security**:

- **Input Monitoring** — to observe the push-to-talk hotkey
- **Accessibility** — to synthesize ⌘V and paste the transcript
- **Microphone** — to record

Because Scribe is a signed bundle with a stable identifier, you grant these **once** — they persist across reboots, relaunches, and Python upgrades.

---

## Windows install

**Requirements:**
- Windows 10 or 11.
- Python 3.11+ from [python.org/downloads](https://www.python.org/downloads/) — **during install, tick "Add python.exe to PATH".**
- A free Groq API key from [console.groq.com/keys](https://console.groq.com/keys).

**Install:**
1. Download the repo as a ZIP (green *Code* button → *Download ZIP*) and extract it somewhere like `C:\Scribe\`. (Or `git clone` if you have Git installed.)
2. Open the extracted folder in File Explorer.
3. Double-click **`install.bat`**.
4. When prompted, paste your Groq API key and press Enter.
5. Scribe launches and pins itself to start at login.

Look for the mic icon in the **system tray** (bottom-right of the taskbar). Windows hides tray icons by default — click the `^` chevron and drag Scribe out to make it permanently visible.

### Windows permissions

Windows has no TCC/permissions system to grant — Scribe just works. Caveats:

- **Antivirus**: some AV products (Defender, Kaspersky) may flag `pynput` as a keylogger the first time it observes a key. This is a false positive — `pynput` only watches for your hotkey locally and never sends anything anywhere. Whitelist `pythonw.exe` in your repo's `venv\Scripts\` if it gets blocked.
- **Microphone**: Windows 10/11 may prompt the first time Scribe records. Allow it under Settings → Privacy & security → Microphone.

---

## Usage (both OSes)

- **Dictate**: hold your chosen hotkey → speak → release. Transcript pastes where your cursor is.
- **Re-paste the last transcript**: `⌃⌘V` on macOS, `Ctrl+Alt+V` on Windows.
- **Change hotkey**: tray menu → *Dictation Hotkey*. Options differ slightly per OS (macOS has Fn/🌐 + left/right modifiers; Windows has left/right Alt/Ctrl/Shift/Win).
- **Change dictation language**: tray menu → *Dictation Language*.
- **Speak text aloud**: tray menu → *Test Voice*.
- **Browse history**: tray menu → *History*. Click any entry to copy it.

## File locations

| | macOS | Windows |
|---|---|---|
| Config + history | `~/.config/scribe/` | `%APPDATA%\scribe\` |
| `.env` (API key) | repo dir, `chmod 600` | repo dir |
| Auto-start | `~/Library/LaunchAgents/com.avi.scribe.plist` | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Scribe.lnk` |

## Privacy

- Audio: sent to `api.groq.com` for transcription. Groq's policy is on [their site](https://groq.com/privacy-policy/).
- TTS text: sent to Microsoft's Edge read-aloud endpoint.
- Nothing else leaves your machine. No analytics, no crash reporting, no phone-home.
- Transcript history is stored locally as plain JSON Lines — easy to grep or delete.

## Uninstall

**macOS:**
```bash
launchctl bootout gui/$(id -u)/com.avi.scribe
rm -rf ~/Applications/Scribe ~/Applications/Scribe.app
rm -f ~/Library/LaunchAgents/com.avi.scribe.plist
rm -rf ~/.config/scribe
```

**Windows:** delete the Scribe folder, then delete the Startup shortcut:
```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Scribe.lnk
%APPDATA%\scribe\
```

## Contributing cross-platform changes

All shared logic goes in `scribe_core.py`. If you add a feature:

- If it's pure logic (audio, networking, history, config) → `scribe_core.py`, used by both.
- If it's UI / global hotkey / paste / clipboard → per-OS file (`scribe.py` or `scribe_windows.py`).
- If it's a new OS port → add `scribe_<os>.py` and a matching `requirements-<os>.txt`.

## License

MIT — see [LICENSE](LICENSE). Do whatever you want with it.

## Acknowledgements

- [rumps](https://github.com/jaredks/rumps) — the macOS menubar framework.
- [pystray](https://github.com/moses-palmer/pystray) — the cross-platform tray framework (used on Windows).
- [pynput](https://github.com/moses-palmer/pynput) — cross-platform keyboard observation.
- [edge-tts](https://github.com/rany2/edge-tts) — Microsoft Edge read-aloud voices, no API key.
- [Groq](https://groq.com/) — fast, free Whisper inference.
- [Wispr Flow](https://wisprflow.ai/) — the commercial product that inspired this one.
