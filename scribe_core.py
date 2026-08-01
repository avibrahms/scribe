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
import subprocess
import sys
import tempfile
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


SUPPORTED_STT_LANGS = {"en", "fr", "es", "de", "it", "auto"}


def load_stt_language(default: str = "en") -> str:
    cfg = load_cfg()
    lang = str(cfg.get("stt_language", "")).strip().lower()
    if lang in SUPPORTED_STT_LANGS:
        return lang
    return default


def save_stt_language(language: str) -> None:
    language = language.strip().lower()
    if language not in SUPPORTED_STT_LANGS:
        return
    cfg = load_cfg()
    cfg["stt_language"] = language
    save_cfg(cfg)


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


# ---------- TTS engine selection (Edge vs OpenAI) ------------------------
# Two backends, switchable from the menu, choice persisted across relaunch.
#   • "edge"   — free Microsoft Edge neural voices (no key, see VOICES)
#   • "openai" — gpt-4o-mini-tts, natural + steerable emotion (needs a key)

TTS_ENGINES = ("openai", "edge")
DEFAULT_TTS_ENGINE = "openai"

OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
DEFAULT_OPENAI_VOICE = "coral"
DEFAULT_TTS_INSTRUCTIONS = (
    "Perform this text aloud like an expressive human storyteller — never a "
    "narrator droning through a script. Constantly vary your pitch, pace, and "
    "volume: rise with excitement, slow down and lower your voice for emphasis "
    "or suspense, lift the ends of questions, and let energy build and release "
    "across sentences. Use clear emotional ups and downs that match the meaning "
    "of the words — warmth, curiosity, surprise, enthusiasm. Add natural human "
    "rhythm with small pauses for breath and thought. Absolutely avoid a flat, "
    "monotone, robotic, or detached delivery; every sentence should sound alive "
    "and genuinely felt."
)

# Curated gpt-4o-mini-tts voices (label, id). All support emotion steering.
OPENAI_VOICES: list[tuple[str, str]] = [
    ("Coral · warm, expressive",  "coral"),
    ("Nova · bright female",      "nova"),
    ("Shimmer · soft female",     "shimmer"),
    ("Sage · calm female",        "sage"),
    ("Alloy · neutral",           "alloy"),
    ("Ash · natural male",        "ash"),
    ("Ballad · gentle male",      "ballad"),
    ("Echo · clear male",         "echo"),
    ("Onyx · deep male",          "onyx"),
    ("Fable · storyteller",       "fable"),
]

# OpenAI models. Only gpt-4o-mini-tts honours `instructions` (emotion
# steering); tts-1 / tts-1-hd ignore it but all three honour `speed`.
DEFAULT_OPENAI_MODEL = OPENAI_TTS_MODEL  # "gpt-4o-mini-tts"
OPENAI_MODELS: list[tuple[str, str]] = [
    ("gpt-4o-mini-tts · steerable emotion", "gpt-4o-mini-tts"),
    ("tts-1 · fast, no emotion steering",   "tts-1"),
    ("tts-1-hd · higher fidelity",          "tts-1-hd"),
]
OPENAI_MODEL_IDS = {m for _, m in OPENAI_MODELS}

# Playback speed multiplier accepted by the speech endpoint.
DEFAULT_OPENAI_SPEED = 1.0
OPENAI_SPEED_MIN, OPENAI_SPEED_MAX = 0.25, 4.0

# Edge prosody (SSML-style strings edge-tts accepts verbatim).
DEFAULT_EDGE_RATE = "+0%"
DEFAULT_EDGE_PITCH = "+0Hz"
DEFAULT_EDGE_VOLUME = "+0%"


def load_tts_engine() -> str:
    cfg = load_cfg()
    eng = str(cfg.get("tts_engine", "")).strip().lower()
    return eng if eng in TTS_ENGINES else DEFAULT_TTS_ENGINE


def save_tts_engine(engine: str) -> None:
    engine = engine.strip().lower()
    if engine not in TTS_ENGINES:
        return
    cfg = load_cfg()
    cfg["tts_engine"] = engine
    save_cfg(cfg)
    _write_speak_settings(engine=engine)


