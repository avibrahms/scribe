"""
Text preparation shared by the read-aloud helpers (edge-tts-stream,
openai-tts-stream), deployed next to them like tts_lang.py.

Turns markdown, tables, symbols and terse notation into text a voice reads
the way a person would: "3s" becomes "3 seconds", an arrow or an emoji
between two words becomes a short pause instead of vanishing (which ran the
words together), and a table row names each column with a pause before its
value. Pauses are plain punctuation, the only pacing both engines honour.
"""
import functools
import re
import unicodedata

try:
    import tts_lang
except Exception:
    tts_lang = None

# Stands in for "pause here" until the end, when it becomes a comma or
# disappears next to punctuation or a line edge (a private-use code point,
# so it never collides with real text).
PAUSE = ""

# --- Language-dependent words ----------------------------------------------

# unit -> {lang: (singular, plural)}
_UNIT_WORDS = {
    "ms": {"en": ("millisecond", "milliseconds"),
           "fr": ("milliseconde", "millisecondes"),
           "es": ("milisegundo", "milisegundos"),
           "de": ("Millisekunde", "Millisekunden"),
           "it": ("millisecondo", "millisecondi")},
    "s": {"en": ("second", "seconds"),
          "fr": ("seconde", "secondes"),
          "es": ("segundo", "segundos"),
          "de": ("Sekunde", "Sekunden"),
          "it": ("secondo", "secondi")},
    "min": {"en": ("minute", "minutes"),
            "fr": ("minute", "minutes"),
            "es": ("minuto", "minutos"),
            "de": ("Minute", "Minuten"),
            "it": ("minuto", "minuti")},
    "h": {"en": ("hour", "hours"),
          "fr": ("heure", "heures"),
          "es": ("hora", "horas"),
          "de": ("Stunde", "Stunden"),
          "it": ("ora", "ore")},
}
# Spellings of each unit, all mapped to a key above.
_UNIT_ALIASES = {
    "ms": "ms", "msec": "ms", "msecs": "ms",
    "s": "s", "sec": "s", "secs": "s",
    "min": "min", "mins": "min",
    "h": "h", "hr": "h", "hrs": "h",
}
# "roughly", not "about": it still reads right after "a" ("a ~3s clip").
_APPROX_WORD = {"en": "roughly", "fr": "environ", "es": "unos",
                "de": "etwa", "it": "circa"}
_ARROW_WORDS = {"en": ("up", "down"), "fr": ("en hausse", "en baisse"),
                "es": ("sube", "baja"), "de": ("hoch", "runter"),
                "it": ("su", "giù")}
# Words written in capitals that are spoken as one word, not spelled out
# letter by letter ("A V I"), in every language. Not after a dot, so a
# file extension (clip.AVI) is left alone.
_SPOKEN_AS_WORD = {"AVI": "Avi"}
_RE_SPOKEN_AS_WORD = re.compile(
    r"(?<![\w.])(" + "|".join(_SPOKEN_AS_WORD) + r")(?!\w)")
_TABLE_WORDS = {"en": ("Table", "and"), "fr": ("Tableau", "et"),
                "es": ("Tabla", "y"), "de": ("Tabelle", "und"),
                "it": ("Tabella", "e")}
_YES_NO = {"en": ("yes", "no"), "fr": ("oui", "non"), "es": ("sí", "no"),
           "de": ("ja", "nein"), "it": ("sì", "no")}
# Mac modifier keys, named the way they are printed on the keys. Checked
# before arrows: ⇧ (Shift) is "UPWARDS WHITE ARROW" to Unicode.
_KEY_NAMES = {"⌘": "Command", "⌥": "Option", "⇧": "Shift", "⌃": "Control",
              "⎋": "Escape", "⏎": "Return", "↩": "Return", "⌫": "Delete",
              "⌦": "Forward Delete", "⇥": "Tab", "⇪": "Caps Lock",
              "␣": "Space", "⏏": "Eject"}


