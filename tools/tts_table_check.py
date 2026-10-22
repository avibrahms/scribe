#!/usr/bin/env python3
"""Listen check for spoken tables: does the voice group what the eye groups?

By ear a table only makes sense if the pauses nest: almost none inside a
column/value pair, a clear one between pairs, and a longer one between
rows. Read as text, "Free: 5GB, Pro: 50GB" looked fine, but Edge paused
longer on each colon than on each comma, so listeners heard "5GB Pro" as
one unit. This script checks, on real Edge voices:

  1. pairs: Edge's word timings for one row, no pause inside a pair as long
     as the shortest pause between two pairs;
  2. rows: the audio bin/edge-tts-stream actually sends to the player,
     every silence between rows clearly longer than any other silence;
  3. the copies in ~/bin, which the hotkey runs, match this repository.

    python3 tools/tts_table_check.py [voice ...]
Voices default to the ones set in Scribe. Needs edge-tts, ffmpeg and network.
"""
import asyncio
import filecmp
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile

REPO_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
DEPLOYED_BIN = os.path.expanduser("~/bin")
SETTINGS = os.path.expanduser("~/.config/speak-selection/settings.json")
sys.path.insert(0, REPO_BIN)
import edge_tts  # noqa: E402
import tts_text  # noqa: E402

TABLE = """| Plan | Basic | Plus | Premium |
|---|---|---|---|
| Storage | small | large | huge |
| Support | email | phone | concierge |
| Price | free | cheap | custom |"""
ROWS = 3
# Each pair is (column, value), one word each so the timings map 1:1.
PAIRS = [("Basic", "small"), ("Plus", "large"), ("Premium", "huge")]
# A row break must be heard as a different thing, not a slightly longer pause.
ROW_RATIO = 1.5


def edge_helper():
    loader = importlib.machinery.SourceFileLoader(
        "edge_tts_stream", os.path.join(REPO_BIN, "edge-tts-stream"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


async def word_timings(text, voice):
    words = []
    async for chunk in edge_tts.Communicate(text, voice, boundary="WordBoundary").stream():
        if chunk["type"] == "WordBoundary":
            start = chunk["offset"] / 1e7
            words.append((chunk["text"].strip(".,;:"), start, start + chunk["duration"] / 1e7))
    return words


def gaps(words, first, second):
    """Silence between each occurrence of word `first` and a following `second`."""
    return [round(words[i + 1][1] - words[i][2], 2) for i in range(len(words) - 1)
            if words[i][0] == first and words[i + 1][0] == second]


def check_pairs(voice):
    row = tts_text.clean_text_for_speech(TABLE, "en").split("\n\n")[1]
    words = asyncio.run(word_timings(row, voice))
    inside = [g for h, v in PAIRS for g in gaps(words, h, v)]
    between = [g for (_, v), (h, _) in zip(PAIRS, PAIRS[1:]) for g in gaps(words, v, h)]
    ok = bool(inside) and bool(between) and max(inside) < min(between)
    print(f"{'ok  ' if ok else 'FAIL'} pairs {voice}: inside {inside}, between {between}")
    return ok


def check_rows(voice, helper):
    """Silences in the helper's real output: the ROWS longest must be the
    row breaks (intro and each row but the last), all ROW_RATIO x longer
    than any other."""
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "table.mp3")
        helper.PLAYER_CMD = ["/bin/sh", "-c", 'cat > "$0"', out]
        pieces = tts_text.split_breaks(
            tts_text.clean_text_for_speech(TABLE, "en", breaks=True))
        spoke, errors, _ = helper.speak_edge(pieces, voice, "+0%", "+0Hz", "+0%")
        if not spoke:
            print(f"FAIL rows  {voice}: Edge did not speak ({errors[-1] if errors else '?'})")
            return False
        log = subprocess.run(
            ["ffmpeg", "-i", out, "-af", "silencedetect=n=-45dB:d=0.15", "-f", "null", "-"],
            capture_output=True, text=True).stderr
    # The last silence is the end of the audio, not a gap
    silences = sorted(float(d) for d in re.findall(r"silence_duration: ([\d.]+)", log)[:-1])
    rows, other = silences[-ROWS:], silences[:-ROWS]
    ok = len(rows) == ROWS and (not other or min(rows) >= ROW_RATIO * max(other))
    print(f"{'ok  ' if ok else 'FAIL'} rows  {voice}: between rows {[round(s, 2) for s in rows]}, "
          f"longest other {round(max(other), 2) if other else None}")
    return ok


def check_deployed():
    ok = True
    for name in ("tts_text.py", "edge-tts-stream", "openai-tts-stream"):
        deployed = os.path.join(DEPLOYED_BIN, name)
        if os.path.exists(deployed) and not filecmp.cmp(
                os.path.join(REPO_BIN, name), deployed, shallow=False):
            print(f"FAIL ~/bin/{name} differs from bin/{name}: the hotkey still runs the old one")
            ok = False
    if ok:
        print("ok   ~/bin matches bin/")
    return ok


def configured_voices():
    try:
        with open(SETTINGS) as f:
            settings = json.load(f)
    except (OSError, ValueError):
        return ["en-US-BrianNeural"]
    voices = [settings.get("voice")] + list((settings.get("edge_voice_by_lang") or {}).values())
    return [v for v in dict.fromkeys(voices) if v] or ["en-US-BrianNeural"]


if __name__ == "__main__":
    helper = edge_helper()
    results = []
    for voice in sys.argv[1:] or configured_voices():
        results += [check_pairs(voice), check_rows(voice, helper)]
    results.append(check_deployed())
    sys.exit(0 if all(results) else 1)
