#!/usr/bin/env python3
"""
Scribe — macOS menubar app for voice in and voice out.

Two things in one:

  • TTS  — free Microsoft Edge voices via the `edge-tts` package. A voice
           picker writes the selection to ~/.config/speak-selection/config
           so any external "speak selection" hotkey you already use keeps
           working with no extra setup.

  • STT  — hold a modifier key to record. Pick which key from the
           "Dictation Hotkey" menu (Right ⌥ Option, Fn/🌐 Globe, etc).
           On release, the clip is sent to Groq Whisper
           (whisper-large-v3-turbo, free) and the transcript is pasted
           into whatever text field currently has focus. Every transcript
           is kept in History and is one click away from the menubar.

           Missed the text field? Press ⌃⌘V anywhere to paste the last
           transcript again.

The Groq API key is loaded (in order) from:
    $GROQ_API_KEY env var  →  ./.env (in the app folder)  →  ~/.config/scribe/config.json

Permissions needed on first launch (System Settings → Privacy & Security):
  • Microphone            — to record
  • Input Monitoring      — to observe the hotkey
  • Accessibility         — to post ⌘V into the focused field
"""

from __future__ import annotations

# -- Accessory activation policy: off the Dock, windows can focus ----------
# LSBackgroundOnly=1 would prevent any modal dialog (e.g. "Set Groq API
# key…") from taking focus — the dialog appears but can't be interacted
# with, which looks like the whole app has frozen. LSUIElement is the
# standard policy for menubar apps and lets us briefly activate when a
# dialog needs attention.
from AppKit import NSBundle
NSBundle.mainBundle().infoDictionary()["LSUIElement"] = "1"

import io
import json
import os
import subprocess
import sys
import threading
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path

import rumps
import numpy as np
import sounddevice as sd
import httpx

from AppKit import (
    NSApp,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyRegular,
    NSPasteboard,
    NSPasteboardTypeString,
    NSSound,
)
from contextlib import contextmanager
from Foundation import NSURL
from PyObjCTools.AppHelper import callAfter
from Quartz import (
    CGEventTapCreate,
    CGEventTapEnable,
    CGEventGetFlags,
    CGEventGetIntegerValueField,
    CGEventMaskBit,
    CGEventCreateKeyboardEvent,
    CGEventPost,
    CGEventSetFlags,
    CFMachPortCreateRunLoopSource,
    CFRunLoopAddSource,
    CFRunLoopRun,
    CFRunLoopGetCurrent,
    kCFRunLoopCommonModes,
    kCGEventFlagsChanged,
    kCGEventKeyDown,
    kCGHIDEventTap,
    kCGHeadInsertEventTap,
    kCGAnnotatedSessionEventTap,
    kCGSessionEventTap,
    kCGKeyboardEventKeycode,
)


# ---------- paths / config ------------------------------------------------

