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
import datetime
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
# A longer silence than any punctuation gets from the voice: between table
# rows, so the ear hears where one row ends. Kept on a line of its own by
# clean_text_for_speech(..., breaks=True); see split_breaks().
BREAK = "\ue002"

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
# A short acronym is read as a word by some voices: "AI’s" came out as
# "Ari's" (Edge, en-AU), with or without the "’s". With dots ("A.I.’s") the
# letters are said, without the pause a space adds. Short capitalised words
# are left alone, as is a run of capitals (a shouted line, "WHAT WE DO").
_CAPS_WORDS = frozenset("""
    AM AN AS AT BE BY DO GO HE HI IF IN IS IT ME MY NO OF OH OK ON OR SO TO
    UP US WE ADD AGE AGO ALL AND ANY ARE ART ASK BAD BIG BUT BUY CAN CAR DAY
    DID END FAR FEW FIX FOR FUN GET GOT HAS HER HIM HIS HOT HOW ITS JOB KEY
    LET LOW MAN MAP MAX MIN NET NEW NOT NOW OFF OLD ONE OUR OUT OWN PAY PUT
    RED RUN SAY SEE SET SHE SIX TAX TEN THE TOO TOP TRY TWO USE WAR WAS WAY
    WHO WHY WIN YES YET YOU
""".split()) | frozenset(_SPOKEN_AS_WORD)
_RE_SHORT_ACRONYM = re.compile(
    r"(?<![\w.])(?<![A-Z] )([A-Z]{2,3})(?=s?(?![\w.])|\.(?!\w))(?! [A-Z]{2})")
# Spoken frame of a table: its size with one row and with n rows, the
# column list, "and", and a value shared by every column ("yes for all").
_TABLE_WORDS = {
    "en": ("Table with 1 row.", "Table with {n} rows.", "Columns: {cols}.",
           "and", "{v} for all"),
    "fr": ("Tableau d'une ligne.", "Tableau de {n} lignes.",
           "Colonnes : {cols}.", "et", "{v} pour tous"),
    "es": ("Tabla de una fila.", "Tabla de {n} filas.", "Columnas: {cols}.",
           "y", "{v} para todos"),
    "de": ("Tabelle mit einer Zeile.", "Tabelle mit {n} Zeilen.",
           "Spalten: {cols}.", "und", "{v} für alle"),
    "it": ("Tabella di una riga.", "Tabella di {n} righe.",
           "Colonne: {cols}.", "e", "{v} per tutti"),
}
# Names a numbered row whose column has no word for a header ("#")
_ROW_WORD = {"en": "Row", "fr": "Ligne", "es": "Fila", "de": "Zeile",
             "it": "Riga"}
# Words a file path is spoken with: slash, dot, the home folder, backslash
# and a Windows drive ("C drive"). Read raw, Edge says nothing or "or" for
# "/" and turns French paths into nonsense; spelled out, both are heard.
_PATH_WORDS = {
    "en": ("slash", "dot", "home", "backslash", "{d} drive"),
    "fr": ("slash", "point", "home", "antislash", "lecteur {d}"),
    "es": ("barra", "punto", "home", "barra invertida", "unidad {d}"),
    "de": ("Slash", "Punkt", "Home", "Backslash", "Laufwerk {d}"),
    "it": ("slash", "punto", "home", "backslash", "unità {d}"),
}
# Names of symbols that are the subject of a sentence ("starts with /, ~
# or ."), which the voice skips or the noise pass deletes.
_SYMBOL_NAMES = {
    "en": {"/": "slash", "\\": "backslash", "~": "tilde", ".": "dot",
           "|": "pipe", "#": "hash", "@": "at", "*": "asterisk",
           "&": "ampersand", "_": "underscore", "-": "hyphen", "=": "equals",
           "+": "plus", "%": "percent", "^": "caret", "`": "backtick",
           "<": "less than", ">": "greater than", "$": "dollar"},
    "fr": {"/": "slash", "\\": "antislash", "~": "tilde", ".": "point",
           "|": "pipe", "#": "dièse", "@": "arobase", "*": "astérisque",
           "&": "esperluette", "_": "tiret bas", "-": "tiret", "=": "égal",
           "+": "plus", "%": "pourcent", "^": "accent circonflexe",
           "`": "accent grave", "<": "inférieur", ">": "supérieur",
           "$": "dollar"},
    "es": {"/": "barra", "\\": "barra invertida", "~": "virgulilla",
           ".": "punto", "|": "barra vertical", "#": "almohadilla",
           "@": "arroba", "*": "asterisco", "&": "et", "_": "guion bajo",
           "-": "guion", "=": "igual", "+": "más", "%": "por ciento",
           "^": "circunflejo", "`": "acento grave", "<": "menor que",
           ">": "mayor que", "$": "dólar"},
    "de": {"/": "Slash", "\\": "Backslash", "~": "Tilde", ".": "Punkt",
           "|": "Pipe", "#": "Raute", "@": "at", "*": "Sternchen",
           "&": "kaufmännisches Und", "_": "Unterstrich", "-": "Bindestrich",
           "=": "gleich", "+": "plus", "%": "Prozent", "^": "Zirkumflex",
           "`": "Backtick", "<": "kleiner als", ">": "größer als",
           "$": "Dollar"},
    "it": {"/": "slash", "\\": "backslash", "~": "tilde", ".": "punto",
           "|": "pipe", "#": "cancelletto", "@": "chiocciola",
           "*": "asterisco", "&": "e commerciale", "_": "trattino basso",
           "-": "trattino", "=": "uguale", "+": "più", "%": "percento",
           "^": "accento circonflesso", "`": "accento grave",
           "<": "minore di", ">": "maggiore di", "$": "dollaro"},
}
# "5 * 3": multiplication between numbers
_TIMES_WORD = {"en": "times", "fr": "fois", "es": "por", "de": "mal",
               "it": "per"}
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


