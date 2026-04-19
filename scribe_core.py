#!/usr/bin/env python3
"""
scribe_core — platform-agnostic pieces of Scribe.

Both the macOS entry point (scribe.py) and the Windows entry point
(scribe_windows.py) import from here. This module MUST NOT import any
OS-specific GUI / global-hotkey / clipboard libraries. Only pure-Python
and cross-platform deps (sounddevice, httpx, numpy, edge-tts).

Everything that can be shared lives here:
  • paths + config   (platform-aware CONFIG_DIR)
  • .env loader
  • voice catalog (Microsoft Edge neural voices)
  • history (append / load / clear)
  • Groq Whisper transcription
  • garbage-detector for Whisper's silent-clip hallucinations
  • Recorder (sounddevice is cross-platform)

The per-OS files handle only the things that cannot be shared:
menubar/tray UI, global hotkey observation, and synthetic paste.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
import httpx


# ---------- paths / config ------------------------------------------------

APP_NAME = "scribe"
APP_DIR = Path(__file__).resolve().parent
DOTENV_FILE = APP_DIR / ".env"


def _config_dir() -> Path:
    """
    Per-OS application-data directory.
      • Windows  — %APPDATA%\\scribe
      • macOS    — ~/.config/scribe   (under .config for parity with the
                   shared "speak-selection" tool ecosystem)
      • Linux    — $XDG_CONFIG_HOME/scribe or ~/.config/scribe
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / ".config"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / APP_NAME


CONFIG_DIR = _config_dir()
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = CONFIG_DIR / "history.jsonl"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Shared voice config. On macOS this path is historically consumed by a
# separate "speak selection" hotkey tool, so we keep it at the well-known
# location. On other OSes there is no such ecosystem; we keep the voice
# config inside our own config dir.
if sys.platform == "darwin":
    SPEAK_SELECTION_DIR = Path.home() / ".config" / "speak-selection"
else:
    SPEAK_SELECTION_DIR = CONFIG_DIR / "speak-selection"
SPEAK_SELECTION_DIR.mkdir(parents=True, exist_ok=True)
VOICE_CONFIG_FILE = SPEAK_SELECTION_DIR / "config"
SETTINGS_FILE = SPEAK_SELECTION_DIR / "settings.json"


def load_dotenv(path: Path = DOTENV_FILE) -> None:
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


# ---------- voices --------------------------------------------------------
# A curated subset of Microsoft's Edge neural voices. All free, no key.
VOICES: dict[str, list[tuple[str, str]]] = {
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


# ---------- history -------------------------------------------------------

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


# ---------- config & API key ---------------------------------------------

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
        # Windows ignores POSIX perms; chmod can raise on some FSes.
        pass


# ---------- TTS voice config (shared with macOS speak-selection) ---------

def _parse_shell_kv(text: str) -> dict[str, str]:
    """Shell-style KEY=\"VALUE\" parser."""
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
    """Write shell-sourceable config alongside a JSON mirror."""
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

import threading  # noqa: E402  (grouped with Recorder which needs it)


class Recorder:
    """
    In-memory recorder using sounddevice. Mono, 16 kHz, int16.

    The audio callback appends int16 numpy chunks to self._chunks on a
    PortAudio worker thread. At stop() time we take those chunks and mux
    them into a WAV in-memory — this is the only thing the transcription
    stage cares about.

    Critical property: stop() always returns in bounded time (≤ timeout s),
    even if PortAudio's Pa_StopStream / Pa_CloseStream hangs — which it
    famously does on macOS when the input device is yanked, switched, or
    enters a bad state. When PortAudio hangs, we orphan the stream (its
    daemon close-thread keeps trying in the background) and build the WAV
    directly from the captured chunks. NO RECORDING IS EVER LOST to a
    driver bug — that was the previous failure mode where _finalize stuck
    on Pa_CloseStream and the transcript never made it to history.
    """

    SAMPLE_RATE = 16000
    STOP_TIMEOUT = 3.0  # seconds; above this we give up on Pa_CloseStream

    def __init__(self) -> None:
        self._chunks: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._started_at: float = 0.0
        # Set to True when Pa_CloseStream timed out and this recorder's
        # stream was orphaned. The caller checks this to know PortAudio
        # is in a bad state and needs a hard reset before the next
        # open_stream — otherwise the next Pa_OpenStream will hang too
        # because the device is still held by the zombie.
        self.orphaned: bool = False

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

    def stop(self, timeout: float | None = None) -> tuple[bytes, int]:
        """
        Returns (wav_bytes, duration_ms). Empty bytes if nothing captured.

        Runs the PortAudio close on a daemon thread with a hard timeout.
        If it doesn't return in time, the stream is orphaned and we build
        the WAV from whatever chunks the callback already delivered. The
        user's audio is preserved.
        """
        if timeout is None:
            timeout = self.STOP_TIMEOUT
        duration_ms = int((time.time() - self._started_at) * 1000)
        stream = self._stream
        self._stream = None

        if stream is not None:
            done = threading.Event()

            def _close() -> None:
                try:
                    stream.stop()
                    stream.close()
                except Exception as exc:
                    print(f"[rec] close error: {exc}", file=sys.stderr)
                finally:
                    done.set()

            threading.Thread(target=_close, daemon=True).start()
            if not done.wait(timeout):
                # PortAudio hung. Orphan the stream, keep moving.
                # Mark this recorder as orphaned so the caller knows
                # PortAudio needs a hard reset before the next open,
                # or the next Pa_OpenStream will also hang — that's
                # the bug that leaves the app stuck with the icon red
                # and no FN press doing anything.
                self.orphaned = True
                print(
                    "[rec] Pa_CloseStream timed out after "
                    f"{timeout}s — orphaning stream, salvaging audio.",
                    file=sys.stderr,
                )

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


# ---------- PortAudio hard reset ------------------------------------------

def reset_portaudio() -> bool:
    """
    Hard-reset the PortAudio library. Used after a Pa_CloseStream hang
    orphans a stream — without this, the next Pa_OpenStream hangs too
    because the previous (zombie) stream still holds the audio device,
    leaving the app wedged until relaunch. Returns True on success.

    sd._terminate/_initialize are private but are the documented way to
    cycle the library; calling them from outside a stream operation is
    safe. If the zombie thread is still literally inside a PortAudio
    call this may block too — we run it off the main thread so worst
    case we leak a thread but never freeze the UI.
    """
    try:
        sd._terminate()
        sd._initialize()
        return True
    except Exception as exc:
        print(f"[rec] PortAudio reset failed: {exc}", file=sys.stderr)
        return False