APP_NAME = "scribe"
APP_DIR = Path(__file__).resolve().parent
DOTENV_FILE = APP_DIR / ".env"
CONFIG_DIR = Path.home() / ".config" / APP_NAME
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = CONFIG_DIR / "history.jsonl"
CONFIG_FILE = CONFIG_DIR / "config.json"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader — no extra dependency. Existing env wins."""
    if not path.exists():
        return
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            if (v.startswith('"') and v.endswith('"')) or (
                v.startswith("'") and v.endswith("'")
            ):
                v = v[1:-1]
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception as exc:
        print(f"[.env] load error: {exc}", file=sys.stderr)


_load_dotenv(DOTENV_FILE)

# Shared voice config, same path v1 used — so any external tool that already
# reads it (e.g. a "Speak Selection" hotkey) keeps working with v2.
SPEAK_SELECTION_DIR = Path.home() / ".config" / "speak-selection"
SPEAK_SELECTION_DIR.mkdir(parents=True, exist_ok=True)
VOICE_CONFIG_FILE = SPEAK_SELECTION_DIR / "config"
SETTINGS_FILE = SPEAK_SELECTION_DIR / "settings.json"


# ---------- voices --------------------------------------------------------
# A curated subset of Microsoft's Edge neural voices. All free, no key.
VOICES = {
    "English (US)": [
        ("Ava · warm, expressive",         "en-US-AvaMultilingualNeural"),
        ("Andrew · natural male",          "en-US-AndrewMultilingualNeural"),
        ("Emma · friendly female",         "en-US-EmmaMultilingualNeural"),
        ("Brian · clear male",             "en-US-BrianMultilingualNeural"),
        ("Aria · news anchor",             "en-US-AriaNeural"),
        ("Jenny · casual female",          "en-US-JennyNeural"),
        ("Guy · confident male",           "en-US-GuyNeural"),
    ],
    "English (UK)":  [
        ("Sonia · UK female",              "en-GB-SoniaNeural"),
        ("Ryan · UK male",                 "en-GB-RyanNeural"),
    ],
    "English (AU)":  [
        # Microsoft does not ship an `en-US-William`. William is AU-only.
        ("William · AU male",              "en-AU-WilliamNeural"),
        ("Natasha · AU female",            "en-AU-NatashaNeural"),
    ],
    "French":        [
        ("Denise · France female",         "fr-FR-DeniseNeural"),
        ("Henri · France male",            "fr-FR-HenriNeural"),
        ("Vivienne · FR multilingual",     "fr-FR-VivienneMultilingualNeural"),
        ("Remy · FR multilingual",         "fr-FR-RemyMultilingualNeural"),
    ],
    "Spanish":       [
        ("Elvira · ES female",             "es-ES-ElviraNeural"),
        ("Alvaro · ES male",               "es-ES-AlvaroNeural"),
    ],
    "German":        [
        ("Katja · DE female",              "de-DE-KatjaNeural"),
        ("Conrad · DE male",               "de-DE-ConradNeural"),
    ],
    "Italian":       [
        ("Elsa · IT female",               "it-IT-ElsaNeural"),
        ("Diego · IT male",                "it-IT-DiegoNeural"),
    ],
}

DEFAULT_VOICE = "en-US-AvaMultilingualNeural"


# ---------- helpers: foreground activation --------------------------------

@contextmanager
def _foreground_app():
    """
    Temporarily promote the app to a Regular activation policy so modal
    dialogs (rumps.Window, rumps.alert) can take focus. Without this, under
    LSUIElement the dialog appears but the menu system is left in a modal
    state with no interactable window — looks like a freeze.
    """
    app = NSApp()
    restored = False
    try:
        if app is not None:
            app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
            app.activateIgnoringOtherApps_(True)
            restored = True
        yield
    finally:
        if restored and app is not None:
            app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)


# ---------- helpers: pasteboard / paste -----------------------------------

def copy_to_pasteboard(text: str) -> None:
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)


def post_cmd_v() -> None:
    """Send ⌘V so whatever has focus receives a paste."""
    # Virtual key code for 'v' on an ANSI US layout is 9.
    V = 9
    CMD = 1 << 20  # kCGEventFlagMaskCommand

    down = CGEventCreateKeyboardEvent(None, V, True)
    CGEventSetFlags(down, CMD)
    CGEventPost(kCGHIDEventTap, down)

    up = CGEventCreateKeyboardEvent(None, V, False)
    CGEventSetFlags(up, CMD)
    CGEventPost(kCGHIDEventTap, up)


def _snapshot_pasteboard() -> list[dict[str, bytes]]:
    """
    Take a best-effort snapshot of the current general pasteboard — every
    item × every declared type → raw data bytes. Good enough to restore
    text, RTF, images, files, and most anything else the OS puts there.
    """
    pb = NSPasteboard.generalPasteboard()
    items = pb.pasteboardItems() or []
    snap: list[dict[str, bytes]] = []
    for item in items:
        types = list(item.types() or [])
        blob: dict[str, bytes] = {}
        for t in types:
            data = item.dataForType_(t)
            if data is None:
                continue
            # NSData → python bytes via bytes(...)
            try:
                blob[str(t)] = bytes(data)
            except Exception:
                pass
        if blob:
            snap.append(blob)
    return snap


def _restore_pasteboard(snapshot: list[dict[str, bytes]]) -> None:
    from AppKit import NSPasteboardItem
    from Foundation import NSData
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    items = []
    for blob in snapshot:
        item = NSPasteboardItem.alloc().init()
        for t, data in blob.items():
            nsdata = NSData.dataWithBytes_length_(data, len(data))
            item.setData_forType_(nsdata, t)
        items.append(item)
    if items:
        pb.writeObjects_(items)


def paste_text(text: str) -> None:
    """
    Paste `text` into the focused field without permanently clobbering the
    user's clipboard. Flow:

      1. Snapshot whatever is in the clipboard right now.
      2. Put our transcript in the clipboard and synthesize ⌘V.
      3. Wait for the target app to consume the paste, then restore the
         original clipboard contents.

    This means after dictation finishes, ⌘V in any other app still pastes
    whatever the user had on their clipboard before dictating.
    """
    snapshot = _snapshot_pasteboard()
    try:
        copy_to_pasteboard(text)
        # Tiny delay so the pasteboard is ready before the key event lands.
        time.sleep(0.03)
        post_cmd_v()
        # Give the receiving app time to actually read the pasteboard
        # before we restore the original contents. 250ms is enough for
        # every app I've tested; slow Electron apps may need more.
        time.sleep(0.25)
    finally:
        try:
            _restore_pasteboard(snapshot)
        except Exception as exc:
            # Never let clipboard-restore failure break dictation.
            print(f"[pasteboard] restore failed: {exc}", file=sys.stderr)


# ---------- helpers: history ----------------------------------------------

def append_history(text: str, duration_ms: int) -> None:
    row = {
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "text": text,
        "duration_ms": duration_ms,
    }
    with HISTORY_FILE.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_history(limit: int = 50) -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    rows: list[dict] = []
    with HISTORY_FILE.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows[-limit:][::-1]  # newest first


def clear_history() -> None:
    if HISTORY_FILE.exists():
        HISTORY_FILE.unlink()


# ---------- helpers: config & API key -------------------------------------

def load_cfg() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_cfg(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def groq_api_key() -> str:
    k = (os.environ.get("GROQ_API_KEY") or "").strip()
    if k:
        return k
    cfg = load_cfg()
    return str(cfg.get("groq_api_key", "")).strip()


def save_groq_key_to_dotenv(key: str) -> None:
    """Upsert GROQ_API_KEY into the app's .env file with 0600 perms."""
    key = key.strip()
    existing: list[str] = []
    if DOTENV_FILE.exists():
        existing = DOTENV_FILE.read_text().splitlines()
    out: list[str] = []
    replaced = False
    for line in existing:
        if line.strip().startswith("GROQ_API_KEY="):
            out.append(f"GROQ_API_KEY={key}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"GROQ_API_KEY={key}")
    DOTENV_FILE.write_text("\n".join(out) + "\n")
    try:
        os.chmod(DOTENV_FILE, 0o600)
    except Exception:
        pass


# ---------- TTS voice config (shared with v1's speak-selection) -----------

def _parse_shell_kv(text: str) -> dict[str, str]:
    """v1 writes shell-style KEY=\"VALUE\" lines. Parse that."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (
            v.startswith("'") and v.endswith("'")
        ):
            v = v[1:-1]
        out[k.strip()] = v
    return out


def load_voice() -> str:
    try:
        if VOICE_CONFIG_FILE.exists():
            raw = VOICE_CONFIG_FILE.read_text()
            kv = _parse_shell_kv(raw)
            v = kv.get("VOICE") or raw.strip()
            return v or DEFAULT_VOICE
    except Exception:
        pass
    return DEFAULT_VOICE


def save_voice(voice: str) -> None:
    """Write shell-sourceable config that is compatible with whatever tool
    already reads VOICE/RATE/PITCH from this file."""
    kv: dict[str, str] = {"RATE": "+0%", "PITCH": "+0Hz"}
    if VOICE_CONFIG_FILE.exists():
        try:
            kv.update(_parse_shell_kv(VOICE_CONFIG_FILE.read_text()))
        except Exception:
            pass
    kv["VOICE"] = voice
    out = "\n".join(f'{k}="{v}"' for k, v in kv.items()) + "\n"
    VOICE_CONFIG_FILE.write_text(out)

    settings = {}
    if SETTINGS_FILE.exists():
        try:
            settings = json.loads(SETTINGS_FILE.read_text())
        except Exception:
            settings = {}
    settings["voice"] = voice
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


# ---------- audio recorder ------------------------------------------------

class Recorder:
    """Simple in-memory recorder using sounddevice. Mono, 16 kHz, int16."""

    SAMPLE_RATE = 16000

    def __init__(self) -> None:
        self._chunks: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._started_at: float = 0.0

    def start(self) -> None:
        self._chunks = []
        self._started_at = time.time()

        def cb(indata, frames, t, status):  # noqa: ANN001
            if status:
                # under/overflow — log but keep recording
                pass
            self._chunks.append(indata.copy())

        self._stream = sd.InputStream(
            samplerate=self.SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=0,
            callback=cb,
        )
        self._stream.start()

    def stop(self) -> tuple[bytes, int]:
        """Returns (wav_bytes, duration_ms). Empty bytes if nothing captured."""
        duration_ms = int((time.time() - self._started_at) * 1000)
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if not self._chunks:
            return b"", duration_ms

        data = np.concatenate(self._chunks, axis=0)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.SAMPLE_RATE)
            w.writeframes(data.tobytes())
        return buf.getvalue(), duration_ms


# ---------- Whisper STT via Groq ------------------------------------------

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
STT_MODEL = "whisper-large-v3-turbo"


def transcribe(wav_bytes: bytes, language: str = "en") -> str:
    key = groq_api_key()
    if not key:
        return ""
    if not wav_bytes:
        return ""
    try:
        with httpx.Client(timeout=30.0) as c:
            r = c.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("rec.wav", wav_bytes, "audio/wav")},
                data={
                    "model": STT_MODEL,
                    "language": language,
                    "response_format": "json",
                    "temperature": "0",
                },
            )
            r.raise_for_status()
            return (r.json().get("text") or "").strip()
    except Exception as exc:
        print(f"[stt] {exc}", file=sys.stderr)
        return ""


# Whisper is famous for hallucinating these on silent / noisy clips.
_JUNK = {
    "you", "thanks", "thankyou", "bye", "okay", "ok",
    "thanksforwatching", "subscribe", "thanksforwatchingthevideo",
    "pleasesubscribe", "music",
}


def is_garbage(text: str) -> bool:
    norm = "".join(ch for ch in text.lower() if ch.isalpha())
    return len(norm) < 2 or norm in _JUNK


# ---------- Hotkey watcher (runs on a background thread) ------------------

# CGEvent modifier-flag masks
_FLAG_SHIFT   = 0x00020000
_FLAG_CONTROL = 0x00040000
_FLAG_OPTION  = 0x00080000  # Alternate / Option
_FLAG_COMMAND = 0x00100000
_FLAG_FN      = 0x00800000  # Secondary Fn / Globe

# Virtual keycodes for the modifier keys (left vs right are distinct).
#   Fn/Globe = 63, but Wispr Flow and macOS "Hold Globe to dictate" both
#   claim Fn, so we default to Right Option to avoid collisions.
HOTKEYS: dict[str, tuple[int, int, str]] = {
    # id          : (virtual_keycode, flag_mask, label)
    "right_option":  (61, _FLAG_OPTION,  "Right ⌥ Option"),
    "left_option":   (58, _FLAG_OPTION,  "Left ⌥ Option"),
    "right_command": (54, _FLAG_COMMAND, "Right ⌘ Command"),
    "right_shift":   (60, _FLAG_SHIFT,   "Right ⇧ Shift"),
    "right_control": (62, _FLAG_CONTROL, "Right ⌃ Control"),
    "fn":            (63, _FLAG_FN,      "Fn / 🌐 Globe"),
}
DEFAULT_HOTKEY = "right_option"


def load_hotkey_id() -> str:
    # env var wins; else config.json; else default.
    hk = (os.environ.get("EDGETTS_HOTKEY") or "").strip().lower()
    if hk in HOTKEYS:
        return hk
    cfg = load_cfg()
    hk = str(cfg.get("hotkey", "")).strip().lower()
    if hk in HOTKEYS:
        return hk
    return DEFAULT_HOTKEY


def save_hotkey_id(hotkey_id: str) -> None:
    cfg = load_cfg()
    cfg["hotkey"] = hotkey_id
    save_cfg(cfg)


# Paste-last-transcript shortcut: ⌃⌘V (Control + Command + V).
# V has virtual keycode 9 on Mac ANSI layouts.
_KEYCODE_V = 9
_PASTE_LAST_MODS = _FLAG_CONTROL | _FLAG_COMMAND
# Mask of modifiers we compare against, ignoring CapsLock and NumericPad bits.
_MOD_COMPARE_MASK = _FLAG_SHIFT | _FLAG_CONTROL | _FLAG_OPTION | _FLAG_COMMAND


class HotkeyWatcher:
    """
    Runs a CGEventTap on a background thread. Fires:
      • on press/release of the chosen push-to-talk modifier key
      • on keydown of ⌃⌘V (paste-last-transcript)
    """

    def __init__(self, hotkey_id: str, on_change, on_paste_last,
                 on_tap_silent=None) -> None:
        self._on_change = on_change
        self._on_paste_last = on_paste_last
        # Called on the main thread if the tap goes >N seconds without
        # seeing a single event (strong signal Input Monitoring is not
        # granted — CGEventTapCreate returns a handle but delivers nothing).
        self._on_tap_silent = on_tap_silent
        self._events_seen = 0
        self._thread: threading.Thread | None = None
        self._tap = None
        self._pressed = False
        self.set_hotkey(hotkey_id)

    def set_hotkey(self, hotkey_id: str) -> None:
        keycode, mask, label = HOTKEYS.get(hotkey_id, HOTKEYS[DEFAULT_HOTKEY])
        self._keycode = keycode
        self._mask = mask
        self._label = label
        self._pressed = False  # drop any stuck state

    @property
    def label(self) -> str:
        return self._label

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _callback(self, proxy, event_type, event, refcon):  # noqa: ANN001
        self._events_seen += 1
        try:
            if event_type == kCGEventFlagsChanged:
                kc = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
                if kc == self._keycode:
                    flags = CGEventGetFlags(event)
                    pressed_now = bool(flags & self._mask)
                    if pressed_now != self._pressed:
                        self._pressed = pressed_now
                        self._on_change(pressed_now)
            elif event_type == kCGEventKeyDown:
                kc = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
                if kc == _KEYCODE_V:
                    mods = CGEventGetFlags(event) & _MOD_COMPARE_MASK
                    if mods == _PASTE_LAST_MODS:
                        self._on_paste_last()
        except Exception as exc:
            print(f"[hotkey] cb error: {exc}", file=sys.stderr)
        return event

    def _run(self) -> None:
        mask = CGEventMaskBit(kCGEventFlagsChanged) | CGEventMaskBit(kCGEventKeyDown)
        tap = CGEventTapCreate(
            kCGSessionEventTap,
            kCGHeadInsertEventTap,
            1,  # kCGEventTapOptionListenOnly -> don't intercept, just observe
            mask,
            self._callback,
            None,
        )
        if not tap:
            print(
                "[hotkey] could not create event tap. "
                "Grant 'Input Monitoring' to this app in System Settings.",
                file=sys.stderr,
            )
            return
        self._tap = tap
        source = CFMachPortCreateRunLoopSource(None, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopCommonModes)
        CGEventTapEnable(tap, True)

        # TCC silent-failure detector: if we receive zero events in 20s of
        # wall time, Input Monitoring is almost certainly not granted.
        # (Any normal typing/mouse modifier change generates events.)
        def _silence_check() -> None:
            time.sleep(20)
            if self._events_seen == 0 and self._on_tap_silent is not None:
                try:
                    self._on_tap_silent()
                except Exception as exc:
                    print(f"[hotkey] silence cb: {exc}", file=sys.stderr)
        threading.Thread(target=_silence_check, daemon=True).start()

        CFRunLoopRun()


# ---------- rumps app -----------------------------------------------------

class ScribeApp(rumps.App):
    IDLE_TITLE = "🎙"
    REC_TITLE = "🔴"
    BUSY_TITLE = "✎"

    def __init__(self) -> None:
        super().__init__(ScribeApp.IDLE_TITLE, quit_button=None)

        self.recorder = Recorder()
        self.recording = False
        self._rec_lock = threading.Lock()
        self._lang = "en"  # default transcription language

        self.current_voice = load_voice()
        self.hotkey_id = load_hotkey_id()

        self._build_menu()

        # Hotkey watcher on background thread. Handles both the push-to-talk
        # modifier and the global ⌃⌘V "paste last transcript" shortcut.
        self.hotkey = HotkeyWatcher(
            self.hotkey_id,
            on_change=self._on_fn_change,
            on_paste_last=self._on_paste_last_shortcut,
            on_tap_silent=self._on_tap_silent,
        )
        self.hotkey.start()

    # -- menu construction ------------------------------------------------

    def _build_menu(self) -> None:
        # Status line at top (not clickable)
        hk_label = HOTKEYS[self.hotkey_id][2]
        self.status_item = rumps.MenuItem(f"Ready — hold {hk_label} to dictate")
        self.status_item.set_callback(None)

        # Dictation hotkey picker
        self.hotkey_menu = rumps.MenuItem("Dictation Hotkey")
        for hk_id, (_kc, _mask, label) in HOTKEYS.items():
            mi = rumps.MenuItem(label, callback=self._make_hotkey_cb(hk_id))
            if hk_id == self.hotkey_id:
                mi.state = 1
            self.hotkey_menu.add(mi)

        # TTS voice picker
        self.voice_menu = rumps.MenuItem("TTS Voice")
        for lang, voices in VOICES.items():
            sub = rumps.MenuItem(lang)
            for label, voice_id in voices:
                item = rumps.MenuItem(
                    label,
                    callback=self._make_voice_cb(voice_id),
                )
                if voice_id == self.current_voice:
                    item.state = 1
                sub.add(item)
            self.voice_menu.add(sub)

        # Test speak
        self.test_item = rumps.MenuItem("Test Voice", callback=self.on_test_voice)

        # Language for STT
        self.lang_menu = rumps.MenuItem("Dictation Language")
        for code, label in [
            ("en", "English"), ("fr", "French"), ("es", "Spanish"),
            ("de", "German"), ("it", "Italian"), ("auto", "Auto-detect"),
        ]:
            mi = rumps.MenuItem(label, callback=self._make_lang_cb(code))
            if code == self._lang:
                mi.state = 1
            self.lang_menu.add(mi)

        # History submenu (the "Paste last transcript  ⌃⌘V" entry lives
        # inside this submenu — see _refresh_history_menu).
        self.history_menu = rumps.MenuItem("History")
        self._refresh_history_menu()

        # Permissions submenu — the three TCC panes Scribe needs. When
        # something breaks (paste silently drops, hotkey stops firing) the
        # user's first stop is this menu, and one click opens the exact
        # pane that matters. Keeping entries separate (instead of one
        # "Open Permissions…" button) because Input Monitoring and
        # Accessibility failures look different from the outside.
        self.perms_item = rumps.MenuItem("Permissions")
        self.perms_item.add(rumps.MenuItem(
            "Input Monitoring (hotkey)",
            callback=self._make_perm_cb("Privacy_ListenEvent"),
        ))
        self.perms_item.add(rumps.MenuItem(
            "Accessibility (paste)",
            callback=self._make_perm_cb("Privacy_Accessibility"),
        ))
        self.perms_item.add(rumps.MenuItem(
            "Microphone",
            callback=self._make_perm_cb("Privacy_Microphone"),
        ))

        # Key status
        self.set_key_item = rumps.MenuItem("Set Groq API key…", callback=self.on_set_key)

        self.menu = [
            self.status_item,
            None,
            self.voice_menu,
            self.test_item,
            None,
            self.hotkey_menu,
            self.lang_menu,
            self.history_menu,
            None,
            self.perms_item,
            self.set_key_item,
            None,
            rumps.MenuItem("Quit Scribe", callback=self._quit),
        ]

    def _refresh_history_menu(self) -> None:
        # Rebuild the submenu from the current jsonl file.
        # Skip .clear() if the submenu hasn't been attached to a parent yet
        # (rumps sets the underlying NSMenu only after attachment).
        try:
            if getattr(self.history_menu, "_menu", None) is not None:
                self.history_menu.clear()
            else:
                # Initial build: drop existing keys via rumps public API if any.
                for k in list(self.history_menu.keys()):
                    del self.history_menu[k]
        except Exception:
            pass
        rows = load_history(limit=30)
        if not rows:
            empty = rumps.MenuItem("(empty — nothing dictated yet)")
            empty.set_callback(None)
            self.history_menu.add(empty)
        else:
            for row in rows:
                text = row["text"]
                preview = text if len(text) <= 60 else text[:57] + "…"
                title = f"⧉  {preview}"
                mi = rumps.MenuItem(title, callback=self._make_copy_cb(text))
                self.history_menu.add(mi)
        self.history_menu.add(None)
        self.history_menu.add(rumps.MenuItem(
            "Paste last transcript  ⌃⌘V",
            callback=self.on_paste_last_menu,
        ))
        self.history_menu.add(rumps.MenuItem("Copy last", callback=self.on_copy_last))
        self.history_menu.add(
            rumps.MenuItem("Open history file", callback=self.on_open_history_file)
        )
        self.history_menu.add(
            rumps.MenuItem("Clear history", callback=self.on_clear_history)
        )

    # -- callbacks --------------------------------------------------------

    def _make_voice_cb(self, voice_id: str):
        def cb(sender: rumps.MenuItem) -> None:
            # Clear previous check marks in this submenu tree.
            for sub in self.voice_menu.values():
                for item in sub.values():
                    item.state = 0
            sender.state = 1
            self.current_voice = voice_id
            save_voice(voice_id)
            rumps.notification("Voice set", voice_id, "")
        return cb

    def _make_lang_cb(self, code: str):
        def cb(sender: rumps.MenuItem) -> None:
            for item in self.lang_menu.values():
                item.state = 0
            sender.state = 1
            self._lang = code
        return cb

    def _make_copy_cb(self, text: str):
        def cb(_sender: rumps.MenuItem) -> None:
            copy_to_pasteboard(text)
            rumps.notification("Copied", "", text[:90])
        return cb

    def _make_hotkey_cb(self, hotkey_id: str):
        def cb(sender: rumps.MenuItem) -> None:
            for item in self.hotkey_menu.values():
                item.state = 0
            sender.state = 1
            self.hotkey_id = hotkey_id
            save_hotkey_id(hotkey_id)
            self.hotkey.set_hotkey(hotkey_id)
            label = HOTKEYS[hotkey_id][2]
            self.status_item.title = f"Ready — hold {label} to dictate"
            rumps.notification("Dictation hotkey", "", f"Hold {label} to record.")
        return cb

    def on_copy_last(self, _sender) -> None:
        rows = load_history(limit=1)
        if not rows:
            rumps.notification("History empty", "", "")
            return
        copy_to_pasteboard(rows[0]["text"])
        rumps.notification("Copied last", "", rows[0]["text"][:90])

    def on_paste_last_menu(self, _sender) -> None:
        """Menu-click equivalent of ⌃⌘V. Runs on the main thread already."""
        rows = load_history(limit=1)
        if not rows:
            rumps.notification("Nothing to paste", "", "History is empty.")
            return
        copy_to_pasteboard(rows[0]["text"])
        # From the menu we can't reliably paste into the previously-focused
        # field, so just copy and tell the user.
        rumps.notification(
            "Copied to clipboard",
            "",
            "⌘V to paste it wherever you want.",
        )

    def _on_paste_last_shortcut(self) -> None:
        """
        ⌃⌘V global shortcut: paste the last transcript into the focused
        field while preserving the user's clipboard. Runs on the event-tap
        thread.
        """
        rows = load_history(limit=1)
        if not rows:
            callAfter(lambda: rumps.notification(
                "Nothing to paste", "", "History is empty."
            ))
            return
        # Small delay so the user's ⌃⌘V keyup is fully processed before we
        # synthesize ⌘V — otherwise the modifier state can bleed through.
        time.sleep(0.05)
        paste_text(rows[0]["text"])

    def on_open_history_file(self, _sender) -> None:
        if not HISTORY_FILE.exists():
            HISTORY_FILE.touch()
        subprocess.run(["open", str(HISTORY_FILE)])

    def on_clear_history(self, _sender) -> None:
        with _foreground_app():
            resp = rumps.alert(
                title="Clear dictation history?",
                message="This will delete every saved transcript.",
                ok="Clear", cancel="Cancel",
            )
        if resp == 1:
            clear_history()
            self._refresh_history_menu()

    def _make_perm_cb(self, pane: str):
        """Each permissions submenu entry opens one specific TCC pane."""
        url = f"x-apple.systempreferences:com.apple.preference.security?{pane}"
        def cb(_sender: rumps.MenuItem) -> None:
            subprocess.run(["open", url])
        return cb

    def _on_tap_silent(self) -> None:
        """
        Called from the tap thread after 20s of silence — almost certainly
        missing Input Monitoring permission. Surface a clear notification
        and open the correct Settings pane on the main thread.
        """
        def _ui() -> None:
            hk_label = HOTKEYS[self.hotkey_id][2]
            self.status_item.title = "⚠ Grant Input Monitoring to Scribe"
            rumps.notification(
                "Scribe needs Input Monitoring",
                "Hotkey won't work until granted",
                f"System Settings → Privacy & Security → Input Monitoring. "
                f"Add Scribe, then dictation ({hk_label}) and ⌃⌘V will work.",
            )
            subprocess.run([
                "open",
                "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
            ])
        callAfter(_ui)

    def on_set_key(self, _sender) -> None:
        # Under LSUIElement, we have to briefly activate the app for the
        # modal dialog to take focus — otherwise the user sees grayed-out
        # menus and can't dismiss the invisible modal.
        with _foreground_app():
            win = rumps.Window(
                title="Groq API key",
                message="Paste your Groq API key (used for Whisper STT).",
                default_text=groq_api_key(),
                ok="Save", cancel="Cancel",
                dimensions=(360, 24),
            )
            resp = win.run()
        if resp.clicked:
            new_key = resp.text.strip()
            save_groq_key_to_dotenv(new_key)
            os.environ["GROQ_API_KEY"] = new_key
            rumps.notification("Groq key saved", "", f"Written to {DOTENV_FILE}")

    def on_test_voice(self, _sender) -> None:
        threading.Thread(target=self._speak_test, daemon=True).start()

    def _speak_test(self) -> None:
        import asyncio
        import edge_tts
        try:
            out = b""
            async def run() -> None:
                nonlocal out
                c = edge_tts.Communicate(
                    f"This is {self.current_voice.split('-')[-1].replace('Neural','')}. Hello.",
                    self.current_voice,
                )
                async for chunk in c.stream():
                    if chunk["type"] == "audio":
                        out += chunk["data"]
            asyncio.run(run())
            if not out:
                return
            tmp = f"/tmp/edgetts-v2-test-{uuid.uuid4().hex[:6]}.mp3"
            with open(tmp, "wb") as f:
                f.write(out)
            subprocess.run(["afplay", tmp])
            os.unlink(tmp)
        except Exception as exc:
            print(f"[tts] {exc}", file=sys.stderr)

    def _quit(self, _sender) -> None:
        rumps.quit_application()

    # -- dictation flow ---------------------------------------------------

    def _on_fn_change(self, pressed: bool) -> None:
        # Called from the event-tap thread. Hop to main thread for UI.
        if pressed:
            callAfter(self._start_recording)
        else:
            callAfter(self._stop_recording)

    def _start_recording(self) -> None:
        with self._rec_lock:
            if self.recording:
                return
            try:
                self.recorder.start()
                self.recording = True
                self.title = ScribeApp.REC_TITLE
                hk_label = HOTKEYS[self.hotkey_id][2]
                self.status_item.title = f"Listening… release {hk_label} to paste"
            except Exception as exc:
                print(f"[rec start] {exc}", file=sys.stderr)
                rumps.notification(
                    "Microphone error",
                    "",
                    "Allow microphone access to Scribe in System Settings.",
                )

    def _stop_recording(self) -> None:
        with self._rec_lock:
            if not self.recording:
                return
            self.recording = False
        # Run transcription on a worker thread — we don't block the UI.
        self.title = ScribeApp.BUSY_TITLE
        self.status_item.title = "Transcribing…"
        threading.Thread(target=self._finalize, daemon=True).start()

    def _finalize(self) -> None:
        try:
            wav, dur_ms = self.recorder.stop()
        except Exception as exc:
            print(f"[rec stop] {exc}", file=sys.stderr)
            wav, dur_ms = b"", 0

        # Very short presses (<400ms) are almost certainly accidental.
        if not wav or dur_ms < 400:
            self._back_to_idle("Too short — ignored")
            return

        if not groq_api_key():
            self._back_to_idle("Set Groq API key in the menu")
            rumps.notification(
                "Groq key missing",
                "",
                "Open the menu → 'Set Groq API key…' to enable dictation.",
            )
            return

        lang = None if self._lang == "auto" else self._lang
        text = transcribe(wav, language=lang or "en")
        text = text.strip()

        if not text or is_garbage(text):
            self._back_to_idle("No speech detected")
            return

        try:
            paste_text(text)
        except Exception as exc:
            print(f"[paste] {exc}", file=sys.stderr)

        # History — always kept.
        try:
            append_history(text, dur_ms)
            callAfter(self._refresh_history_menu)
        except Exception as exc:
            print(f"[history] {exc}", file=sys.stderr)

        self._back_to_idle(text if len(text) <= 40 else text[:37] + "…")

    def _back_to_idle(self, status: str) -> None:
        def _ui() -> None:
            self.title = ScribeApp.IDLE_TITLE
            self.status_item.title = status
        callAfter(_ui)


# ---------- main ----------------------------------------------------------

def main() -> None:
    app = ScribeApp()
    app.run()


if __name__ == "__main__":
    main()