def speech_lang(text, voice=None):
    """Language the text will be spoken in: the voice's own locale when it
    names one ("fr-FR-HenriNeural"), else a guess from the text, else en."""
    if voice:
        code = voice.split("-")[0].lower()
        if "-" in voice and code in _APPROX_WORD:
            return code
    if tts_lang is not None:
        try:
            lang = tts_lang.detect_language(text)
            if lang in _APPROX_WORD:
                return lang
        except Exception:
            pass
    return "en"


def _unit_word(unit, number, lang):
    singular, plural = _UNIT_WORDS[unit].get(lang, _UNIT_WORDS[unit]["en"])
    return singular if number == "1" else plural


# --- Symbols ----------------------------------------------------------------

_DROP = ""          # invisible joiners and modifiers
_KEEP = None        # the voice reads it as is (€, ©, °, ≥, ×, letters)


@functools.lru_cache(maxsize=4096)
def _symbol_role(ch):
    """What a non-ASCII character becomes: _KEEP, _DROP, PAUSE, "up",
    "down" or "key" (see _KEY_NAMES). Decided from the Unicode name and category, so
    every arrow, emoji, bullet, check mark and box-drawing glyph is covered,
    also the ones nobody has typed yet."""
    if ch in _KEY_NAMES:
        return "key"
    cp = ord(ch)
    if (cp in (0x200D, 0x20E3) or 0xFE00 <= cp <= 0xFE0F
            or 0x1F3FB <= cp <= 0x1F3FF or 0xE0020 <= cp <= 0xE007F):
        return _DROP
    cat = unicodedata.category(ch)
    name = unicodedata.name(ch, "")
    if "ARROW" in name and cat in ("So", "Sm"):
        words = set(re.split(r"[ -]", name))
        horizontal = words & {"LEFT", "RIGHT", "LEFTWARDS", "RIGHTWARDS"}
        up = words & {"UP", "UPWARDS", "UPPER"}
        down = words & {"DOWN", "DOWNWARDS", "LOWER"}
        if up and not down and not horizontal:
            return "up"
        if down and not up and not horizontal:
            return "down"
        return PAUSE
    if cat == "So":
        # Letterlike symbols (™ ℃ №), © ® and ° carry meaning: keep them.
        if 0x2100 <= cp <= 0x214F or cp in (0xA9, 0xAE, 0xB0):
            return _KEEP
        return PAUSE
    if ch in "•‣⁃◦∙·":
        return PAUSE
    return _KEEP


def _check_answer(cell):
    """True for a cell that is only a check mark (✓ ✅ ☑), False for only a
    cross (✗ ❌ ✘), None otherwise: in a table they mean yes and no."""
    marks = [ch for ch in cell if not ch.isspace()
             and _symbol_role(ch) != _DROP]
    if not marks or len(marks) > 3:
        return None
    names = {unicodedata.name(ch, "") for ch in marks}
    if all("CHECK" in n for n in names):
        return True
    if all("CROSS" in n or "BALLOT X" in n or n == "MULTIPLICATION X"
           for n in names):
        return False
    return None


def _replace_symbol(match, lang):
    role = _symbol_role(match.group(0))
    if role is _KEEP:
        return match.group(0)
    if role == "key":
        # "⌘⇧V" -> "Command Shift V", but "⌥/Alt" -> "Option/Alt"
        after = match.string[match.end():match.end() + 1]
        return f" {_KEY_NAMES[match.group(0)]}" + (" " if after.isalnum() or after in _KEY_NAMES else "")
    if role in ("up", "down"):
        up, down = _ARROW_WORDS.get(lang, _ARROW_WORDS["en"])
        return f"{PAUSE} {up if role == 'up' else down} "
    return role


_RE_NON_ASCII = re.compile(r"[^\x00-\x7f]")
# ASCII arrows: -> --> => ==> <- <-- <== <-> <=> (not <= or >=, which are math)
_RE_ASCII_ARROW = re.compile(r"<-+>|<=+>|<-+|<==+|-+>|=+>")
# A pipe left over once tables are narrated separates things
_RE_PIPE = re.compile(r"\|")


