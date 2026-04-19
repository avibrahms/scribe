#!/usr/bin/env python3
"""
scribe_windows — Windows system-tray entry point for Scribe.

Mirrors the macOS menubar app (scribe.py) but uses Windows-native libs:
  • pystray    — system tray icon + menu
  • pynput     — global hotkey observation + synthetic Ctrl+V
  • pyperclip  — clipboard read/write (text-only; complex formats are not
                 preserved during paste — unlike the macOS version which
                 snapshots every NSPasteboardItem)
  • tkinter    — modal dialogs for API-key entry + confirmations
  • playsound  — MP3 playback for the TTS Test Voice menu item

All shared logic (audio recording, Whisper transcription, history,
config, voice catalog) lives in scribe_core.py and is imported from
both this file and the macOS entry point.

Permissions:
  Windows has no TCC equivalent — no permission grants needed. However,
  some AV products (Defender, Kaspersky) may flag pynput as a keylogger
  the first time it runs. This is a false positive: pynput only observes
  keys and never exfiltrates. Whitelist Scribe if it gets blocked.

Run:
    python scribe_windows.py

Or launch via the generated Scribe.bat after `install.bat`.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pystray
from PIL import Image, ImageDraw
from pynput import keyboard
import pyperclip

from scribe_core import (
    APP_NAME,
    APP_DIR,
    DOTENV_FILE,
    HISTORY_FILE,
    VOICES,
    DEFAULT_VOICE,
    Recorder,
    load_dotenv,
    append_history,
    load_history,
    clear_history,
    load_cfg,
    save_cfg,
    groq_api_key,
    save_groq_key_to_dotenv,
    load_voice,
    save_voice,
    transcribe,
    is_garbage,
)


load_dotenv()


# ---------- icon drawing --------------------------------------------------

def _make_icon(state: str = "idle") -> Image.Image:
    """
    state: "idle" | "recording" | "busy".

    A simple 64×64 mic silhouette with a coloured status dot. Drawn at
    runtime so there are no binary assets to ship — easier for users to
    copy the repo around without losing files.
    """
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    body = (40, 40, 40, 255)
    d.rounded_rectangle((22, 8, 42, 36), radius=10, fill=body)
    d.line((32, 36, 32, 50), fill=body, width=3)
    d.line((18, 52, 46, 52), fill=body, width=3)
    if state == "recording":
        d.ellipse((44, 4, 60, 20), fill=(220, 50, 50, 255))
    elif state == "busy":
        d.ellipse((44, 4, 60, 20), fill=(50, 120, 220, 255))
    return img


# ---------- paste ---------------------------------------------------------

_kb_controller = keyboard.Controller()


def _post_ctrl_v() -> None:
    """Synthesize Ctrl+V so whatever has focus receives a paste."""
    _kb_controller.press(keyboard.Key.ctrl)
    _kb_controller.press("v")
    _kb_controller.release("v")
    _kb_controller.release(keyboard.Key.ctrl)


def paste_text(text: str) -> None:
    """
    Paste `text` into the focused field, best-effort preserving the
    previous clipboard text. Flow:

      1. Snapshot the current clipboard text.
      2. Put our transcript in the clipboard and synthesize Ctrl+V.
      3. Restore the previous text.

    Caveat vs. the macOS version: pyperclip is text-only. If the user had
    an image or files on the clipboard, only text is preserved. For a
    dictation workflow this is almost always fine.
    """
    try:
        saved = pyperclip.paste()
    except Exception:
        saved = ""
    try:
        pyperclip.copy(text)
        time.sleep(0.05)
        _post_ctrl_v()
        time.sleep(0.25)
    finally:
        try:
            pyperclip.copy(saved)
        except Exception as exc:
            print(f"[pasteboard] restore failed: {exc}", file=sys.stderr)


def copy_to_pasteboard(text: str) -> None:
    try:
        pyperclip.copy(text)
    except Exception as exc:
        print(f"[pasteboard] copy failed: {exc}", file=sys.stderr)


# ---------- hotkey --------------------------------------------------------

# Virtual-key tokens recognised by pynput. Left-vs-right distinguished.
HOTKEYS: dict[str, tuple[str, object]] = {
    "right_alt":   ("Right Alt",   keyboard.Key.alt_r),
    "left_alt":    ("Left Alt",    keyboard.Key.alt_l),
    "right_ctrl":  ("Right Ctrl",  keyboard.Key.ctrl_r),
    "left_ctrl":   ("Left Ctrl",   keyboard.Key.ctrl_l),
    "right_shift": ("Right Shift", keyboard.Key.shift_r),
    "right_win":   ("Right Win",   keyboard.Key.cmd_r),
}
DEFAULT_HOTKEY = "right_alt"


def load_hotkey_id() -> str:
    hk = (os.environ.get("SCRIBE_HOTKEY") or "").strip().lower()
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


class HotkeyWatcher:
    """
    pynput Listener on a background thread.

    Fires:
      • on edge-triggered press/release of the chosen push-to-talk modifier
      • on Ctrl+Alt+V keydown  (paste-last-transcript — mirror of macOS ⌃⌘V)

    Edge-triggering matters: Windows produces key-repeat `on_press` events
    while a modifier is held, so we dedupe with `_pressed`.
    """

    def __init__(self, hotkey_id: str, on_change, on_paste_last) -> None:
        self._on_change = on_change
        self._on_paste_last = on_paste_last
        self._ctrl = False
        self._alt = False
        self._pressed = False
        self._listener: keyboard.Listener | None = None
        self.set_hotkey(hotkey_id)

    def set_hotkey(self, hotkey_id: str) -> None:
        label, key = HOTKEYS.get(hotkey_id, HOTKEYS[DEFAULT_HOTKEY])
        self._label = label
        self._target = key
        self._pressed = False
        self.hotkey_id = hotkey_id

    @property
    def label(self) -> str:
        return self._label

    def start(self) -> None:
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.daemon = True
        self._listener.start()

    @staticmethod
    def _is_ctrl(key) -> bool:
        return key in (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r)

    @staticmethod
    def _is_alt(key) -> bool:
        return key in (
            keyboard.Key.alt,
            keyboard.Key.alt_l,
            keyboard.Key.alt_r,
            keyboard.Key.alt_gr,
        )

    @staticmethod
    def _is_v(key) -> bool:
        try:
            return bool(getattr(key, "char", None)) and key.char.lower() == "v"
        except Exception:
            return False

    def _on_press(self, key) -> None:
        try:
            if self._is_ctrl(key):
                self._ctrl = True
            if self._is_alt(key):
                self._alt = True
            if key == self._target and not self._pressed:
                self._pressed = True
                self._on_change(True)
            if self._ctrl and self._alt and self._is_v(key):
                self._on_paste_last()
        except Exception as exc:
            print(f"[hotkey] press: {exc}", file=sys.stderr)

    def _on_release(self, key) -> None:
        try:
            if self._is_ctrl(key):
                self._ctrl = False
            if self._is_alt(key):
                self._alt = False
            if key == self._target and self._pressed:
                self._pressed = False
                self._on_change(False)
        except Exception as exc:
            print(f"[hotkey] release: {exc}", file=sys.stderr)


# ---------- modal dialogs (tkinter) --------------------------------------

def _ask_api_key(current: str = "") -> str | None:
    import tkinter as tk
    from tkinter import simpledialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        val = simpledialog.askstring(
            "Scribe — Groq API key",
            "Paste your Groq API key (used for Whisper STT):",
            initialvalue=current,
            parent=root,
        )
    finally:
        root.destroy()
    return val


def _confirm_clear_history() -> bool:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        res = messagebox.askyesno(
            "Clear dictation history?",
            "This will delete every saved transcript.",
            parent=root,
        )
    finally:
        root.destroy()
    return bool(res)


# ---------- app -----------------------------------------------------------

class ScribeApp:
    def __init__(self) -> None:
        self.recorder = Recorder()
        self.recording = False
        # See scribe.py for the full rationale — the short version is that
        # Pa_OpenStream can block forever on a bad audio device, so we open
        # the stream on a worker thread and use these flags to coordinate
        # with a possible "released before the mic opened" race.
        self._opening = False
        self._pending_stop = False
        self._rec_lock = threading.Lock()
        self._lang = "en"

        self.current_voice = load_voice()
        self.hotkey_id = load_hotkey_id()

        self.icon = pystray.Icon(
            APP_NAME,
            icon=_make_icon("idle"),
            title=self._tooltip("Ready"),
            menu=self._build_menu(),
        )

        self.hotkey = HotkeyWatcher(
            self.hotkey_id,
            on_change=self._on_hotkey_change,
            on_paste_last=self._on_paste_last_shortcut,
        )
        self.hotkey.start()

    # -- helpers ----------------------------------------------------------

    def _tooltip(self, status: str) -> str:
        hk_label = HOTKEYS[self.hotkey_id][0]
        return f"Scribe — {status} — hold {hk_label}"

    def _set_state(self, state: str, status: str) -> None:
        """Update tray icon + tooltip. Safe to call from any thread."""
        try:
            self.icon.icon = _make_icon(state)
            self.icon.title = self._tooltip(status)
        except Exception:
            pass

    def _rebuild_menu(self) -> None:
        try:
            self.icon.menu = self._build_menu()
            self.icon.update_menu()
        except Exception:
            pass

    def _notify(self, title: str, msg: str) -> None:
        try:
            self.icon.notify(msg, title)
        except Exception:
            print(f"[notify] {title}: {msg}", file=sys.stderr)

    # -- menu -------------------------------------------------------------

    def _build_menu(self) -> pystray.Menu:
        # Voice submenu grouped by language.
        voice_groups = []
        for lang, voices in VOICES.items():
            voice_items = [
                pystray.MenuItem(
                    label,
                    self._make_voice_cb(voice_id),
                    checked=self._voice_checked(voice_id),
                    radio=True,
                )
                for (label, voice_id) in voices
            ]
            voice_groups.append(
                pystray.MenuItem(lang, pystray.Menu(*voice_items))
            )

        # Hotkey picker.
        hotkey_items = [
            pystray.MenuItem(
                label,
                self._make_hotkey_cb(hk_id),
                checked=self._hotkey_checked(hk_id),
                radio=True,
            )
            for hk_id, (label, _k) in HOTKEYS.items()
        ]

        # Language picker.
        lang_items = [
            pystray.MenuItem(
                label,
                self._make_lang_cb(code),
                checked=self._lang_checked(code),
                radio=True,
            )
            for (code, label) in [
                ("en", "English"),
                ("fr", "French"),
                ("es", "Spanish"),
                ("de", "German"),
                ("it", "Italian"),
                ("auto", "Auto-detect"),
            ]
        ]

        # History — snapshot of most recent transcripts. Rebuilt by
        # _rebuild_menu() each time a new transcript is appended.
        hist_items: list = []
        rows = load_history(limit=15)
        if not rows:
            hist_items.append(pystray.MenuItem(
                "(empty — nothing dictated yet)", None, enabled=False,
            ))
        else:
            for row in rows:
                text = row["text"]
                preview = text if len(text) <= 60 else text[:57] + "…"
                hist_items.append(pystray.MenuItem(
                    f"copy: {preview}", self._make_copy_cb(text),
                ))
        hist_items.append(pystray.Menu.SEPARATOR)
        hist_items.append(pystray.MenuItem(
            "Paste last  (Ctrl+Alt+V)", self._on_paste_last_menu,
        ))
        hist_items.append(pystray.MenuItem("Copy last", self._on_copy_last))
        hist_items.append(pystray.MenuItem(
            "Open history file", self._on_open_history_file,
        ))
        hist_items.append(pystray.MenuItem(
            "Clear history", self._on_clear_history,
        ))

        return pystray.Menu(
            pystray.MenuItem(
                f"Ready — hold {HOTKEYS[self.hotkey_id][0]} to dictate",
                None, enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("TTS Voice", pystray.Menu(*voice_groups)),
            pystray.MenuItem("Test Voice", self._on_test_voice),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Dictation Hotkey", pystray.Menu(*hotkey_items)),
            pystray.MenuItem("Dictation Language", pystray.Menu(*lang_items)),
            pystray.MenuItem("History", pystray.Menu(*hist_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Set Groq API key…", self._on_set_key),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit Scribe", self._on_quit),
        )

    def _voice_checked(self, voice_id: str):
        return lambda _item: self.current_voice == voice_id

    def _hotkey_checked(self, hotkey_id: str):
        return lambda _item: self.hotkey_id == hotkey_id

    def _lang_checked(self, code: str):
        return lambda _item: self._lang == code

    # -- callback factories ----------------------------------------------

    def _make_voice_cb(self, voice_id: str):
        def cb(_icon, _item):
            self.current_voice = voice_id
            save_voice(voice_id)
            self._rebuild_menu()
            self._notify("Voice set", voice_id)
        return cb

    def _make_hotkey_cb(self, hotkey_id: str):
        def cb(_icon, _item):
            self.hotkey_id = hotkey_id
            save_hotkey_id(hotkey_id)
            self.hotkey.set_hotkey(hotkey_id)
            self._set_state("idle", "Ready")
            self._rebuild_menu()
            self._notify(
                "Dictation hotkey",
                f"Hold {HOTKEYS[hotkey_id][0]} to record.",
            )
        return cb

    def _make_lang_cb(self, code: str):
        def cb(_icon, _item):
            self._lang = code
            self._rebuild_menu()
        return cb

    def _make_copy_cb(self, text: str):
        def cb(_icon, _item):
            copy_to_pasteboard(text)
            self._notify("Copied", text[:90])
        return cb

    # -- menu action handlers --------------------------------------------

    def _on_test_voice(self, _icon, _item) -> None:
        threading.Thread(target=self._speak_test, daemon=True).start()

    def _speak_test(self) -> None:
        try:
            import edge_tts
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
            tmp = Path(tempfile.gettempdir()) / f"scribe-tts-{uuid.uuid4().hex[:6]}.mp3"
            tmp.write_bytes(out)
            try:
                from playsound import playsound  # lazy import; optional
                playsound(str(tmp))
            except Exception as exc:
                # Fallback: hand the file to the system default player.
                print(f"[tts] playsound failed ({exc}); opening file", file=sys.stderr)
                try:
                    os.startfile(str(tmp))  # type: ignore[attr-defined]
                except Exception as exc2:
                    print(f"[tts] startfile failed: {exc2}", file=sys.stderr)
            finally:
                try:
                    tmp.unlink()
                except Exception:
                    pass
        except Exception as exc:
            print(f"[tts] {exc}", file=sys.stderr)

    def _on_copy_last(self, _icon, _item) -> None:
        rows = load_history(limit=1)
        if not rows:
            self._notify("History empty", "")
            return
        copy_to_pasteboard(rows[0]["text"])
        self._notify("Copied last", rows[0]["text"][:90])

    def _on_paste_last_menu(self, _icon, _item) -> None:
        rows = load_history(limit=1)
        if not rows:
            self._notify("Nothing to paste", "History is empty.")
            return
        copy_to_pasteboard(rows[0]["text"])
        self._notify("Copied to clipboard", "Ctrl+V to paste it wherever.")

    def _on_paste_last_shortcut(self) -> None:
        """Ctrl+Alt+V — paste last transcript into focused field."""
        rows = load_history(limit=1)
        if not rows:
            self._notify("Nothing to paste", "History is empty.")
            return
        # Small delay so the user's modifier-release is processed before we
        # synthesize Ctrl+V — otherwise the held Alt bleeds through.
        time.sleep(0.08)
        paste_text(rows[0]["text"])

    def _on_open_history_file(self, _icon, _item) -> None:
        if not HISTORY_FILE.exists():
            HISTORY_FILE.touch()
        try:
            os.startfile(str(HISTORY_FILE))  # type: ignore[attr-defined]
        except Exception as exc:
            print(f"[history open] {exc}", file=sys.stderr)

    def _on_clear_history(self, _icon, _item) -> None:
        # tkinter dialog must run on its own main loop; spawn briefly.
        def _ask_and_clear() -> None:
            if _confirm_clear_history():
                clear_history()
                self._rebuild_menu()
        threading.Thread(target=_ask_and_clear, daemon=True).start()

    def _on_set_key(self, _icon, _item) -> None:
        def _ask() -> None:
            val = _ask_api_key(groq_api_key())
            if val is None:
                return
            val = val.strip()
            if not val:
                return
            save_groq_key_to_dotenv(val)
            os.environ["GROQ_API_KEY"] = val
            self._notify("Groq key saved", f"Written to {DOTENV_FILE}")
        threading.Thread(target=_ask, daemon=True).start()

    def _on_quit(self, _icon, _item) -> None:
        try:
            self.icon.stop()
        except Exception:
            pass
        # Hard-exit — pystray's stop() returns but the hotkey listener
        # thread may still be blocked inside WinAPI. os._exit is fine here
        # because all important state is already flushed to disk.
        os._exit(0)

    # -- dictation flow --------------------------------------------------

    def _on_hotkey_change(self, pressed: bool) -> None:
        if pressed:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self) -> None:
        with self._rec_lock:
            if self.recording or self._opening:
                return
            self._opening = True
            self._pending_stop = False
        self._set_state("recording", "Listening…")
        threading.Thread(target=self._open_stream_worker, daemon=True).start()

    def _open_stream_worker(self) -> None:
        """
        Opens the audio stream off the main thread. Same reasoning as the
        macOS fix: PortAudio's Pa_OpenStream can block indefinitely if the
        input device is in a bad state. A 5s watchdog surfaces a notification
        so the user knows it's a device issue, not an app freeze.
        """
        opened = threading.Event()

        def _watchdog() -> None:
            if not opened.wait(5.0):
                self._notify(
                    "Scribe: mic not responding",
                    "Try switching input device in Sound Settings.",
                )
        threading.Thread(target=_watchdog, daemon=True).start()

        err: Exception | None = None
        try:
            self.recorder.start()
        except Exception as exc:
            err = exc
            print(f"[rec start] {exc}", file=sys.stderr)
        opened.set()

        with self._rec_lock:
            self._opening = False
            if err is not None:
                self._pending_stop = False
                self._notify("Microphone error", str(err)[:180])
                self._set_state("idle", "Mic error")
                return
            if self._pending_stop:
                self._pending_stop = False
                threading.Thread(target=self._discard_stream, daemon=True).start()
                self._set_state("idle", "Too short — ignored")
                return
            self.recording = True

    def _discard_stream(self) -> None:
        try:
            self.recorder.stop()
        except Exception as exc:
            print(f"[rec discard] {exc}", file=sys.stderr)

    def _stop_recording(self) -> None:
        with self._rec_lock:
            if self._opening:
                self._pending_stop = True
                return
            if not self.recording:
                return
            self.recording = False
        self._set_state("busy", "Transcribing…")
        threading.Thread(target=self._finalize, daemon=True).start()

    def _finalize(self) -> None:
        try:
            wav, dur_ms = self.recorder.stop()
        except Exception as exc:
            print(f"[rec stop] {exc}", file=sys.stderr)
            wav, dur_ms = b"", 0

        if not wav or dur_ms < 400:
            self._set_state("idle", "Too short — ignored")
            return

        if not groq_api_key():
            self._set_state("idle", "Set Groq API key in menu")
            self._notify(
                "Groq key missing",
                "Open the tray menu → 'Set Groq API key…' to enable dictation.",
            )
            return

        lang = None if self._lang == "auto" else self._lang
        text = transcribe(wav, language=lang or "en")
        text = text.strip()

        if not text or is_garbage(text):
            self._set_state("idle", "No speech detected")
            return

        try:
            paste_text(text)
        except Exception as exc:
            print(f"[paste] {exc}", file=sys.stderr)

        try:
            append_history(text, dur_ms)
            self._rebuild_menu()
        except Exception as exc:
            print(f"[history] {exc}", file=sys.stderr)

        self._set_state(
            "idle",
            text if len(text) <= 40 else text[:37] + "…",
        )

    # -- entry ------------------------------------------------------------

    def run(self) -> None:
        # pystray.Icon.run() blocks the main thread and returns on stop().
        self.icon.run()


def main() -> None:
    ScribeApp().run()


if __name__ == "__main__":
    main()