def load_openai_voice() -> str:
    cfg = load_cfg()
    v = str(cfg.get("openai_voice", "")).strip()
    return v or DEFAULT_OPENAI_VOICE


def save_openai_voice(voice: str) -> None:
    voice = voice.strip()
    if not voice:
        return
    cfg = load_cfg()
    cfg["openai_voice"] = voice
    save_cfg(cfg)
    _write_speak_settings(openai_voice=voice)


def load_tts_instructions() -> str:
    cfg = load_cfg()
    v = str(cfg.get("openai_instructions", "")).strip()
    return v or DEFAULT_TTS_INSTRUCTIONS


def save_tts_instructions(text: str) -> None:
    text = text.strip() or DEFAULT_TTS_INSTRUCTIONS
    cfg = load_cfg()
    cfg["openai_instructions"] = text
    save_cfg(cfg)
    _write_speak_settings(openai_instructions=text)


def load_openai_model() -> str:
    m = str(load_cfg().get("openai_model", "")).strip()
    return m if m in OPENAI_MODEL_IDS else DEFAULT_OPENAI_MODEL


def save_openai_model(model: str) -> None:
    model = model.strip()
    if model not in OPENAI_MODEL_IDS:
        return
    cfg = load_cfg()
    cfg["openai_model"] = model
    save_cfg(cfg)
    _write_speak_settings(openai_model=model)


def load_openai_speed() -> float:
    try:
        s = float(load_cfg().get("openai_speed", DEFAULT_OPENAI_SPEED))
    except (TypeError, ValueError):
        return DEFAULT_OPENAI_SPEED
    return min(OPENAI_SPEED_MAX, max(OPENAI_SPEED_MIN, s))


def save_openai_speed(speed: float) -> None:
    try:
        s = min(OPENAI_SPEED_MAX, max(OPENAI_SPEED_MIN, float(speed)))
    except (TypeError, ValueError):
        return
    cfg = load_cfg()
    cfg["openai_speed"] = s
    save_cfg(cfg)
    _write_speak_settings(openai_speed=s)


# ---------- Edge prosody (rate / pitch / volume) -------------------------

def load_edge_rate() -> str:
    return str(load_cfg().get("edge_rate") or DEFAULT_EDGE_RATE)


def load_edge_pitch() -> str:
    return str(load_cfg().get("edge_pitch") or DEFAULT_EDGE_PITCH)


def load_edge_volume() -> str:
    return str(load_cfg().get("edge_volume") or DEFAULT_EDGE_VOLUME)


def save_edge_prosody(*, rate: str | None = None,
                      pitch: str | None = None,
                      volume: str | None = None) -> None:
    """Persist Edge prosody to cfg, the JSON mirror (primary source for the
    standalone helper) and the shell-sourceable config (external-hotkey parity)."""
    cfg = load_cfg()
    if rate is not None:
        cfg["edge_rate"] = rate
    if pitch is not None:
        cfg["edge_pitch"] = pitch
    if volume is not None:
        cfg["edge_volume"] = volume
    save_cfg(cfg)

    mirror = {}
    if rate is not None:
        mirror["rate"] = rate
    if pitch is not None:
        mirror["pitch"] = pitch
    if volume is not None:
        mirror["volume"] = volume
    if mirror:
        _write_speak_settings(**mirror)
    _update_shell_config(RATE=rate, PITCH=pitch, VOLUME=volume)


# ---------- per-language TTS voices --------------------------------------
# The language of the text being spoken is detected at speak time and used to
# look up a voice chosen for that language, per engine. Anything not mapped
# (or not confidently detected) falls back to the single default voice, which
# is exactly how Scribe behaved before this existed.