# --- Markdown and noise (compiled once at import) ---------------------------

# Markdown images ![alt](url) -> alt text only
_RE_MD_IMAGE = re.compile(r'!\[([^\]]*)\]\([^)]+\)')
# Markdown links [text](url) -> text only
_RE_MD_LINK = re.compile(r'\[([^\]]*)\]\([^)]+\)')
# HTML tags
_RE_HTML_TAG = re.compile(r'<[^>]+>')
# Code fences (``` optionally with language)
_RE_CODE_FENCE = re.compile(r'```[a-zA-Z]*')
# Heading markers at start of line (# ## ### etc)
_RE_HEADING = re.compile(r'(?m)^#{1,6}\s+')
# Blockquote markers at start of line
_RE_BLOCKQUOTE = re.compile(r'(?m)^>\s*')
# Bullet/list markers at start of line (- or * followed by space)
_RE_BULLET = re.compile(r'(?m)^[ \t]*[\-\*]\s+')
# Numbered list markers at start of line (1. 2. etc)
_RE_NUMBERED = re.compile(r'(?m)^[ \t]*\d+\.\s+')
# Bold/italic markers (*** ** * ___ __ but NOT underscores inside words)
_RE_BOLD_ITALIC = re.compile(r'(?<!\w)[_*]{1,3}(?!\w)|(?<!\w)[_*]{1,3}(?=\w)|(?<=\w)[_*]{1,3}(?!\w)')
# Inline code backticks
_RE_INLINE_CODE = re.compile(r'`([^`]+)`')
# Horizontal rules (--- *** ___ on their own line)
_RE_HR = re.compile(r'(?m)^[\-\*_]{3,}\s*$')
# URLs: https://example.com/some_path -> "example dot com, some path"
_RE_URL = re.compile(r'https?://([^\s\)\]]+)')
# Remaining noisy characters (tildes, backslashes)
_RE_NOISE = re.compile(r'[~\\]')
# "~5 min" -> "about 5 min"
_RE_APPROX = re.compile(r'~\s?(?=\d)')
# Collapse multiple spaces/newlines
_RE_MULTI_SPACE = re.compile(r'[ \t]{2,}')
_RE_MULTI_NEWLINE = re.compile(r'\n{3,}')
# A line that is a heading or a list item: it ends a thought even without a
# full stop, which the voice only hears if one is there.
_RE_STRUCTURAL_LINE = re.compile(r'^\s*(?:#{1,6}\s|[\-\*]\s|\d+\.\s)')
_TERMINAL = ".!?:;,"


def _humanize_url(match):
    """Turn a URL into something speakable."""
    url = match.group(1)
    # Trailing punctuation ends the sentence, not the URL: keep it after.
    stripped = url.rstrip('.,;:!?')
    tail = url[len(stripped):]
    url = re.sub(r'^www\.', '', stripped)
    parts = url.split('/', 1)
    domain = parts[0].replace('.', ' dot ')
    if len(parts) > 1 and parts[1]:
        path = parts[1].strip('/')
        path = path.replace('_', ' ').replace('-', ' ').replace('/', ', ')
        path = re.sub(r'\.([a-zA-Z]{2,4})$', r' dot \1', path)
        path = re.sub(r'[?#].*', '', path)
        if path.strip():
            return f"{domain}, {path}{tail}"
    return domain + tail


# --- Units --------------------------------------------------------------------

_NUM = r"\d+(?:[.,]\d+)?"
# Not inside a longer token (v1.3s, A3s, 1,2,3s is still fine).
_BEFORE = r"(?<![\w.'’])"
# Followed by neither a letter/digit nor an apostrophe ("2 s'il", "3s's").
_AFTER = r"(?![\w'’])"

