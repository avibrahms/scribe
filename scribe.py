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

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import rumps

from scribe_core import (
    APP_NAME,
    APP_DIR,
    DOTENV_FILE,
    CONFIG_DIR,
    HISTORY_FILE,
    CONFIG_FILE,
    SPEAK_SELECTION_DIR,
    VOICE_CONFIG_FILE,
    SETTINGS_FILE,
    VOICES,
    DEFAULT_VOICE,
    Recorder,
    load_dotenv as _load_dotenv,
    append_history,
    load_history,
    clear_history,
    load_cfg,
    save_cfg,
    groq_api_key,
    save_groq_key_to_dotenv,
    load_stt_language,
    load_voice,
    save_stt_language,
    save_voice,
    transcribe,
    is_garbage,
    reset_portaudio,
)

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
    kCGEventTapDisabledByTimeout,
    kCGEventTapDisabledByUserInput,
    kCGHIDEventTap,
    kCGHeadInsertEventTap,
    kCGAnnotatedSessionEventTap,
    kCGSessionEventTap,
    kCGKeyboardEventKeycode,
)


# Load .env from the app directory so GROQ_API_KEY is available before any
# call into scribe_core.groq_api_key().
_load_dotenv(DOTENV_FILE)


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


# Characters that mean "no leading space needed" when they are the last
# char of the previous Scribe paste. Whitespace (already spaced), openers
# and opening quotes ("hello (" → no space before "world"), hyphens inside
# words (well-known), and slash/at/hash for URLs and handles.
_NO_SPACE_BEFORE = frozenset(
    " \t\n\r\xa0\u2028\u2029"  # all flavors of whitespace
    "([{<"                     # openers
    "\"'`"                     # straight quotes
    "\u201c\u2018\u00ab"       # opening curly/guillemet quotes
    "-\u2013\u2014"            # hyphen, en-dash, em-dash
    "/@#"                      # URL / handle / hashtag
)


# ---------- voice-token expansion -----------------------------------------
#
# Spoken phrase → literal character. Only "joiner" style symbols — ones
# that naturally live between two words with no surrounding spaces
# (foo/bar, foo-bar, foo_bar, @handle). Punctuation that hugs one side
# (periods, question marks, parentheses) isn't covered here because
# Whisper already emits those correctly from prosody.
#
# Longer phrases must come before shorter ones that share a suffix so
# "forward slash" is consumed before "slash" gets a chance to match.
#
# Known trade-off: if the user genuinely means the WORD "slash"/"dash" in
# a sentence, it'll still get converted. For hands-free coding/URL/email
# dictation the ergonomic win is worth the rare false positive; the user
# can always edit.
_VOICE_JOINERS: list[tuple[str, str]] = [
    # Two-word phrases first (longest-match ordering).
    ("forward slash", "/"),
    ("back slash",    "\\"),
    ("under score",   "_"),
    ("at sign",       "@"),
    ("hash sign",     "#"),
    ("pound sign",    "#"),
    ("plus sign",     "+"),
    ("equals sign",   "="),
    ("equal sign",    "="),
    ("percent sign",  "%"),
    ("dollar sign",   "$"),
    ("vertical bar",  "|"),
    # Single-word tokens.
    ("hashtag",       "#"),
    ("asterisk",      "*"),
    ("backslash",     "\\"),
    ("underscore",    "_"),
    ("ampersand",     "&"),
    ("slash",         "/"),
    ("hyphen",        "-"),
    ("dash",          "-"),
    ("tilde",         "~"),
    ("caret",         "^"),
    ("pipe",          "|"),
]

# Precompiled patterns: `\s*\b…\b\s*` so surrounding whitespace is
# consumed ("foo slash bar" → "foo/bar"), word boundaries so substrings
# inside real words are left alone ("slashed", "dashboard").
_VOICE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\s*\b" + re.escape(phrase) + r"\b\s*", re.IGNORECASE), char)
    for phrase, char in _VOICE_JOINERS
]