# --- French dates and fractions ----------------------------------------------

_FR_MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin",
              "juillet", "août", "septembre", "octobre", "novembre", "décembre")
_RE_FR_DATE = re.compile(
    r"(?<![\w/])([0-9]{1,2})/([0-9]{1,2})(?:/([0-9]{4}|[0-9]{2}))?(?![\w/])")
_RE_FR_DATE_CONTEXT = re.compile(
    r"\b(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|"
    r"le|du|au|avant|après|depuis|jusqu’au|jusqu'au|date|prévu)"
    r"\s*:?\s*$", re.IGNORECASE)
_RE_FR_FRACTION_CONTEXT = re.compile(
    r"\s*(?:de\b|du\b|des\b|d['’]|×|[+*=])", re.IGNORECASE)
_RE_FR_FRACTION_PREFIX = re.compile(
    r"\b(?:fraction|part|parts|proportion|quote-part|rapport|ratio|note|score)"
    r"\s*:?\s*$", re.IGNORECASE)
_RE_FR_FRACTION = re.compile(
    r"(?<![\w/.,])([0-9]+)[ \t]*[/⁄∕][ \t]*([0-9]+)(?![\w/]|[.,][0-9])")
_FR_DENOMINATORS = {
    2: "demi", 3: "tiers", 4: "quart", 5: "cinquième", 6: "sixième",
    7: "septième", 8: "huitième", 9: "neuvième", 10: "dixième",
    11: "onzième", 12: "douzième", 13: "treizième", 14: "quatorzième",
    15: "quinzième", 16: "seizième", 17: "dix-septième",
    18: "dix-huitième", 19: "dix-neuvième", 20: "vingtième",
    100: "centième", 1000: "millième",
}


def _expand_french_dates(text):
    """Name the month when a slash notation is a date, not a fraction.

    Short dates require a calendar cue. A quantity cue takes precedence:
    "le 3/4 du bien" is a fraction, even though it follows "le".
    """
    def replace(match):
        day, month, year = match.groups()
        if year is None and (
                _RE_FR_FRACTION_CONTEXT.match(text, match.end())
                or _RE_FR_FRACTION_PREFIX.search(text[:match.start()])):
            return match.group(0)
        if year is None and not _RE_FR_DATE_CONTEXT.search(text[:match.start()]):
            return match.group(0)
        try:
            # A leap year allows 29/02 when no year was specified.
            check_year = int(year) if year else 2000
            if year and len(year) == 2:
                check_year += 2000
            datetime.date(check_year, int(month), int(day))
        except ValueError:
            return match.group(0)
        spoken_day = "premier" if int(day) == 1 else str(int(day))
        result = f"{spoken_day} {_FR_MONTHS[int(month) - 1]}"
        return result + (f" {check_year}" if year else "")
    return _RE_FR_DATE.sub(replace, text)