# 1h30 -> 1 hour 30   ·   1h30m / 2h15min -> 1 hour 30 minutes
_RE_H_M = re.compile(_BEFORE + r"(\d+)\s?h\s?(\d{1,2})(m|min)?" + _AFTER)
# 2m30s -> 2 minutes 30 seconds
_RE_M_S = re.compile(_BEFORE + r"(\d+)m\s?(" + _NUM + r")s" + _AFTER)
# 3s, 3 s, 500ms, 5 min, 2h, 2 hrs, 1.5sec
_RE_UNIT = re.compile(
    _BEFORE + r"(" + _NUM + r")\s?(ms|msecs?|secs?|s|mins?|hrs?|h)" + _AFTER)
# "the 80s", "'90s", "1990s", "in their 30s": decades, not seconds
# "100s of users": hundreds, not seconds
_RE_PLURAL_OF = re.compile(r"\s+of\b", re.I)
_RE_DECADE_CONTEXT = re.compile(
    r"(?:\b(?:the|early|mid|late|their|his|her|my|your|our)\s+|['’])$",
    re.I)
_RE_BINARY = re.compile(r"\b(?:1s and 0s|0s and 1s)\b")


def _expand_units(text, lang):
    if _RE_BINARY.search(text):
        text = _RE_BINARY.sub(lambda m: m.group(0).replace("s", ""),
                              text)

    def h_m(m):
        hours, mins, suffix = m.group(1), m.group(2), m.group(3)
        out = f"{hours} {_unit_word('h', hours, lang)} {mins}"
        if suffix:
            out += f" {_unit_word('min', mins, lang)}"
        return out

    def m_s(m):
        mins, secs = m.group(1), m.group(2)
        return (f"{mins} {_unit_word('min', mins, lang)} "
                f"{secs} {_unit_word('s', secs, lang)}")

    def unit(m):
        number, raw = m.group(1), m.group(2)
        key = _UNIT_ALIASES[raw]
        if key == "s" and raw == "s":
            whole = number.replace(",", "").replace(".", "")
            before = m.string[max(0, m.start() - 12):m.start()]
            # A decade: 1990s, 80s after "the", '90s.
            if _RE_PLURAL_OF.match(m.string, m.end()):
                return m.group(0)
            if (len(whole) == 4 and number.endswith("0")
                    and number[:2] in ("18", "19", "20")):
                return m.group(0)
            if (number.endswith("0") and len(number) >= 2
                    and _RE_DECADE_CONTEXT.search(before)):
                return m.group(0)
        return f"{number} {_unit_word(key, number, lang)}"

    text = _RE_H_M.sub(h_m, text)
    text = _RE_M_S.sub(m_s, text)
    text = _RE_UNIT.sub(unit, text)
    return text.replace("", "s")


# --- Tables -------------------------------------------------------------------