def _load_tts_lang():
    """Import the shared detector from bin/, or ~/bin where it is deployed.

    Loaded by path rather than as a package because the same file is imported
    by the two standalone helpers as a plain sibling module — one detector,
    three consumers, no duplicated word tables.
    """
    import importlib.util
    for cand in (APP_DIR / "bin" / "tts_lang.py", Path.home() / "bin" / "tts_lang.py"):
        try:
            if not cand.exists():
                continue
            spec = importlib.util.spec_from_file_location("tts_lang", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        except Exception as exc:
            print(f"[tts_lang] load failed from {cand}: {exc}", file=sys.stderr)
    return None


tts_lang = _load_tts_lang()

# Languages offered in the menus. Sourced from the detector so the two can
# never disagree about what "supported" means; empty if it failed to load,
# which degrades to the old single-voice behaviour instead of crashing.
TTS_LANGS: tuple[str, ...] = tuple(tts_lang.SUPPORTED_LANGS) if tts_lang else ()
TTS_LANG_LABELS: dict[str, str] = dict(tts_lang.LANG_LABELS) if tts_lang else {}

DEFAULT_TTS_AUTODETECT = True


def detect_tts_language(text: str) -> str | None:
    """Language code for `text`, or None when the detector is unsure/absent."""
    if not tts_lang:
        return None
    try:
        return tts_lang.detect_language(text)
    except Exception as exc:
        print(f"[tts_lang] detect failed: {exc}", file=sys.stderr)
        return None


def lang_of_voice(voice_id: str) -> str:
    """Language code an Edge voice belongs to ('fr-FR-HenriNeural' -> 'fr').

    Derived from the id instead of a hand-kept table, so the three English
    submenus (US/UK/AU) all correctly resolve to one 'en' slot.
    """
    return (voice_id or "").split("-")[0].lower()


def load_tts_autodetect() -> bool:
    val = load_cfg().get("tts_autodetect", DEFAULT_TTS_AUTODETECT)
    return bool(val)


def save_tts_autodetect(enabled: bool) -> None:
    cfg = load_cfg()
    cfg["tts_autodetect"] = bool(enabled)
    save_cfg(cfg)
    _write_speak_settings(tts_autodetect=bool(enabled))


def _load_voice_map(key: str) -> dict[str, str]:
    raw = load_cfg().get(key)
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): str(v) for k, v in raw.items()
        if k in TTS_LANGS and isinstance(v, str) and v.strip()
    }


def load_edge_voice_map() -> dict[str, str]:
    return _load_voice_map("edge_voice_by_lang")


def load_openai_voice_map() -> dict[str, str]:
    return _load_voice_map("openai_voice_by_lang")


def _save_voice_for_lang(key: str, lang: str, voice: str) -> dict[str, str]:
    if lang not in TTS_LANGS or not voice:
        return _load_voice_map(key)
    cfg = load_cfg()
    current = cfg.get(key)
    mapping = dict(current) if isinstance(current, dict) else {}
    mapping[lang] = voice
    cfg[key] = mapping
    save_cfg(cfg)
    # Mirror for the standalone helpers, which read settings.json only.
    _write_speak_settings(**{key: mapping})
    return mapping


def save_edge_voice_for_lang(lang: str, voice: str) -> dict[str, str]:
    return _save_voice_for_lang("edge_voice_by_lang", lang, voice)


def save_openai_voice_for_lang(lang: str, voice: str) -> dict[str, str]:
    return _save_voice_for_lang("openai_voice_by_lang", lang, voice)


def edge_lang_defaults() -> dict[str, str]:
    """{language: first Edge voice of that language}, derived from VOICES.

    The catalogue is the only place this is declared, so adding a language to
    VOICES gives it a sensible default automatically. Edge voices are
    locale-bound, so a language the user has not picked a voice for must still
    fall back to a voice that actually speaks it — not to whatever the global
    default happens to be.
    """
    out: dict[str, str] = {}
    for voices in VOICES.values():
        for _label, voice_id in voices:
            lang = lang_of_voice(voice_id)
            if lang in TTS_LANGS:
                out.setdefault(lang, voice_id)
    return out


def openai_lang_defaults() -> dict[str, str]:
    """{language: a distinct OpenAI voice}, dealt from OPENAI_VOICES in order.

    Unlike Edge, OpenAI's voices carry no locale — every one of them speaks
    every supported language — so there is no linguistically correct mapping
    to derive. What matters is that each language starts with its own stable,
    distinct, overridable default the menu can show. Dealing from the
    catalogue in order keeps even that derived: a language added to
    TTS_LANGS picks up the next voice with no table to update.
    """
    voices = [voice_id for _label, voice_id in OPENAI_VOICES]
    if not voices:
        return {}
    return {lang: voices[i % len(voices)] for i, lang in enumerate(TTS_LANGS)}