def _expand_french_fractions(text):
    """Do not let the voice reinterpret remaining number pairs as dates.

    Common denominators have spoken names; other ratios use the unambiguous
    "sur" form. Three-part dates and file paths are excluded by boundaries.
    """
    def replace(match):
        numerator, denominator = map(int, match.groups())
        before = text[:match.start()]
        if (numerator, denominator) == (24, 7):
            return "24 heures sur 24, 7 jours sur 7"
        if re.search(r"\b(?:note|score|rapport|ratio)\s*:?\s*$", before, re.I):
            return f"{numerator} sur {denominator}"
        name = _FR_DENOMINATORS.get(denominator)
        if name is None:
            return f"{numerator} sur {denominator}"
        if numerator > 1 and not name.endswith("s"):
            name += "s"
        return f"{numerator} {name}"
    return _RE_FR_FRACTION.sub(replace, text)


# --- File paths ---------------------------------------------------------------

# Starts a path: the start of a line, a space, or an opening quote/bracket.
_PATH_EDGE = r"""(?<![^\s(\["'`<])"""
_PATH_SEG = r"[\w.\-@+%]+"
_RE_PATH = re.compile(
    # absolute or home-relative: /etc/hosts, ~/bin/app, ./run.sh, ../x
    _PATH_EDGE + r"(?:~|\.{1,2})?(?:/" + _PATH_SEG + r")+/?"
    # relative: bin/app.py, src/ (kept only if it looks like a file, below)
    + r"|" + _PATH_EDGE + _PATH_SEG + r"(?:/" + _PATH_SEG + r")+/?"
    # Windows: C:\Users\avi\file.txt
    + r"|" + _PATH_EDGE + r"[A-Za-z]:(?:\\" + _PATH_SEG + r")+\\?")
# A user's home folder written out in full
_RE_HOME = re.compile(r"^(?:/Users|/home)/[^/]+(?=/|$)")
_RE_EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,5}$")


def _speak_path(match, lang):
    """A file path read the way a person dictates one: "~/bin/app.py" ->
    "home slash bin slash app dot PY". Fractions and pairs that only look
    like paths (24/7, and/or, km/h, 22/10/2026) are left for later text
    preparation: a relative path needs a file extension or a
    trailing slash. Extensions of up to four letters are capitalised so
    they are spelled out: "sh" was heard as "sure", "py" as "p" in French.
    """
    slash, dot, home, backslash, drive = _PATH_WORDS.get(lang, _PATH_WORDS["en"])
    path = match.group(0)
    # A full stop or comma after the path ends the sentence
    stripped = path.rstrip(".,;:!?")
    tail = path[len(stripped):]
    path = stripped
    if not path.startswith(("/", "~", ".")) and not re.match(r"[A-Za-z]:\\", path):
        if not (path.endswith("/") or _RE_EXTENSION.search(path)):
            return match.group(0)
    sep = slash
    words = []
    if re.match(r"[A-Za-z]:\\", path):
        words.append(drive.format(d=path[0].upper()))
        path, sep = path[2:], backslash
    elif path.startswith("~") or _RE_HOME.match(path):
        words.append(home)
        path = path[1:] if path.startswith("~") else _RE_HOME.sub("", path)
    parts = path.replace("\\", "/").split("/")
    if parts and parts[-1] == "":
        parts.pop()  # a trailing slash is not heard
    for n, part in enumerate(parts):
        if n or words:
            if part or n:
                words.append(sep)
        if not part:
            continue
        name, ext = part, ""
        found = _RE_EXTENSION.search(part)
        if found and found.start() > 0:
            name, ext = part[:found.start()], found.group(0)[1:]
            ext = ext.upper() if len(ext) <= 4 else ext
        if name in (".", ".."):
            words.append(" ".join([dot] * len(name)))
        else:
            # ".config" -> "dot config", "tts_text" -> "tts text"
            words.append(re.sub(r"\.", f" {dot} ", name).replace("_", " ").strip())
        if ext:
            words += [dot, ext]
    # The leading "/" of an absolute path: parts[0] is "" there, which the
    # loop turns into a single "slash" before the first folder.
    # A short pause on each side keeps the path from merging with the words
    # around it: French heard "Lance home" as "Lancôme", "app puis" "appuie".
    return PAUSE + " ".join(w for w in words if w) + tail + PAUSE


# --- Symbols talked about --------------------------------------------------

_SYMBOL_CHARS = re.escape("".join(_SYMBOL_NAMES["en"]))
# `/` or "~" or '.': a symbol quoted on its own is always the subject
_RE_QUOTED_SYMBOL = re.compile(
    r"""(`|"|'|“|‘|«\s?)([""" + _SYMBOL_CHARS + r"""]{1,3})(\s?»|`|"|'|”|’)""")