def _format_table_for_speech(text, lang="en"):
    """Detect tables and reformat them for natural narration.

    Headers are read once, then each row as a short sentence. In rows of
    three or more columns every value is announced by its column name, with
    a colon between them: without it the voice ran "Pro 50GB" together.
    Edge pauses ~250 ms on a colon, comma or dash and ~1 s on a full stop,
    so the full stop is kept for the end of a row.
      2 cols: "Table: Name and Age. Alice, 30. Bob, 25."
      3+ cols: "Table: Feature, Free, and Pro. Storage — Free: 5GB, Pro: 50GB."
    """
    lines = text.split('\n')
    result = []
    i = 0

    table, and_ = _TABLE_WORDS.get(lang, _TABLE_WORDS["en"])
    yes, no = _YES_NO.get(lang, _YES_NO["en"])

    def _header_intro(headers):
        """Natural header list, with the Oxford comma in English only."""
        if len(headers) == 1:
            return headers[0]
        last = f"{',' if lang == 'en' and len(headers) > 2 else ''} {and_} "
        return ', '.join(headers[:-1]) + last + headers[-1]

    def _speak_cell(cell):
        # "✓", "❌" or "✓ (MIT)": the mark is the answer, the rest a detail
        mark = re.match(r"[^\w\s(\[]+", cell)
        answer = _check_answer(mark.group(0)) if mark else None
        if answer is not None:
            rest = cell[mark.end():].strip()
            return (yes if answer else no) + (f" {rest}" if rest else "")
        # "-", "—", "n/a": nothing to say, so the pair is skipped
        if cell.strip("-–— ") == "" or cell.lower() in ("n/a", "na"):
            return ""
        return cell

    def _narrate_rows(headers, table_lines, split_fn):
        """Convert table rows into flowing speech. A blank header (the
        corner cell of a comparison table) keeps its column, so the names
        stay on the right values."""
        n_cols = len(headers)
        named = [h for h in headers if h]
        if not named:
            return
        result.append(f"{table}: {_header_intro(named)}.")
        result.append('')

        for row_line in table_lines:
            cells = split_fn(row_line)
            while len(cells) < n_cols:
                cells.append('')
            cells = [_speak_cell(c) for c in cells[:n_cols]]

            non_empty = [(h, c) for h, c in zip(headers, cells) if c]
            if not non_empty:
                continue

            if n_cols <= 2:
                # Short table: just list values, headers implied from intro
                result.append(', '.join(c for _, c in non_empty) + '.')
            else:
                # Wider table: the first cell names the row, then each value
                # follows its column name after a short pause.
                first_val = non_empty[0][1].rstrip(_TERMINAL)
                rest = [f"{h}: {c.rstrip(_TERMINAL)}" if h
                        else c.rstrip(_TERMINAL) for h, c in non_empty[1:]]
                if rest:
                    result.append(f"{first_val} — {', '.join(rest)}.")
                else:
                    result.append(f"{first_val}.")
            result.append('')

    while i < len(lines):
        line = lines[i].strip()

        # Detect markdown table: line with | delimiters
        if '|' in line and line.count('|') >= 2:
            table_lines = []
            while i < len(lines):
                l = lines[i].strip()
                if '|' not in l or l.count('|') < 2:
                    break
                table_lines.append(l)
                i += 1

            if len(table_lines) >= 2:
                headers = [c.strip() for c in table_lines[0].strip('|').split('|')]

                # Skip separator row (e.g., |---|---|)
                data_start = 1
                if data_start < len(table_lines):
                    sep = table_lines[data_start].replace('|', '').replace('-', '').replace(':', '').strip()
                    if not sep:
                        data_start = 2

                def md_split(row_line):
                    return [c.strip() for c in row_line.strip('|').split('|')]

                _narrate_rows(headers, table_lines[data_start:], md_split)
                continue
            else:
                for tl in table_lines:
                    result.append(tl)
                continue

        # Detect tab-separated table (e.g., copied from spreadsheet)
        elif '\t' in line and i + 1 < len(lines) and '\t' in lines[i + 1].strip():
            table_lines = []
            while i < len(lines):
                l = lines[i]
                if '\t' not in l and l.strip():
                    break
                if l.strip():
                    table_lines.append(l)
                i += 1

            if len(table_lines) >= 2:
                headers = [c.strip() for c in table_lines[0].split('\t')]

                def tsv_split(row_line):
                    return [c.strip() for c in row_line.split('\t')]

                _narrate_rows(headers, table_lines[1:], tsv_split)
                continue
            else:
                for tl in table_lines:
                    result.append(tl)
                continue

        else:
            result.append(lines[i])
            i += 1

    return '\n'.join(result)


# --- Pauses -------------------------------------------------------------------

def _end_structural_lines(text):
    """Give headings and list items a full stop when they lack one, so the
    voice stops between them instead of reading the list as one sentence."""
    out = []
    for line in text.split('\n'):
        if _RE_STRUCTURAL_LINE.match(line):
            body = line.rstrip(" \t*_`" + PAUSE)
            if body and body[-1] not in _TERMINAL:
                line = body + '.' + line[len(body):]
        out.append(line)
    return '\n'.join(out)


