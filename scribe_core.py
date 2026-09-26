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
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_history(limit: int = 50) -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    rows: list[dict] = []
    with HISTORY_FILE.open("r", encoding="utf-8") as f:
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

import collections  # noqa: E402  (grouped with Recorder which needs it)
import queue      # noqa: E402  (grouped with Recorder which needs it)
import threading  # noqa: E402  (grouped with Recorder which needs it)


# The audio capture subprocess — embedded as a string so there's no extra
# file to package / locate at runtime. This child owns PortAudio; when
# it hangs (Pa_StopStream / Pa_CloseStream wedging on macOS), we SIGKILL
# it and the kernel tears down its audio unit, instantly releasing the
# microphone (the "orange mic icon" stays on until the audio unit is
# released — killing the process IS the release).
_AUDIO_CHILD_SCRIPT = r"""
import os, sys, threading, time

# PortAudio's CoreAudio backend writes warnings straight to fd 1 with C-level
# printf ("||PaMacCore (AUHAL)|| Warning on line 521..."), bypassing
# sys.stdout entirely. Sharing fd 1 with the parent protocol means such a
# warning gets read as a command response: the parent sees something that is
# not "ok", SIGKILLs a perfectly healthy child and retries — and if the fresh
# child warns too, the recording never starts at all.
#
# So the protocol gets a private duplicate of fd 1, and fd 1 itself is pointed
# at stderr where library chatter is harmless. Done before importing
# sounddevice, so PortAudio never sees the original descriptor.
try:
    _PROTO = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
except Exception:
    _PROTO = sys.stdout

import collections
import sounddevice as sd

SR = int(sys.argv[1])
# Frames per callback. Left at 0, PortAudio's CoreAudio backend picks ~15
# frames here, i.e. it runs this Python callback about 1060 times a second
# on the real-time audio thread. At 512 frames that drops to ~31/s for the
# same audio — 34x less interpreter work on the one thread that must never
# fall behind.
#
# PortAudio only hands over whole blocks, so a bigger block also means more
# of the tail can be left undelivered when the key is released: on average
# half a block, 16ms here. Inaudible, and there is always some trailing
# silence before a hand comes off the key. 512 keeps that margin while still
# removing the pathological callback rate.
BLOCKSIZE = int(sys.argv[2]) if len(sys.argv) > 2 else 512

_stream = None
# Strong refs to streams whose close is still in flight (or wedged), so
# Python's GC can't run __del__ → Pa_CloseStream underneath us.
_leaked = []
_lock = threading.Lock()

# Roughly ten minutes of audio. A bound, not a target: the writer keeps the
# deque near-empty in practice, and it only stops the queue growing without
# limit if the disk stops accepting writes entirely.
_MAX_QUEUED = (SR * 600) // BLOCKSIZE


# One recording's buffer, writer thread and output file.
#
# Per-recording rather than module-wide on purpose. The callback runs on
# CoreAudio's real-time thread and used to write straight to the file from
# there — a write() syscall on the RT thread, which on a machine that is
# paging parks the thread for as long as the disk takes, with no slack for
# PortAudio to absorb it. So a writer thread does the disk I/O instead.
# That writer is joined with a timeout at stop, and a writer still stuck in
# a slow write when the timeout expires would, if it shared one buffer with
# the next recording, drain the NEXT clip's audio into the PREVIOUS clip's
# file. Giving each capture its own state makes that impossible: a straggler
# can only ever finish writing its own.
class _Capture(object):
    def __init__(self, f):
        self.file = f
        self.chunks = collections.deque()
        self.event = threading.Event()
        # Set by the first callback: proof the device is really delivering
        # audio, as opposed to merely having accepted Pa_StartStream.
        self.live = threading.Event()
        self.capturing = True
        self.thread = None

    def drain(self):
        while self.chunks:
            try:
                chunk = self.chunks.popleft()
            except IndexError:
                break
            try:
                self.file.write(chunk)
            except Exception:
                pass

    def run(self):
        while self.capturing:
            self.event.wait(0.2)
            self.event.clear()
            self.drain()
            # Push it to the OS every pass. The parent SIGKILLs this process
            # when a stop wedges, and anything still in Python's file buffer
            # (up to a quarter second of speech) would die with it.
            try:
                self.file.flush()
            except Exception:
                pass

    # Stop accepting audio, flush everything captured so far, close the file.
    def finish(self):
        self.capturing = False
        self.event.set()
        if self.thread is not None:
            self.thread.join(timeout=1.0)
            self.thread = None
        try:
            self.drain()       # whatever the writer hadn't picked up yet
            self.file.flush()
            self.file.close()
        except Exception:
            pass


_cap = None   # the in-flight _Capture, or None

def _cb(indata, frames, t, status):
    # Real-time thread. Append and return — nothing blocking, ever.
    c = _cap
    if c is not None and c.capturing and len(c.chunks) < _MAX_QUEUED:
        c.chunks.append(bytes(indata))
        c.event.set()
        if not c.live.is_set():
            c.live.set()

def _do_start(path):
    global _stream, _cap
    with _lock:
        if _stream is not None or _cap is not None:
            # Previous recording never got a "stop". Roll it over.
            _stop_locked()
        cap = None
        try:
            cap = _Capture(open(path, "wb"))
            cap.thread = threading.Thread(target=cap.run, daemon=True)
            cap.thread.start()
            _cap = cap
            err = None
            for attempt in (1, 2):
                _refresh_portaudio()
                err = _open_verified(cap)
                if err is None:
                    return "ok"
                sys.stderr.write("[child] open attempt %d failed: %s\n"
                                 % (attempt, err))
                sys.stderr.flush()
            raise RuntimeError(err)
        except Exception as exc:
            _stream = None
            _cap = None
            if cap is not None:
                cap.finish()
            return "err start " + str(exc)[:160]

# How long a freshly started stream gets to produce its first buffer. One
# buffer is 32ms; a Bluetooth headset switching profile can take a couple of
# seconds. Anything beyond this is a stream that opened but is not wired to a
# working device.
FIRST_AUDIO_TIMEOUT = 2.5


def _refresh_portaudio():
    # PortAudio enumerates CoreAudio devices once, at Pa_Initialize, and keeps
    # those device object IDs for the life of the process. This child lives for
    # days. Across a sleep, a lid close, or AirPods coming and going, CoreAudio
    # hands out new IDs and the cached ones point at nothing: the next open
    # fails with AUHAL '!obj' / -10851 / PaErrorCode -9986. That was the "first
    # press of the day never records" bug — every one of those errors in the
    # log followed an idle gap of 30 minutes to several hours.
    #
    # Re-initialising costs a few milliseconds, so do it before every open
    # rather than trying to guess when the device list went stale.
    #
    # Pa_Terminate closes every open stream itself, so it must not race a
    # close still running on a helper thread. Give those a moment to finish,
    # and skip the refresh rather than risk a double close.
    deadline = time.time() + 0.5
    while _leaked and time.time() < deadline:
        time.sleep(0.02)
    if _leaked:
        sys.stderr.write("[child] close still in flight; PortAudio not refreshed\n")
        sys.stderr.flush()
        return
    try:
        sd._terminate()
        sd._initialize()
    except Exception as exc:
        sys.stderr.write("[child] PortAudio refresh failed: %s\n" % exc)
        sys.stderr.flush()


def _open_verified(cap):
    # Open and start a stream, then wait for audio to actually arrive. Returns
    # None on success (the stream is installed as _stream), otherwise a reason.
    # Replying "ok" only once a buffer has landed is what lets the parent's
    # recording indicator mean "you are being heard" rather than "we asked".
    global _stream
    try:
        s = sd.InputStream(
            samplerate=SR, channels=1, dtype="int16",
            blocksize=BLOCKSIZE, callback=_cb,
        )
    except Exception as exc:
        return str(exc)
    try:
        s.start()
    except Exception as exc:
        _release(s)
        return str(exc)
    if cap.live.wait(FIRST_AUDIO_TIMEOUT):
        _stream = s
        return None
    _release(s)
    return "input device opened but delivered no audio"


def _release(s):
    # Close on a helper thread, holding a strong ref. Closing releases the
    # audio unit instead of stranding it for the life of the child; doing it
    # off to the side means that even if Pa_CloseStream ever does wedge, it
    # costs the parent nothing.
    _leaked.append(s)
    def _close(stream=s):
        try:
            stream.close()
        except Exception:
            pass
        finally:
            # Whether or not it raised, the close is over: holding the ref any
            # longer would only block every later PortAudio refresh.
            try: _leaked.remove(stream)
            except ValueError: pass
    threading.Thread(target=_close, daemon=True).start()


def _stop_locked():
    global _stream, _cap
    if _stream is not None:
        s = _stream
        _stream = None
        try:
            # .stop() drains the buffer PortAudio is holding, so the tail of
            # the dictation survives; .abort() would discard it. Measured at
            # ~0.1s, comfortably inside the parent's budget.
            s.stop()
        except Exception as exc:
            sys.stderr.write("[child] stop err: " + str(exc) + "\n")
            sys.stderr.flush()
        _release(s)
    # After s.stop() no further callback can fire, so nothing more will be
    # appended and finish() sees the complete recording.
    cap = _cap
    _cap = None
    if cap is not None:
        cap.finish()

def _do_stop():
    with _lock:
        _stop_locked()
        return "ok"

def _reply(msg):
    _PROTO.write(msg + "\n")
    _PROTO.flush()

def _main():
    _reply("ready")
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
            _reply("ok")
            break
        else:
            resp = "err unknown"
        _reply(resp)

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

    # Opening the input stream is the one command that is legitimately slow:
    # a cold or contended CoreAudio device routinely needs longer than
    # CMD_TIMEOUT, and treating that as "the service is sick" used to kill a
    # perfectly healthy child mid-open. The retry then paid the same cold
    # cost, blew the caller's watchdog budget and returned failure — which is
    # what made the mic indicator appear late, or not at all until the hotkey
    # was pressed a second time. Stop keeps the short timeout on purpose: a
    # hung stop must be SIGKILLed quickly or the mic is never released.
    #
    # The child now also waits for the first audio buffer and, failing that,
    # refreshes PortAudio and opens once more. Opens have been logged at up to
    # 3.7s, so two attempts plus their first-audio waits need this much room.
    START_TIMEOUT = 12.0

    # Frames per PortAudio callback in the child. See BLOCKSIZE there.
    BLOCKSIZE = 512

    # Lines the protocol recognises. Anything else on the channel is library
    # chatter and must be skipped rather than mistaken for a response.
    _TOKENS = ("ok", "ready")

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lines: "queue.Queue[str | None]" = queue.Queue()
        self._lock = threading.Lock()
        # Warm-up runs on its own thread and must not be able to block, or
        # deadlock with, a press that arrives while it is in flight — hence
        # its own lock, held only to deduplicate warm-up threads.
        self._warm_lock = threading.Lock()
        self._warming: threading.Thread | None = None
        self._closed = False

    # -- lifecycle ----------------------------------------------------

    def _spawn(self) -> None:
        proc = subprocess.Popen(
            [_child_python(), "-u", "-c", _AUDIO_CHILD_SCRIPT,
             str(Recorder.SAMPLE_RATE), str(self.BLOCKSIZE)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        # One reader for the child's whole life, feeding a queue. A
        # thread-per-read would strand itself inside readline() whenever a
        # command timed out, and that stranded thread would later swallow a
        # response meant for the next command.
        self._lines = queue.Queue()
        threading.Thread(target=self._pump, args=(proc, self._lines),
                         daemon=True).start()
        # The child's stderr now carries PortAudio's chatter as well as its
        # own. Nobody reading it would let a noisy driver fill the pipe
        # buffer and block the child mid-callback, so drain it continuously
        # and keep the tail for diagnostics.
        self._errtail = collections.deque(maxlen=20)
        threading.Thread(target=self._pump_stderr, args=(proc, self._errtail),
                         daemon=True).start()
        line = self._await(timeout=5.0)
        if line != "ready":
            try: proc.kill()
            except Exception: pass
            raise RuntimeError(
                f"audio service did not start (got {line!r}); "
                f"stderr: {' | '.join(self._errtail)[:200]}"
            )
        self._proc = proc

    @staticmethod
    def _pump(proc: subprocess.Popen, q: "queue.Queue[str | None]") -> None:
        """Drain the child's protocol channel into `q` until it exits."""
        try:
            out = proc.stdout
            if out is not None:
                for raw in iter(out.readline, b""):
                    q.put(raw.decode(errors="replace").strip())
        except Exception:
            pass
        finally:
            q.put(None)   # EOF sentinel: the child is gone

    @staticmethod
    def _pump_stderr(proc: subprocess.Popen, tail) -> None:
        """Keep the child's stderr flowing so it can never block on a full pipe."""
        try:
            err = proc.stderr
            if err is not None:
                for raw in iter(err.readline, b""):
                    msg = raw.decode(errors="replace").rstrip()
                    if msg:
                        tail.append(msg)
                        print(f"[rec child] {msg}", file=sys.stderr)
        except Exception:
            pass

    def _await(self, timeout: float):
        """Next protocol response, skipping noise. None on timeout or EOF.

        Skipping rather than failing is what stops a stray line — a PortAudio
        warning that beat the child's fd redirect, a Python warning — from
        costing a healthy child a SIGKILL and the user a lost recording.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                return None
            if line in self._TOKENS or line.startswith("err"):
                return line
            print(f"[rec svc] ignoring stray output: {line[:120]!r}",
                  file=sys.stderr)

    # -- keeping a child warm -----------------------------------------

    def ensure_warm(self) -> None:
        """
        Make sure a live child is standing by, spawning one in the background
        if not. Returns immediately; never blocks the caller.

        This is the difference between a press that costs ~0.16s and one that
        costs ~0.60s or worse. Spawning the child means starting a Python
        interpreter, importing sounddevice and running Pa_Initialize, and
        that used to land squarely on the press path every time the previous
        recording ended with a SIGKILL — which is exactly the "hold the key
        and the mic indicator takes ages to show up, but the second press is
        fine" symptom: the second press was simply the one that found a warm
        child. Warming after the kill instead of before the next open moves
        that whole cost off the hot path.
        """
        if self._closed:
            return
        with self._warm_lock:
            if self._warming is not None and self._warming.is_alive():
                return
            self._warming = threading.Thread(target=self._warm, daemon=True)
            self._warming.start()

    def _warm(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._proc is not None and self._proc.poll() is None:
                return
            t0 = time.monotonic()
            try:
                self._spawn()
            except Exception as exc:
                print(f"[rec svc] warm-up failed: {exc}", file=sys.stderr)
                return
        print(f"[rec svc] warm child ready in {time.monotonic() - t0:.2f}s",
              file=sys.stderr)

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
        return self._await(timeout)

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
        # Rebuild the standby child right away rather than leaving the next
        # press to pay for it. The warm thread takes the same lock we are
        # holding, so it can't race a start/stop already in progress: it
        # waits, then finds either a live child (no-op) or spawns one.
        self.ensure_warm()

    # -- public API ---------------------------------------------------

    def start(self, path: str) -> bool:
        """Begin capture to `path` (raw 16-bit mono PCM). Returns True on success."""
        with self._lock:
            t0 = time.monotonic()
            warm = self._proc is not None and self._proc.poll() is None
            resp = self._send(f"start {path}", timeout=self.START_TIMEOUT)
            if resp == "ok":
                # The number to look at when "the mic icon took ages": this
                # is the whole press-to-capture cost, and whether the child
                # had to be built first.
                took = time.monotonic() - t0
                if took > 0.75 or not warm:
                    print(f"[rec svc] mic open took {took:.2f}s "
                          f"({'warm' if warm else 'COLD — had to spawn'})",
                          file=sys.stderr)
                return True

            # The child must not be left holding the device: it may still be
            # inside Pa_OpenStream and would go on recording into a file the
            # caller is about to delete, with the orange mic indicator stuck
            # on. SIGKILL is the only thing guaranteed to free it.
            alive = self._proc is not None and self._proc.poll() is None
            print(f"[rec svc] start got {resp!r}; killing child", file=sys.stderr)
            self._kill()

            # Retry only when the failure was cheap. A timeout means we have
            # already spent the caller's budget, and a second open would be
            # just as slow — better to fail now and let the next hotkey press
            # meet a warm device than to stack another wait on top.
            if resp is None and alive:
                return False
            try:
                resp = self._send(f"start {path}", timeout=self.START_TIMEOUT)
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
            t0 = time.monotonic()
            resp = self._send("stop", timeout=self.CMD_TIMEOUT)
            if resp == "ok":
                took = time.monotonic() - t0
                if took > 0.75:
                    print(f"[rec svc] slow stop: {took:.2f}s", file=sys.stderr)
                return True
            # The child is stuck in PortAudio. SIGKILL is the only thing
            # that will reliably free the mic and clear the orange icon.
            # _kill() immediately warms a replacement, so this costs the mic
            # for this clip but not the latency of the next press.
            print(f"[rec svc] stop got {resp!r} after "
                  f"{time.monotonic() - t0:.2f}s; SIGKILL", file=sys.stderr)
            self._kill()
            return False

    def shutdown(self) -> None:
        # Set before anything else: _kill() re-warms by default, and a
        # shutdown that keeps respawning children never finishes.
        self._closed = True
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


def prewarm_recorder() -> None:
    """
    Build the capture child ahead of time so the first hotkey press doesn't.

    Safe to call repeatedly and from any thread; returns immediately.
    """
    _svc.ensure_warm()


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


class TranscriptionError(RuntimeError):
    """The audio could not be transcribed (network, API), as opposed to
    containing no speech."""


# Seconds to wait before each retry of a failed upload. Right after a wake the
# Wi-Fi is often still reconnecting and DNS fails outright ("nodename nor
# servname provided"); a few seconds later the same request goes through.
_STT_RETRY_DELAYS = (2.0, 4.0, 8.0)


def transcribe(wav_bytes: bytes, language: str | None = "en",
               raise_errors: bool = False) -> str:
    """
    Transcribe WAV bytes with Groq Whisper.

    Returns "" for no key, no audio, or no speech. A request that fails
    (after retrying transient network errors) also returns "" unless
    `raise_errors` is set, in which case TranscriptionError is raised so the
    caller can keep the audio instead of silently discarding it.
    """
    key = groq_api_key()
    if not key:
        return ""
    if not wav_bytes:
        return ""
    data = {
        "model": STT_MODEL,
        "response_format": "json",
        "temperature": "0",
    }
    # Omitting `language` lets Whisper auto-detect the spoken language.
    if language:
        data["language"] = language
    last: Exception | None = None
    for attempt, delay in enumerate((0.0,) + _STT_RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            with httpx.Client(timeout=30.0) as c:
                r = c.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {key}"},
                    files={"file": ("rec.wav", wav_bytes, "audio/wav")},
                    data=data,
                )
                r.raise_for_status()
                return (r.json().get("text") or "").strip()
        except httpx.TransportError as exc:
            # Connection, DNS, timeouts: worth another go.
            last = exc
            print(f"[stt] attempt {attempt + 1}: {exc}", file=sys.stderr)
        except httpx.HTTPStatusError as exc:
            last = exc
            print(f"[stt] {exc}", file=sys.stderr)
            code = exc.response.status_code
            if code != 429 and code < 500:
                break   # bad key, bad request: retrying won't change it
        except Exception as exc:
            last = exc
            print(f"[stt] {exc}", file=sys.stderr)
            break
    if raise_errors:
        raise TranscriptionError(str(last) or type(last).__name__)
    return ""


UNSENT_DIR = CONFIG_DIR / "unsent-recordings"


def save_unsent_recording(wav_bytes: bytes) -> Path:
    """Keep a recording that could not be transcribed, so it isn't lost."""
    UNSENT_DIR.mkdir(parents=True, exist_ok=True)
    path = UNSENT_DIR / f"scribe-{datetime.now():%Y%m%d-%H%M%S}.wav"
    path.write_bytes(wav_bytes)
    # Bounded: keep the most recent 20.
    for old in sorted(UNSENT_DIR.glob("scribe-*.wav"))[:-20]:
        try:
            old.unlink()
        except Exception:
            pass
    return path


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