# A lone symbol in running text: after a word and a space, or after "(",
# so never a list "* item" or a heading "# Title", even indented.
_LONE = r"(?:(?<=\S[ \t])|(?<=\())"
_CONJ = r"(?:or|and|ou|et|y|o|oder|und|e)"
# Followed by punctuation, the end of the sentence, or a conjunction
# ("with / or ~"; "and / or", between two, is prose):
# "starts with /, ~ or ."
_RE_LONE_SYMBOL = re.compile(
    _LONE + r"([" + _SYMBOL_CHARS
    + r"])(?=[,;:)]|[.!?](?:\s|$)|[ \t]*(?:\n|$)|[ \t]+" + _CONJ + r"\b)")
# Never prose wherever it stands alone: "~ or", "press * to mark". Not
# before a number: "~ 3s" is "roughly 3 seconds", "5 * 3" is arithmetic.
_RE_AFTER_CONJ = re.compile(r"\b" + _CONJ + r"[ \t]$")
_RE_BEFORE_CONJ = re.compile(r"[ \t]+" + _CONJ + r"\b")
_RE_LONE_NAMED = re.compile(
    _LONE + r"([~\\^`#*])(?=[ \t]+(?![\d(-]))")


# "5 * 3", "5*3", "(2+3) * 4": a star between numbers multiplies. Read
# raw, the bold-marker pass deleted it and the voice said "5 3".
_RE_TIMES = re.compile(r"(?<=[\d)])[ \t]*\*[ \t]*(?=[\d(])")


def _name_symbols(text, lang):
    """A symbol that is itself the subject is read by name: "starts with /,
    ~ or ." was heard as "starts with, or" (Edge skips a lone / and ., the
    noise pass deleted ~). Prose stays prose: "and / or", "a - b", "5 + 3"
    are not followed by punctuation, so they are left alone."""
    names = _SYMBOL_NAMES.get(lang, _SYMBOL_NAMES["en"])
    times = _TIMES_WORD.get(lang, _TIMES_WORD["en"])
    text = _RE_TIMES.sub(f" {times} ", text)

    def say(chars):
        return " ".join(names[c] for c in chars)
    text = _RE_QUOTED_SYMBOL.sub(lambda m: say(m.group(2)), text)
    def lone(m):
        # "and / or" is prose: a symbol between two conjunctions
        if (_RE_AFTER_CONJ.search(m.string, 0, m.start())
                and _RE_BEFORE_CONJ.match(m.string, m.end())):
            return m.group(0)
        return say(m.group(1))
    text = _RE_LONE_SYMBOL.sub(lone, text)
    return _RE_LONE_NAMED.sub(lambda m: say(m.group(1)), text)


# --- Tables -------------------------------------------------------------------