def _apply_voice_tokens(text: str) -> str:
    """
    Replace spoken joiner tokens with their literal characters, eating
    surrounding whitespace so the output reads as natural inline
    punctuation.

        "hello slash world"     → "hello/world"
        "foo dash bar"          → "foo-bar"
        "my path underscore x"  → "my path_x"
        "ship it hashtag launch"→ "ship it#launch"

    Applied in insertion order — longer phrases first — so "forward
    slash" wins over "slash", etc.
    """
    original = text
    for pattern, char in _VOICE_PATTERNS:
        # Replacement goes through a lambda so backreferences like `\1`
        # and a literal `\` in `char` aren't interpreted by re.sub.
        text = pattern.sub(lambda _m, c=char: c, text)
    if text != original:
        print(f"[voice-tokens] {original!r} → {text!r}", file=sys.stderr)
    return text


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
                 on_tap_silent=None, on_tap_reenabled=None) -> None:
        self._on_change = on_change
        self._on_paste_last = on_paste_last
        # Called on the main thread if the tap goes >N seconds without
        # seeing a single event (strong signal Input Monitoring is not
        # granted — CGEventTapCreate returns a handle but delivers nothing).
        self._on_tap_silent = on_tap_silent
        # Fired from the tap thread when macOS had disabled our tap and we
        # just re-armed it. Any push-to-talk recording in flight almost
        # certainly lost its key-release while the tap was dead, so the app
        # should recover to a clean idle state.
        self._on_tap_reenabled = on_tap_reenabled
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
            # macOS can disable the tap without tearing it down — typically
            # on callback timeout, heavy user input, sleep/wake, or fast
            # user switch. If we don't re-arm it here, NO further events
            # arrive and the hotkey stays silently dead until the app is
            # relaunched. This is the single biggest cause of "icon stuck
            # red, FN does nothing" in the wild.
            if event_type == kCGEventTapDisabledByTimeout or \
               event_type == kCGEventTapDisabledByUserInput:
                if self._tap is not None:
                    CGEventTapEnable(self._tap, True)
                was_pressed = self._pressed
                # The release event for any in-flight press was almost
                # certainly dropped while the tap was dead — forget the
                # press state so the next real press registers cleanly.
                self._pressed = False
                if was_pressed and self._on_tap_reenabled is not None:
                    try:
                        self._on_tap_reenabled()
                    except Exception as exc:
                        print(f"[hotkey] reenable cb: {exc}",
                              file=sys.stderr)
                return event

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

        # One Recorder instance per recording — prevents a hung
        # Pa_CloseStream on the previous clip from contaminating the next
        # one. The "active" one lives here between _start_recording and
        # _finalize; it is None the rest of the time.
        self._active_recorder: Recorder | None = None
        self.recording = False
        # _opening: a background thread is inside sd.InputStream(...).start(),
        # which calls PortAudio's Pa_OpenStream. That call can block forever
        # on macOS if the audio device was just yanked (AirPods/USB mic swap)
        # or CoreAudio is in a bad state. We MUST NOT run it on the main
        # thread — doing so freezes the whole menubar UI with no way out.
        # _pending_stop: user released the hotkey before the stream finished
        # opening. When the open completes we should immediately tear it down
        # instead of recording.
        self._opening = False
        self._pending_stop = False
        # True when the previous recording's Pa_CloseStream timed out and
        # left PortAudio holding the audio device. If we don't reset the
        # library before the next Pa_OpenStream, that call will hang too
        # — the exact failure that leaves self._opening stuck True and
        # the FN key doing nothing until the app is relaunched.
        self._pa_needs_reset = False
        self._rec_lock = threading.Lock()
        self._lang = load_stt_language("en")

        # Back-to-back dictation space guard. We can't read the focused
        # field's caret without diving into the Accessibility API (and
        # AX reads are unreliable in Electron, Chrome, sandboxed apps).
        # So we track what Scribe itself last pasted: if the next paste
        # happens soon, into the same frontmost app, and the previous
        # paste ended on a non-space/non-opener char, we prepend a space.
        # Misses the "user typed/clicked between dictations" case, but
        # never inserts a wrong space when the user is clearly elsewhere.
        self._last_paste_tail: str | None = None
        self._last_paste_bundle: str | None = None
        self._last_paste_ts: float = 0.0

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
            on_tap_reenabled=self._on_tap_reenabled,
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
            save_stt_language(code)
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
        field while preserving the user's clipboard.

        Called from the event-tap callback thread. paste_text() sleeps
        ~280ms in total (clipboard settle + post-paste wait) — long
        enough to risk tripping macOS's callback-timeout watchdog, which
        would disable the tap and kill the hotkey until relaunch. So we
        dispatch the actual work to a background thread and return from
        the callback immediately.
        """
        threading.Thread(target=self._do_paste_last, daemon=True).start()

    def _do_paste_last(self) -> None:
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

    def _on_tap_reenabled(self) -> None:
        """
        Fired from the tap thread after macOS disabled our event tap
        (timeout / sleep-wake / fast user switch) and we re-armed it.

        If a push-to-talk recording was in flight, its key-release was
        dropped while the tap was dead — without recovery, self.recording
        stays True forever, the icon stays 🔴, and subsequent presses hit
        the `if self.recording` early-return in _start_recording. Tear
        the in-flight recording down and return to idle.
        """
        callAfter(self._force_cancel_recording)

    def _force_cancel_recording(self) -> None:
        """
        Discard any in-flight recording and reset the UI. Used when we
        know we've lost the key-release event. Must run on the main
        thread (touches rumps UI state).
        """
        rec: Recorder | None = None
        with self._rec_lock:
            if self._opening:
                # Stream hasn't finished opening — let the opener thread
                # tear it down when it completes, same path as a rapid
                # press+release.
                self._pending_stop = True
                return
            if not self.recording:
                return
            self.recording = False
            rec = self._active_recorder
            self._active_recorder = None
        if rec is not None:
            threading.Thread(
                target=self._discard_stream, args=(rec,), daemon=True,
            ).start()
        self._back_to_idle("Recovered — hotkey ready")

    def _on_tap_silent(self) -> None:
        """
        Called from the tap thread after 20s of silence.

        Two things can cause 20s of silence:
          (a) Input Monitoring is genuinely not granted — the tap returns
              a handle but delivers zero events.
          (b) The user simply hasn't touched the keyboard since Scribe
              started. This is the norm at login: the LaunchAgent
              auto-starts Scribe and the user's hands are off the keys
              for the first minute while the desktop settles. This used
              to pop open System Settings on every boot — false positive.

        Before hijacking focus, confirm permission is actually denied
        via IOHIDCheckAccess. If granted, stay silent: the tap will
        wake up the instant the user types.
        """
        # Guard against the cold-boot false positive.
        # kIOHIDRequestTypeListenEvent = 1, kIOHIDAccessTypeGranted = 0.
        try:
            from Quartz import (
                IOHIDCheckAccess,
                kIOHIDRequestTypeListenEvent,
            )
            if IOHIDCheckAccess(kIOHIDRequestTypeListenEvent) == 0:
                return
        except Exception:
            # If the API isn't importable on this macOS/pyobjc version,
            # fall back to just flagging it in the menubar — still
            # don't auto-open Settings, since the false positive is
            # worse than a missed warning (the user can see the ⚠
            # title and click the permissions menu manually).
            def _flag_only() -> None:
                self.status_item.title = "⚠ Grant Input Monitoring to Scribe"
            callAfter(_flag_only)
            return

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
        # Main thread. Must return FAST — never call Pa_OpenStream here.
        with self._rec_lock:
            if self.recording or self._opening:
                return
            self._opening = True
            self._pending_stop = False
            # Fresh Recorder per recording: an old hung stop() from the
            # previous clip cannot race with this one.
            rec = Recorder()
            self._active_recorder = rec
        self.title = ScribeApp.REC_TITLE
        hk_label = HOTKEYS[self.hotkey_id][2]
        self.status_item.title = f"Listening… release {hk_label} to paste"
        threading.Thread(
            target=self._open_stream_worker, args=(rec,), daemon=True,
        ).start()

    def _open_stream_worker(self, rec: Recorder) -> None:
        """
        Opens the audio stream off the main thread. Pa_OpenStream can hang
        indefinitely when PortAudio is in a bad state — especially after
        a previous Pa_CloseStream orphaned its stream. Two defenses:

        1. If the previous recording orphaned its close, hard-reset
           PortAudio before trying to open. This releases the device
           that the zombie stream was still holding.
        2. A real watchdog: if opening doesn't complete in time, we
           give up, reset app state (self._opening, self._active_recorder)
           so the NEXT FN press is accepted, and let the zombie open
           finish eventually in the background. Without this reset, the
           user has to relaunch the whole app.
        """
        OPEN_TIMEOUT = 5.0

        # Defense 1: recover from a previous orphaned close.
        if self._pa_needs_reset:
            self._pa_needs_reset = False
            print("[rec] resetting PortAudio before open", file=sys.stderr)
            reset_portaudio()

        opened = threading.Event()
        aborted = threading.Event()

        def _watchdog() -> None:
            if opened.wait(OPEN_TIMEOUT):
                return
            # Pa_OpenStream is hung. Give up on this attempt so the app
            # doesn't stay stuck. The zombie thread may complete later;
            # we handle that race below.
            aborted.set()
            with self._rec_lock:
                if self._active_recorder is rec:
                    self._opening = False
                    self._pending_stop = False
                    self._active_recorder = None
                    # PortAudio is clearly wedged — force a reset before
                    # the user's next attempt.
                    self._pa_needs_reset = True
            callAfter(lambda: rumps.notification(
                "Scribe: mic stuck",
                "Audio device didn't respond",
                "Recording aborted. Try again — if it keeps failing, "
                "switch your input device in System Settings → Sound.",
            ))
            callAfter(lambda: self._back_to_idle("Mic stuck — try again"))
        threading.Thread(target=_watchdog, daemon=True).start()

        err: Exception | None = None
        try:
            rec.start()
        except Exception as exc:
            err = exc
            print(f"[rec start] {exc}", file=sys.stderr)
        opened.set()

        with self._rec_lock:
            # Race: the watchdog fired already and moved on. We are now
            # a zombie — discard whatever stream we just opened (may
            # hang its own close, but that's off the hot path).
            if aborted.is_set() or self._active_recorder is not rec:
                threading.Thread(
                    target=self._discard_stream, args=(rec,), daemon=True,
                ).start()
                return
            self._opening = False
            if err is not None:
                self._pending_stop = False
                self._active_recorder = None
                callAfter(lambda: rumps.notification(
                    "Microphone error", "", str(err)[:180] or
                    "Allow microphone access to Scribe in System Settings.",
                ))
                callAfter(lambda: self._back_to_idle("Mic error"))
                return
            if self._pending_stop:
                # User let go of the hotkey before the stream finished
                # opening — tear it down silently and bail.
                self._pending_stop = False
                self._active_recorder = None
                threading.Thread(
                    target=self._discard_stream, args=(rec,), daemon=True,
                ).start()
                callAfter(lambda: self._back_to_idle("Too short — ignored"))
                return
            self.recording = True

    def _discard_stream(self, rec: Recorder) -> None:
        """Close a stream we no longer want, without transcribing."""
        try:
            rec.stop()
        except Exception as exc:
            print(f"[rec discard] {exc}", file=sys.stderr)
        # If the close got orphaned, the audio device is still held by
        # the zombie stream — flag PortAudio for reset before the next
        # open so Pa_OpenStream doesn't hang.
        if getattr(rec, "orphaned", False):
            with self._rec_lock:
                self._pa_needs_reset = True

    def _stop_recording(self) -> None:
        with self._rec_lock:
            if self._opening:
                # Stream hasn't finished opening yet. Mark for cancellation;
                # the opener thread will tear it down when it completes.
                self._pending_stop = True
                return
            if not self.recording:
                return
            self.recording = False
            rec = self._active_recorder
            self._active_recorder = None
        if rec is None:
            # Shouldn't happen, but guard anyway.
            self._back_to_idle("Idle")
            return
        # Run transcription on a worker thread — we don't block the UI.
        self.title = ScribeApp.BUSY_TITLE
        self.status_item.title = "Transcribing…"
        threading.Thread(
            target=self._finalize, args=(rec,), daemon=True,
        ).start()

    def _finalize(self, rec: Recorder) -> None:
        """
        Close the stream, transcribe, save to history, paste.

        CRITICAL: this whole method runs in a try/finally so _back_to_idle
        ALWAYS fires — the menubar can never get stuck on "Transcribing…".
        History is saved BEFORE paste so a paste crash can never lose a
        transcript. rec.stop() has a hard timeout inside scribe_core so
        a hung Pa_CloseStream never blocks this thread forever.
        """
        status = "Ready"
        try:
            try:
                wav, dur_ms = rec.stop()
            except Exception as exc:
                print(f"[rec stop] {exc}", file=sys.stderr)
                wav, dur_ms = b"", 0
            # If Pa_CloseStream hung and this stream is now a zombie
            # holding the audio device, flag PortAudio for reset before
            # the next open. Without this, the NEXT FN press hangs in
            # Pa_OpenStream and stays wedged forever.
            if rec.orphaned:
                with self._rec_lock:
                    self._pa_needs_reset = True

            # Very short presses (<400ms) are almost certainly accidental.
            if not wav or dur_ms < 400:
                status = "Too short — ignored"
                return

            if not groq_api_key():
                status = "Set Groq API key in the menu"
                rumps.notification(
                    "Groq key missing", "",
                    "Open the menu → 'Set Groq API key…' to enable dictation.",
                )
                return

            lang = None if self._lang == "auto" else self._lang
            text = transcribe(wav, language=lang).strip()
            # Turn spoken "slash/dash/underscore/etc." into literal chars
            # before history, space-guard, and paste all see the same text.
            text = _apply_voice_tokens(text)

            if not text or is_garbage(text):
                status = "No speech detected"
                return

            # Save to history FIRST. If paste crashes, the transcript is
            # still safe and accessible from the History menu / ⌃⌘V.
            try:
                append_history(text, dur_ms)
                callAfter(self._refresh_history_menu)
            except Exception as exc:
                print(f"[history] {exc}", file=sys.stderr)

            # Prepend a space when the previous dictation landed in the
            # same app recently and didn't end on whitespace/opener. Only
            # the pasted string gets the prefix — history keeps the clean
            # transcript. Read the bundle BEFORE pasting so focus-stealing
            # side-effects of ⌘V can't confuse the check.
            bundle = self._frontmost_bundle()
            try:
                to_paste = self._maybe_prepend_space(text, bundle)
            except Exception as exc:
                print(f"[space-guard] {exc}", file=sys.stderr)
                to_paste = text

            try:
                paste_text(to_paste)
                self._record_paste(to_paste, bundle)
            except Exception as exc:
                print(f"[paste] {exc}", file=sys.stderr)

            status = text if len(text) <= 40 else text[:37] + "…"
        except Exception as exc:
            # Any uncaught error (network, JSON, unexpected) lands here and
            # still reaches the finally below, so the UI always resets.
            print(f"[finalize] unexpected: {exc}", file=sys.stderr)
            status = "Error — see log"
        finally:
            self._back_to_idle(status)

    # -- back-to-back space guard ----------------------------------------

    _SPACE_GUARD_WINDOW = 60.0  # seconds

    def _frontmost_bundle(self) -> str | None:
        """Bundle identifier of the app currently in the foreground, or
        None if we can't determine it. Read-only NSWorkspace query — safe
        to call from any thread."""
        try:
            from AppKit import NSWorkspace
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app is None:
                return None
            bid = app.bundleIdentifier()
            return str(bid) if bid is not None else None
        except Exception:
            return None

    def _maybe_prepend_space(self, text: str, bundle: str | None) -> str:
        """
        Decide whether to prepend a space to `text` so back-to-back
        dictations don't smash together. Returns the text to actually
        paste. `bundle` is the frontmost app bundle read at call time
        (passed in rather than re-read so the decision and the tracker
        update agree on "which app").
        """
        if not text or text[0].isspace():
            print("[space-guard] skip: transcript already starts with "
                  "whitespace", file=sys.stderr)
            return text
        if self._last_paste_tail is None:
            print("[space-guard] skip: no previous paste recorded",
                  file=sys.stderr)
            return text
        if self._last_paste_tail in _NO_SPACE_BEFORE:
            print(f"[space-guard] skip: prev tail {self._last_paste_tail!r} "
                  f"is in no-space set", file=sys.stderr)
            return text
        age = time.monotonic() - self._last_paste_ts
        if age > self._SPACE_GUARD_WINDOW:
            print(f"[space-guard] skip: prev paste was {age:.1f}s ago "
                  f"(> {self._SPACE_GUARD_WINDOW}s)", file=sys.stderr)
            return text
        if bundle is None or bundle != self._last_paste_bundle:
            print(f"[space-guard] skip: bundle mismatch "
                  f"(now={bundle!r}, prev={self._last_paste_bundle!r})",
                  file=sys.stderr)
            return text
        print(f"[space-guard] PREPEND: prev tail {self._last_paste_tail!r}, "
              f"bundle {bundle!r}, age {age:.1f}s", file=sys.stderr)
        return " " + text

    def _record_paste(self, pasted: str, bundle: str | None) -> None:
        """Remember what we just pasted so the next dictation can decide
        whether to lead with a space."""
        self._last_paste_tail = pasted[-1] if pasted else None
        self._last_paste_bundle = bundle
        self._last_paste_ts = time.monotonic()
        print(f"[space-guard] recorded: tail={self._last_paste_tail!r}, "
              f"bundle={bundle!r}", file=sys.stderr)

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
