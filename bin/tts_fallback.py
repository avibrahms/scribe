"""
Fallbacks shared by the read-aloud helpers (edge-tts-stream,
openai-tts-stream), deployed next to them like tts_lang.py.

Read aloud degrades instead of going silent: OpenAI hands its text to Edge,
and Edge, once every endpoint and a library upgrade have failed, hands it to
the Mac's own speech synthesizer. Everything here uses only what ships with
macOS, so it keeps working when every online voice is down.
"""
import os
import subprocess
import sys
import tempfile

SAY = "/usr/bin/say"
OSASCRIPT = "/usr/bin/osascript"

# Asks AVFoundation, the system's own voice catalogue, for the best installed
# voice of a locale such as "fr-FR": never a novelty voice (Bells, Zarvox, …),
# the exact locale before its siblings (fr-CA), the highest quality, and the
# system's own default for that locale on a tie. Prints its identifier, or
# nothing when the language has no voice at all.
_PICK_VOICE_JS = r"""
ObjC.import("AVFoundation");
function run(argv) {
  var want = argv[0], lang = want.split("-")[0];
  var preferred = $.AVSpeechSynthesisVoice.voiceWithLanguage(want);
  preferred = preferred.isNil() ? "" : preferred.identifier.js;
  var voices = $.AVSpeechSynthesisVoice.speechVoices, best = "", bestKey = null;
  for (var i = 0; i < voices.count; i++) {
    var v = voices.objectAtIndex(i), locale = v.language.js;
    if (v.voiceTraits & 1) continue;  // AVSpeechSynthesisVoiceTraitIsNoveltyVoice
    if (locale.split("-")[0] !== lang) continue;
    var key = [locale === want ? 1 : 0, Number(v.quality),
               v.identifier.js === preferred ? 1 : 0];
    var better = !bestKey;
    for (var k = 0; !better && k < key.length && key[k] >= bestKey[k]; k++) {
      better = key[k] > bestKey[k];
    }
    if (better) { best = v.identifier.js; bestKey = key; }
  }
  return best;
}
"""


def notify(title, message):
    """Post a macOS notification without waiting for it. The text goes in as
    arguments, never into the AppleScript source."""
    try:
        subprocess.Popen(
            [OSASCRIPT, "-e", "on run argv",
             "-e", "display notification (item 2 of argv) "
                   "with title (item 1 of argv)",
             "-e", "end run", title, message],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    except OSError:
        pass


def system_voice(locale):
    """Identifier of the best Mac voice for a locale like "fr-FR", or None
    (then `say` uses the system default voice)."""
    if not locale:
        return None
    try:
        out = subprocess.run(
            [OSASCRIPT, "-l", "JavaScript", "-e", _PICK_VOICE_JS, locale],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


def speak_with_system_voice(text, locale=None):
    """Read text with the Mac's own speech synthesizer; True if it did.

    `say` runs in the caller's process group, so a Stop silences it like any
    other playback. Its text file is named scribe-say-* so Scribe's stray-
    process sweep can find it too.
    """
    cmd = [SAY]
    voice = system_voice(locale)
    if voice:
        cmd += ["-v", voice]
    with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", prefix="scribe-say-", suffix=".txt") as f:
        f.write(text)
        f.flush()
        try:
            return subprocess.run(
                cmd + ["-f", f.name], stdin=subprocess.DEVNULL).returncode == 0
        except OSError:
            return False


def exec_with_stdin(argv, text):
    """Replace this process with argv, with `text` as its stdin.

    Same pid and process group, so the shared PID file and a Stop still reach
    the new program. The text sits in an unlinked temp file, so there is no
    argv size limit and nothing is left behind.
    """
    with tempfile.TemporaryFile() as f:
        f.write(text.encode("utf-8", "replace"))
        f.seek(0)
        os.dup2(f.fileno(), 0)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(argv[0], argv)