_RE_PAUSE_RUN = re.compile(r"[ \t]*" + PAUSE + r"(?:[ \t,]*" + PAUSE + r")*[ \t]*")


def _resolve_pauses(text):
    """Each run of PAUSE becomes ", " between words, and nothing where the
    text already pauses: a line edge or neighbouring punctuation."""
    def repl(m):
        before = m.string[:m.start()].rstrip(" \t")
        after = m.string[m.end():]
        if not before or before[-1] in "\n" + _TERMINAL + "(-–—":
            return " " if before and before[-1] != "\n" else ""
        if not after or after[0] in "\n" + _TERMINAL + ")":
            return ""
        return ", "
    return _RE_PAUSE_RUN.sub(repl, text)


# --- Entry point ----------------------------------------------------------------

def clean_text_for_speech(text, lang="en"):
    """Strip markdown/URL noise and spell out notation so TTS reads cleanly.

    `lang` (en, fr, es, de, it) picks the words units are spelled out with;
    see speech_lang().
    """
    # ASCII arrows first: the HTML-tag pass would eat "<- a ->" as a tag.
    text = _RE_ASCII_ARROW.sub(PAUSE, text)
    # Tables — before pipe characters get turned into pauses, and before
    # symbols, so a ✓ cell can still be read as "yes"
    text = _format_table_for_speech(text, lang)
    # Symbols, emoji and arrows: a pause, never silence. Dropping them
    # outright ran the surrounding words together ("System  Settings").
    text = _RE_NON_ASCII.sub(lambda m: _replace_symbol(m, lang), text)
    text = _end_structural_lines(text)
    # Images first (before links, since ![...] contains [...])
    text = _RE_MD_IMAGE.sub(r'\1', text)
    # Links: keep the display text
    text = _RE_MD_LINK.sub(r'\1', text)
    # HTML tags
    text = _RE_HTML_TAG.sub('', text)
    # Code fences
    text = _RE_CODE_FENCE.sub('', text)
    # Horizontal rules
    text = _RE_HR.sub('', text)
    # Headings (strip the # markers, keep the text)
    text = _RE_HEADING.sub('', text)
    # Blockquotes
    text = _RE_BLOCKQUOTE.sub('', text)
    # Bullet markers
    text = _RE_BULLET.sub('', text)
    # Numbered list markers
    text = _RE_NUMBERED.sub('', text)
    # Inline code: keep the content, drop the backticks
    text = _RE_INLINE_CODE.sub(r'\1', text)
    # Bold/italic markers
    text = _RE_BOLD_ITALIC.sub('', text)
    # URLs -> human readable
    text = _RE_URL.sub(_humanize_url, text)
    # Underscores between words -> spaces (e.g., my_function -> my function)
    text = re.sub(r'(?<=\w)_(?=\w)', ' ', text)
    # "AVI" -> "Avi": a name, not an acronym
    text = _RE_SPOKEN_AS_WORD.sub(lambda m: _SPOKEN_AS_WORD[m.group(1)], text)
    # "3s" -> "3 seconds", "500ms" -> "500 milliseconds"
    text = _expand_units(text, lang)
    # "~5" -> "about 5"
    text = _RE_APPROX.sub(_APPROX_WORD.get(lang, "about") + " ", text)
    # A pipe outside a table separates things: pause on it
    text = _RE_PIPE.sub(PAUSE, text)
    # Remaining noise characters
    text = _RE_NOISE.sub('', text)
    # Stray hashes that aren't part of words (e.g., leftover #)
    text = re.sub(r'(?<!\w)#(?!\w)', '', text)
    text = _resolve_pauses(text)
    # Clean up whitespace
    text = _RE_MULTI_SPACE.sub(' ', text)
    text = re.sub(r'(?m)^[ \t]+|[ \t]+$', '', text)
    text = _RE_MULTI_NEWLINE.sub('\n\n', text)
    return text.strip()