def lang_defaults_for(engine: str) -> dict[str, str]:
    """Per-language default voices for the engine."""
    return openai_lang_defaults() if engine == "openai" else edge_lang_defaults()


def effective_tts_voice(engine: str, lang: str | None) -> str:
    """Voice that `lang` will actually be spoken with, given current config.

    Shared by the resolver and the menu so a check mark can never disagree
    with what is about to come out of the speakers.
    """
    default = load_openai_voice() if engine == "openai" else load_voice()
    mapping = load_openai_voice_map() if engine == "openai" else load_edge_voice_map()
    if tts_lang:
        return tts_lang.voice_for_lang(mapping, lang, default, lang_defaults_for(engine))
    if not lang:
        return default
    return mapping.get(lang) or lang_defaults_for(engine).get(lang) or default


def sync_speak_settings() -> None:
    """Push the app's whole TTS state into the settings.json mirror.

    The app reads config.json; the standalone ⌃D/⌃X helpers read only
    settings.json. Any config written without going through a save_* helper —
    or any config predating a newly added key — leaves the hotkey resolving a
    different voice than the menu displays. Called at startup so the two
    stores cannot silently drift apart.
    """
    _write_speak_settings(
        engine=load_tts_engine(),
        voice=load_voice(),
        rate=load_edge_rate(),
        pitch=load_edge_pitch(),
        volume=load_edge_volume(),
        openai_voice=load_openai_voice(),
        openai_instructions=load_tts_instructions(),
        openai_model=load_openai_model(),
        openai_speed=load_openai_speed(),
        tts_autodetect=load_tts_autodetect(),
        edge_voice_by_lang=load_edge_voice_map(),
        openai_voice_by_lang=load_openai_voice_map(),
    )


def resolve_tts_voice(engine: str, text: str) -> tuple[str, str | None]:
    """(voice, detected_lang) for speaking `text` with `engine`.

    The single place the fallback chain lives: auto-detect off or a detector
    that is unsure end at the configured default; a detected language resolves
    through the user's pick, then the language's own default voice.
    """
    if not load_tts_autodetect():
        return effective_tts_voice(engine, None), None
    lang = detect_tts_language(text)
    return effective_tts_voice(engine, lang), lang


def _update_shell_config(**kv) -> None:
    """Merge KEY="value" pairs into the shell-sourceable VOICE_CONFIG_FILE,
    preserving any keys already there."""
    existing = {
        "VOICE": load_voice(),
        "RATE": DEFAULT_EDGE_RATE,
        "PITCH": DEFAULT_EDGE_PITCH,
        "VOLUME": DEFAULT_EDGE_VOLUME,
    }
    if VOICE_CONFIG_FILE.exists():
        try:
            existing.update(_parse_shell_kv(VOICE_CONFIG_FILE.read_text()))
        except Exception:
            pass
    for k, v in kv.items():
        if v is not None:
            existing[k] = v
    out = "\n".join(f'{k}="{v}"' for k, v in existing.items()) + "\n"
    try:
        VOICE_CONFIG_FILE.write_text(out)
    except Exception:
        pass


def _write_speak_settings(**fields) -> None:
    """Mirror OpenAI TTS prefs into the speak-selection settings.json so the
    standalone `openai-tts-stream` helper (and any external hotkey) sees them."""
    settings = {}
    if SETTINGS_FILE.exists():
        try:
            settings = json.loads(SETTINGS_FILE.read_text())
        except Exception:
            settings = {}
    # Ensure the helper always has sane values present.
    settings.setdefault("openai_voice", DEFAULT_OPENAI_VOICE)
    settings.setdefault("openai_instructions", DEFAULT_TTS_INSTRUCTIONS)
    settings.setdefault("openai_model", OPENAI_TTS_MODEL)
    # Always refreshed rather than defaulted: both are derived from the voice
    # catalogues, so they must follow VOICES / OPENAI_VOICES rather than
    # whatever was written to disk by an older build.
    settings["edge_lang_defaults"] = edge_lang_defaults()
    settings["openai_lang_defaults"] = openai_lang_defaults()
    for k, v in fields.items():
        if v is not None:
            settings[k] = v
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    except Exception:
        pass


