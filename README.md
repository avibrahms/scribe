# Scribe

**A free, local, macOS menubar app for push-to-talk dictation and text-to-speech. A drop-in replacement for [Wispr Flow](https://wisprflow.ai/).**

Hold a modifier key anywhere on your Mac, speak, release → the transcript pastes into whatever text field you're looking at. Miss the text field? Press `⌃⌘V` anywhere to paste the last transcript again. Every transcript is kept in a scrollable history you can copy from.

No subscription, no account, no telemetry. The only thing that leaves your Mac is a ~3-second audio clip sent to Groq's free Whisper API, and an optional TTS text string sent to Microsoft Edge's free read-aloud endpoint.

## Why I built it

Wispr Flow is great, but it's **$12/month**, proprietary, and sends your audio and transcripts to their servers. Scribe does the same core job — push-to-talk dictation with automatic paste — for $0, in ~1000 lines of Python you can read in one sitting.

| | Wispr Flow | Scribe |
|---|---|---|
| Push-to-talk anywhere | ✓ | ✓ |
| Auto-pastes into focused field | ✓ | ✓ |
| Transcript history | ✓ | ✓ |
| Multi-language dictation | ✓ | ✓ (Whisper-large-v3-turbo) |
| TTS read-aloud | ✗ | ✓ (Microsoft Edge voices) |
| Clipboard preserved during paste | — | ✓ |
| Open source | ✗ | ✓ (MIT) |
| Cost | $12 / month | **$0** |

## How it works

- **STT (dictation)**: hold a modifier key (default: Right ⌥ Option — pick yours in the menu) → record via `sounddevice` → send WAV to **Groq's free Whisper API** (`whisper-large-v3-turbo`) → snapshot the clipboard → paste the transcript → restore your clipboard. Your previous clipboard contents are *not* destroyed.
- **TTS (read-aloud)**: any selected text → **Microsoft Edge TTS voices** (Ava, Andrew, Brian, Emma, William, Sonia, Natasha, Denise, Elvira, Katja, Elsa, etc.) → no API key required, free, unlimited.
- **Hotkey observation**: a `CGEventTap` in listen-only mode watches for your chosen modifier's press/release and for `⌃⌘V`. Never intercepts or blocks keystrokes.
- **Persistence**: runs under `launchd` with `KeepAlive=true` — survives reboots and relaunches immediately if killed.
- **Proper `.app` bundle**: a signed macOS app bundle so permission grants attach to `com.avi.scribe` and stick. No re-granting Input Monitoring every time Homebrew updates Python.

## Requirements

- **macOS 12 (Monterey) or newer** — Scribe uses `CGEventTap`, `NSPasteboard`, `NSEvent`, etc. It is **not** cross-platform.
- **Python 3.11+** (tested on 3.12). Homebrew: `brew install python@3.12`.
- A **free Groq API key** from [console.groq.com/keys](https://console.groq.com/keys). Groq's free tier is more than enough for personal dictation.
- ~15 MB disk for the venv + dependencies.

## Install

```bash
git clone https://github.com/avibrahms/scribe.git ~/Applications/Scribe
cd ~/Applications/Scribe
./install.sh
```

`install.sh` will:
1. Create a `venv/` and `pip install` the dependencies (including `py2app`).
2. Prompt you for your Groq API key and write it to a `.env` file (not committed to git, `chmod 600`).
3. Build a proper `Scribe.app` bundle with `py2app`, ad-hoc signed as `com.avi.scribe`, at `~/Applications/Scribe.app`.
4. Install a `launchd` agent at `~/Library/LaunchAgents/com.avi.scribe.plist` that auto-starts Scribe at login and restarts it if it exits.
5. Register the bundle with Launch Services and open the Input Monitoring pane so you can grant the first permission.

After install, you'll see a 🎙 icon in your menubar.

### Grant permissions (one-time)

Scribe needs three standard macOS permissions. Grant each under **System Settings → Privacy & Security**:

- **Input Monitoring** — to observe the push-to-talk hotkey
- **Accessibility** — to synthesize ⌘V and paste the transcript
- **Microphone** — to record

Because Scribe is a proper signed bundle, you grant these **once** — they persist across reboots, relaunches, and Python upgrades. No more weekly re-authorization.

## Usage

- **Dictate**: hold your chosen hotkey → speak → release. Transcript pastes where your cursor is.
- **Re-paste the last transcript**: `⌃⌘V` anywhere (even after you've copied something else — Scribe preserves and restores your clipboard).
- **Change hotkey**: menubar → Dictation Hotkey. Options: Fn/🌐, Right/Left Option, Right Command, Right Shift, Right Control.
- **Change dictation language**: menubar → Dictation Language. Default is English; Auto-detect also available.
- **Speak text aloud**: menubar → Test Voice, or wire `speak-selection` to the shared config at `~/.config/speak-selection/config`.
- **Browse history**: menubar → History. Click any entry to copy it.

## File layout

```
~/Applications/Scribe/        # your clone
  scribe.py                   # the app (one file, ~1000 LOC)
  setup.py                    # py2app build recipe
  install.sh                  # one-shot installer
  requirements.txt
  .env                        # Groq API key (gitignored, 0600)

~/Applications/Scribe.app/    # signed py2app bundle (built by install.sh)
~/Library/LaunchAgents/com.avi.scribe.plist
~/.config/scribe/             # history.jsonl, config.json
~/.config/speak-selection/    # voice preference (shared with other TTS tools)
```

## Privacy

- Audio: sent to `api.groq.com` for transcription. Groq's policy is on [their site](https://groq.com/privacy-policy/).
- TTS text: sent to Microsoft's Edge read-aloud endpoint.
- Nothing else leaves your Mac. No analytics, no crash reporting, no phone-home.
- Transcript history is stored locally at `~/.config/scribe/history.jsonl` (plain JSON Lines, one per line, easy to grep or delete).

## Uninstall

```bash
launchctl bootout gui/$(id -u)/com.avi.scribe
rm -rf ~/Applications/Scribe ~/Applications/Scribe.app
rm -f ~/Library/LaunchAgents/com.avi.scribe.plist
rm -rf ~/.config/scribe
```

## License

MIT — see [LICENSE](LICENSE). Do whatever you want with it.

## Acknowledgements

- [rumps](https://github.com/jaredks/rumps) — the menubar framework.
- [edge-tts](https://github.com/rany2/edge-tts) — Microsoft Edge read-aloud voices, no API key.
- [Groq](https://groq.com/) — fast, free Whisper inference.
- [Wispr Flow](https://wisprflow.ai/) — the commercial product that inspired this one. It's polished and worth the money if you don't want to self-host.