def _format_table_for_speech(text, lang="en"):
    """Detect tables and reformat them so they can be followed by ear.

    A listener cannot glance back at the headers, so the table is framed
    first (its size and its columns) and every row then starts with its
    own name, which the ear uses as an anchor:
      "Table with 2 rows. Columns: Free, Pro, and Enterprise.
       Storage. Free, 5GB. Pro, 50GB. Enterprise, Unlimited.
       Support. Free, no. Pro and Enterprise, yes."
    Grouping is carried by the pauses, measured on Edge word timings
    (tools/tts_table_check.py): a full stop after the row name and
    between pairs (~0.4 s), a comma inside a pair (~0.2 s). The former
    "Free: 5GB, Pro: 50GB" paused longer inside a pair than between two,
    so the ear heard "5GB Pro" as one unit; a semicolon between pairs
    nearly vanished on some voices. Columns that share a value
    are said once ("Pro and Enterprise, yes"). Two-column tables read as
    "Alice, 30." under a "Columns: Name and Age." frame.
    """
    lines = text.split('\n')
    result = []
    i = 0

    one_row, n_rows, columns, and_, for_all = _TABLE_WORDS.get(
        lang, _TABLE_WORDS["en"])
    yes, no = _YES_NO.get(lang, _YES_NO["en"])

    def _join(items):
        """Natural list, with the Oxford comma in English only."""
        if len(items) == 1:
            return items[0]
        last = f"{',' if lang == 'en' and len(items) > 2 else ''} {and_} "
        return ', '.join(items[:-1]) + last + items[-1]

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
        return cell.rstrip(_TERMINAL)

    def _row_name(header, cell):
        """A bare number names nothing by ear: "1" under "Step" is "Step 1",
        under "#" "Row 1" (and a line starting "1." would be taken for a
        list marker and dropped)."""
        if re.fullmatch(r"#?\d+[.)]?", cell):
            if not re.search(r"[^\W\d_]", header):
                header = _ROW_WORD.get(lang, _ROW_WORD["en"])
            return f"{header} {cell.strip('#.)')}"
        return cell

    def _speak_pairs(pairs):
        """(column, value) pairs as "Free, no. Pro and Enterprise, yes".
        Neighbouring columns sharing a value are named together; columns
        are never reordered, so a series (Q1, Q2...) keeps its order."""
        runs = []
        for h, v in pairs:
            if runs and h and runs[-1][1] and runs[-1][0].lower() == v.lower():
                runs[-1][1].append(h)
            else:
                runs.append((v, [h] if h else []))
        if len(pairs) > 1 and len(runs) == 1 and runs[0][1]:
            return for_all.format(v=pairs[0][1])
        # A blank header names nothing: its value stands alone
        return '. '.join(f"{_join(hs)}, {v}" if hs else v for v, hs in runs)

    def _narrate_rows(headers, table_lines, split_fn):
        """Convert table rows into speech. A blank header (the corner cell
        of a comparison table) keeps its column, so the names stay on the
        right values."""
        n_cols = len(headers)
        if not any(headers):
            return
        rows = []
        for row_line in table_lines:
            cells = split_fn(row_line)
            while len(cells) < n_cols:
                cells.append('')
            cells = [_speak_cell(c) for c in cells[:n_cols]]
            if not any(cells):
                continue
            name = _row_name(headers[0], cells[0]) if cells[0] else ""
            pairs = [(h, c) for h, c in zip(headers[1:], cells[1:]) if c]
            if n_cols <= 2:
                rows.append(', '.join(c for c in (name, *[c for _, c in pairs]) if c) + '.')
            elif not pairs:
                rows.append(f"{name}.")
            else:
                said = _speak_pairs(pairs)
                said = said[:1].upper() + said[1:]  # "Yes for all", a sentence
                rows.append(f"{name}. {said}." if name else f"{said}.")
        if not rows:
            return

        size = one_row if len(rows) == 1 else n_rows.format(n=len(rows))
        # In a wider table the first column holds the row names, which
        # are heard on each row: the columns announced are the values'.
        cols = [h for h in (headers if n_cols <= 2 else headers[1:]) if h]
        result.append(BREAK)
        result.append(f"{size} {columns.format(cols=_join(cols))}" if cols else size)
        for row in rows:
            result.extend(['', BREAK, '', row])
        result.extend(['', BREAK, ''])

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

def split_breaks(text):
    """Text cleaned with breaks=True -> the pieces to speak with a silence
    between each (empty pieces dropped)."""
    return [p.strip() for p in text.split(BREAK) if p.strip()]


def _dot_acronym(m):
    word = m.group(1)
    if word in _CAPS_WORDS:
        return word
    # A full stop already after it ("AI.") ends the last letter
    end = "" if m.string.startswith(".", m.end()) else "."
    return ".".join(word) + end


def clean_text_for_speech(text, lang="en", breaks=False):
    """Strip markdown/URL noise and spell out notation so TTS reads cleanly.

    `lang` (en, fr, es, de, it) picks the words units are spelled out with;
    see speech_lang(). With `breaks`, BREAK marks where a player that can
    insert silence should (between table rows); without, they are removed.
    """
    # ASCII arrows first: the HTML-tag pass would eat "<- a ->" as a tag.
    text = _RE_ASCII_ARROW.sub(PAUSE, text)
    # Tables — before pipe characters get turned into pauses, and before
    # symbols, so a ✓ cell can still be read as "yes"
    text = _format_table_for_speech(text, lang)
    # File paths, before "~" and "\\" are dropped as noise and "/" left
    # to the voice, which says nothing or "or" for it
    text = _RE_PATH.sub(lambda m: _speak_path(m, lang), text)
    # "starts with /, ~ or .": symbols that are the subject, by name
    text = _name_symbols(text, lang)
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
    # "AI’s" -> "A.I.’s"
    text = _RE_SHORT_ACRONYM.sub(_dot_acronym, text)
    if lang == "fr":
        text = _expand_french_dates(text)
        text = _expand_french_fractions(text)
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
    if not breaks:
        text = '\n\n'.join(split_breaks(text))
    return text.strip()