def openai_api_key() -> str:
    k = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if k:
        return k
    # Standalone key file (0600), shared with the helper.
    try:
        kf = SPEAK_SELECTION_DIR / "openai_key"
        if kf.exists():
            return kf.read_text().strip()
    except Exception:
        pass
    cfg = load_cfg()
    return str(cfg.get("openai_api_key", "")).strip()


def save_openai_key(key: str) -> None:
    """Persist the OpenAI key OUT of the repo: the app's gitignored .env (so
    the running app inherits it via env) plus a 0600 key file for the helper."""
    key = key.strip()
    if not key:
        return
    # .env upsert (mirrors save_groq_key_to_dotenv).
    existing = DOTENV_FILE.read_text().splitlines() if DOTENV_FILE.exists() else []
    out, replaced = [], False
    for line in existing:
        if line.strip().startswith("OPENAI_API_KEY="):
            out.append(f"OPENAI_API_KEY={key}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"OPENAI_API_KEY={key}")
    DOTENV_FILE.write_text("\n".join(out) + "\n")
    try:
        os.chmod(DOTENV_FILE, 0o600)
    except Exception:
        pass
    # 0600 standalone key file.
    try:
        kf = SPEAK_SELECTION_DIR / "openai_key"
        kf.write_text(key)
        os.chmod(kf, 0o600)
    except Exception:
        pass
    # Make it live in this process immediately.
    os.environ["OPENAI_API_KEY"] = key


# ---------- audio recorder ------------------------------------------------

import threading  # noqa: E402  (grouped with Recorder which needs it)


# The audio capture subprocess — embedded as a string so there's no extra
# file to package / locate at runtime. This child owns PortAudio; when
# it hangs (Pa_StopStream / Pa_CloseStream wedging on macOS), we SIGKILL
# it and the kernel tears down its audio unit, instantly releasing the
# microphone (the "orange mic icon" stays on until the audio unit is
# released — killing the process IS the release).
_AUDIO_CHILD_SCRIPT = r"""
import os, sys, threading
import sounddevice as sd

SR = int(sys.argv[1])

_stream = None
_file = None
# Strong refs to stopped streams — prevents Python GC from calling
# __del__ → Pa_CloseStream, which hangs on macOS. We deliberately never
# close streams; the process exits and the kernel reclaims everything.
_leaked = []
_lock = threading.Lock()

def _cb(indata, frames, t, status):
    f = _file
    if f is not None:
        try:
            f.write(bytes(indata))
        except Exception:
            pass

def _do_start(path):
    global _stream, _file
    with _lock:
        if _stream is not None:
            # Previous recording never got a "stop". Roll it over.
            _stop_locked()
        try:
            _file = open(path, "wb")
            _stream = sd.InputStream(
                samplerate=SR, channels=1, dtype="int16",
                blocksize=0, callback=_cb,
            )
            _stream.start()
            return "ok"
        except Exception as exc:
            if _file is not None:
                try: _file.close()
                except Exception: pass
                _file = None
            _stream = None
            return "err start " + str(exc)[:160]

def _stop_locked():
    global _stream, _file
    if _stream is not None:
        s = _stream
        _stream = None
        try:
            s.stop()  # Never .close() — Pa_CloseStream hangs on macOS
        except Exception as exc:
            sys.stderr.write("[child] stop err: " + str(exc) + "\n")
            sys.stderr.flush()
        _leaked.append(s)
    if _file is not None:
        try:
            _file.flush()
            _file.close()
        except Exception: pass
        _file = None

def _do_stop():
    with _lock:
        _stop_locked()
        return "ok"

def _main():
    sys.stdout.write("ready\n")
    sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        cmd = parts[0]
        arg = parts[1] if len(parts) > 1 else ""
        if cmd == "start":
            resp = _do_start(arg)
        elif cmd == "stop":
            resp = _do_stop()
        elif cmd == "quit":
            _do_stop()
            sys.stdout.write("ok\n")
            sys.stdout.flush()
            break
        else:
            resp = "err unknown"
        sys.stdout.write(resp + "\n")
        sys.stdout.flush()

_main()
"""


def _child_python() -> str:
    """
    Path to a Python interpreter that has sounddevice available.

    Inside the Scribe.app bundle, sys.executable resolves to the shared
    Homebrew Python and does NOT pick up the venv's site-packages when
    spawned as a subprocess. Point directly at the venv binary — pyvenv.cfg
    next to it makes site-packages discoverable automatically.
    """
    venv_py = APP_DIR / "venv" / "bin" / "python"
    if venv_py.exists():
        return str(venv_py)
    return sys.executable


class _RecordService:
    """
    Long-lived audio-capture subprocess.

    All PortAudio/CoreAudio state lives in the child. On any hang
    (Pa_StopStream, Pa_CloseStream, Pa_OpenStream), the parent SIGKILLs
    the child — this ALWAYS succeeds and causes the kernel to tear down
    the audio unit, which is the only reliable way to make the orange
    mic indicator disappear and to release the device for the next
    recording. The next recording spawns a fresh child, so no zombie
    state ever leaks between recordings.
    """

    CMD_TIMEOUT = 2.0     # seconds: how long to wait for a "ok"/"err"
    KILL_WAIT = 2.0       # seconds: how long to wait for SIGKILL to reap

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------

    def _spawn(self) -> None:
        proc = subprocess.Popen(
            [_child_python(), "-u", "-c", _AUDIO_CHILD_SCRIPT,
             str(Recorder.SAMPLE_RATE)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        line = self._readline(proc, timeout=5.0)
        if line != "ready":
            err = b""
            try:
                if proc.stderr is not None:
                    err = proc.stderr.read(2048) or b""
            except Exception:
                pass
            try: proc.kill()
            except Exception: pass
            raise RuntimeError(
                f"audio service did not start (got {line!r}); "
                f"stderr: {err.decode(errors='replace')[:200]}"
            )
        self._proc = proc

    def _readline(self, proc: subprocess.Popen, timeout: float):
        """Read one line from proc.stdout with a hard timeout. None on timeout."""
        result: list[str | None] = [None]
        done = threading.Event()
        def _r() -> None:
            try:
                line = proc.stdout.readline() if proc.stdout else b""
                result[0] = line.decode(errors="replace").strip() if line else None
            except Exception:
                result[0] = None
            finally:
                done.set()
        threading.Thread(target=_r, daemon=True).start()
        done.wait(timeout)
        return result[0]

    def _send(self, cmd: str, timeout: float):
        if self._proc is None or self._proc.poll() is not None:
            self._spawn()
        try:
            assert self._proc is not None and self._proc.stdin is not None
            self._proc.stdin.write((cmd + "\n").encode())
            self._proc.stdin.flush()
        except Exception as exc:
            print(f"[rec svc] write failed: {exc}", file=sys.stderr)
            return None
        return self._readline(self._proc, timeout)

    def _kill(self) -> None:
        """SIGKILL the child. Cannot fail. Releases the mic at the kernel."""
        if self._proc is None:
            return
        try:
            self._proc.kill()
            try:
                self._proc.wait(timeout=self.KILL_WAIT)
            except Exception:
                pass
        except Exception:
            pass
        self._proc = None

    # -- public API ---------------------------------------------------

    def start(self, path: str) -> bool:
        """Begin capture to `path` (raw 16-bit mono PCM). Returns True on success."""
        with self._lock:
            resp = self._send(f"start {path}", timeout=self.CMD_TIMEOUT)
            if resp == "ok":
                return True
            # Service is sick — kill and retry once with a fresh child.
            print(f"[rec svc] start got {resp!r}; respawning", file=sys.stderr)
            self._kill()
            try:
                resp = self._send(f"start {path}", timeout=self.CMD_TIMEOUT)
            except Exception as exc:
                print(f"[rec svc] respawn start failed: {exc}", file=sys.stderr)
                return False
            return resp == "ok"

    def stop(self) -> bool:
        """
        Stop current capture. Returns True on a clean stop. On False the
        child was SIGKILLed (because it was hung inside PortAudio) —
        whatever was written to the file before that is still usable.
        """
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return True
            resp = self._send("stop", timeout=self.CMD_TIMEOUT)
            if resp == "ok":
                return True
            # The child is stuck in PortAudio. SIGKILL is the only thing
            # that will reliably free the mic and clear the orange icon.
            print(f"[rec svc] stop got {resp!r}; SIGKILL", file=sys.stderr)
            self._kill()
            return False

    def shutdown(self) -> None:
        with self._lock:
            if self._proc is None:
                return
            try:
                if self._proc.stdin is not None:
                    self._proc.stdin.write(b"quit\n")
                    self._proc.stdin.flush()
                try:
                    self._proc.wait(timeout=1.0)
                except Exception:
                    pass
            except Exception:
                pass
            self._kill()


# Module-level singleton. Spawned lazily on first recording.
_svc = _RecordService()


class Recorder:
    """
    Audio recorder with PortAudio running in a separate subprocess.

    Thin facade over _RecordService. Keeps the same .start() / .stop()
    API that scribe.py expects. The critical invariant: whether or not
    PortAudio hangs, stop() always returns in bounded time AND the
    microphone is always released by the end of stop() — via SIGKILL on
    the child when the graceful path times out. That is what makes the
    orange mic indicator actually disappear, and what lets the very next
    FN press start a new recording without a relaunch.
    """

    SAMPLE_RATE = 16000
    STOP_TIMEOUT = 3.0  # retained for API compatibility; unused in subprocess path

    def __init__(self) -> None:
        self._tmp_path: str | None = None
        self._started_at: float = 0.0
        # True when the service had to be SIGKILLed because it hung.
        # Whatever PCM made it to disk is still read and transcribed.
        self.orphaned: bool = False

    def start(self) -> None:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".pcm", prefix="scribe-rec-", delete=False,
        )
        self._tmp_path = tmp.name
        tmp.close()
        self._started_at = time.time()
        if not _svc.start(self._tmp_path):
            # Clean up the empty temp file so it doesn't leak.
            try: os.unlink(self._tmp_path)
            except Exception: pass
            self._tmp_path = None
            raise RuntimeError("audio service failed to start recording")

    def stop(self, timeout: float | None = None) -> tuple[bytes, int]:
        duration_ms = int((time.time() - self._started_at) * 1000)
        stopped_cleanly = _svc.stop()
        self.orphaned = not stopped_cleanly

        data = b""
        if self._tmp_path is not None:
            try:
                if os.path.exists(self._tmp_path):
                    with open(self._tmp_path, "rb") as f:
                        data = f.read()
            except Exception as exc:
                print(f"[rec] read error: {exc}", file=sys.stderr)
            try: os.unlink(self._tmp_path)
            except Exception: pass
            self._tmp_path = None

        if not data:
            return b"", duration_ms

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.SAMPLE_RATE)
            w.writeframes(data)
        return buf.getvalue(), duration_ms


# ---------- Whisper STT via Groq ------------------------------------------

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
STT_MODEL = "whisper-large-v3-turbo"


def transcribe(wav_bytes: bytes, language: str | None = "en") -> str:
    key = groq_api_key()
    if not key:
        return ""
    if not wav_bytes:
        return ""
    try:
        data = {
            "model": STT_MODEL,
            "response_format": "json",
            "temperature": "0",
        }
        # Omitting `language` lets Whisper auto-detect the spoken language.
        if language:
            data["language"] = language
        with httpx.Client(timeout=30.0) as c:
            r = c.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("rec.wav", wav_bytes, "audio/wav")},
                data=data,
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
    No-op kept for import compatibility.

    Previously the parent process owned PortAudio and this function
    cycled the library after a hang. Since Recorder now runs PortAudio
    in a subprocess, recovery is handled by SIGKILLing that subprocess —
    which is what _RecordService does on a stop-timeout. There's no
    parent-side PortAudio state to reset anymore.
    """
    return True
