#!/usr/bin/env python3
"""
tts_lang — offline language detection for the text Scribe is about to speak.

Deliberately dependency-free and pure (no I/O, no network): it is imported by
three different consumers that must all agree on the answer —

  • scribe.py / scribe_core.py  (the menubar app's Read-aloud path)
  • edge-tts-stream            (standalone helper, ⌃D Quick Action path)
  • openai-tts-stream          (standalone helper, ⌃D Quick Action path)

The helpers live in ~/bin and import this as a sibling module, so this file is
deployed next to them. Keeping it importable from both places is why it has no
package, no config access, and no third-party imports.

Scope is the languages Scribe actually has voices for (see VOICES in
scribe_core). Detecting a language we cannot speak differently from the default
would be noise, so anything else resolves to None and the caller falls back to
its configured default voice.

Method: weighted function-word scoring plus diacritic and suffix signals. Word
weights are derived from the tables themselves — a word shared by N languages
is worth 1/N — so adding a word to a list never silently over-credits a
language, and the tables stay the single source of truth.
"""

import re
import unicodedata

# Languages we ship voices for. Order is only used for stable tie output.
SUPPORTED_LANGS = ("en", "fr", "es", "de", "it")

# Human labels, shared by the menu builders so the two never drift.
LANG_LABELS = {
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "it": "Italian",
}


# --- function-word tables -------------------------------------------------
# High-frequency closed-class words: articles, pronouns, prepositions,
# auxiliaries. These carry almost all of the signal in short clipboard text,
# which is the common case for Read-aloud. Overlap between tables is expected
# and is handled by the automatic 1/N weighting below — do not prune a word
# just because another language also has it.

_WORDS = {
    "en": """
        the be to of and a in that have i it for not on with he as you do at
        this but his by from they we say her she or an will my one all would
        there their what so up out if about who get which go me when make can
        like time no just him know take people into year your good some could
        them see other than then now look only come its over think also back
        after use two how our work first well way even new want because any
        these give day most us is are was were been has had did said more very
        much should may must might here does doing done thing things while
        every another between under such where why still own same too own
    """,
    "fr": """
        le la les un une des du de et est en que qui dans pour pas sur ne se
        ce il elle nous vous ils elles je tu au aux avec plus par mais son sa
        ses leur leurs comme tout tous toute toutes tres bien etre avoir fait
        faire dit dire peut peu ou quand donc alors aussi encore deja jamais
        toujours rien chose temps homme femme jour vie monde meme sans sous
        entre chez depuis avant apres pendant contre vers cette ces cet celui
        celle ceux quel quelle quels quelles si oui non merci bonjour bonsoir
        salut voila voici cest jai nest dun dune quil quelle sont etait ete
        avait ont nos vos mon ma mes ton ta tes lui y en
        a on va vais veux veut faut fais vois sais sait allez allons suis
        sommes etes doit dois peux
    """,
    "es": """
        el la los las un una unos unas de del y en que es se no por con para
        su sus al lo como mas pero le ya este esta estos estas si porque esa
        eso hay muy sin sobre tambien me hasta hacia donde quien desde todo
        todos nos durante uno les ni contra otros ese ante ellos esto antes
        algunos que yo otro otras otra tanto mucho quienes nada muchos cual
        poco ella estar algunas algo nosotros gracias hola buenos dias senor
        senora son era fue ser tiene tienen hacer puede cuando cada vez asi
        aqui alli ahora entonces siempre nunca tener hemos han habia
        a y o vamos voy va van quiero quiere estan soy eres somos hago
        hace debe puedo
    """,
    "de": """
        der die das den dem des ein eine einen einem eines und ist nicht mit
        sich auch auf fur von zu im in an es sie er wir ihr sind war haben hat
        hatte wird werden kann konnen soll sollen muss mussen mehr sehr nur
        noch schon aber oder wenn weil dass wie was wer wo wann warum dann
        dort hier alle alles man nach uber unter vor bei aus durch gegen ohne
        um bis seit wahrend wegen sowie zwischen jetzt heute morgen gestern
        immer nie oft gut viel wenig gross klein danke bitte hallo guten tag
        ich du mich dir mir dich uns euch ihnen dieser diese dieses beim zum
        zur vom am als so kein keine wieder etwas nichts
    """,
    "it": """
        il lo la i gli le un uno una di del della dei delle e che non per con
        su come piu ma anche se ci si da al alla ai alle nel nella nei nelle
        sono stato essere avere ho hai ha abbiamo avete hanno questo questa
        questi queste quello quella quelli quelle molto poco tutto tutti tutte
        niente nulla sempre mai ancora gia quando dove perche chi cosa quale
        grazie ciao buongiorno buonasera lei lui loro noi voi io tu mi ti ne
        era erano fare fatto dice detto puo devo deve solo anche cosi qui li
        dopo prima senza sotto sopra tra fra ogni nostro vostro suo sua
        a o va vado andiamo voglio vuole sto stanno siamo siete faccio fa
        posso
    """,
}


def _build_weights():
    """word -> {lang: weight}, weight = 1/(number of languages using it).

    Derived, never hand-written: the tables above are the only place a word
    is declared, so a word that turns out to be shared automatically stops
    being treated as evidence for any single language.
    """
    langs_by_word = {}
    for lang, blob in _WORDS.items():
        for word in blob.split():
            langs_by_word.setdefault(word, set()).add(lang)
    weights = {}
    for word, langs in langs_by_word.items():
        w = 1.0 / len(langs)
        weights[word] = {lang: w for lang in langs}
    return weights


_WEIGHTS = _build_weights()


# --- diacritic signals ----------------------------------------------------
# Only characters that genuinely discriminate. Accents shared by French and
# Italian (a-grave, e-grave) are worth little; ess-zed and n-tilde are worth a
# lot. Values are per-occurrence and the total is capped in _score().

_CHAR_HINTS = {
    "ß": {"de": 2.0},                              # ß
    "ä": {"de": 1.0}, "ö": {"de": 0.9},       # ä ö
    "ü": {"de": 0.8},                              # ü
    "ñ": {"es": 2.0},                              # ñ
    "¿": {"es": 2.0}, "¡": {"es": 1.5},       # ¿ ¡
    "œ": {"fr": 2.0}, "ç": {"fr": 1.2},       # œ ç
    "ê": {"fr": 0.9}, "î": {"fr": 0.9},       # ê î
    "û": {"fr": 0.9}, "ë": {"fr": 0.6},       # û ë
    "ï": {"fr": 0.6},                              # ï
    "ì": {"it": 1.2}, "ò": {"it": 1.2},       # ì ò
    "à": {"fr": 0.3, "it": 0.5},                   # à
    "è": {"fr": 0.35, "it": 0.45},                 # è
    "é": {"fr": 0.5, "es": 0.2, "it": 0.15},       # é
    "ù": {"fr": 0.3, "it": 0.4},                   # ù
}

_MAX_CHAR_SCORE = 4.0  # a wall of accents must not outvote the words


# --- suffix / substring signals -------------------------------------------
# Applied per token. Morphology is a strong tiebreaker exactly where function
# words are scarce (technical prose, headlines, single sentences).

_SUFFIX_HINTS = (
    ("ung",    {"de": 0.8}),
    ("keit",   {"de": 1.0}),
    ("heit",   {"de": 1.0}),
    ("schaft", {"de": 1.0}),
    ("lich",   {"de": 0.8}),
    ("chen",   {"de": 0.6}),
    ("cion",   {"es": 1.0}),
    ("dad",    {"es": 0.7}),
    ("mente",  {"es": 0.35, "it": 0.35}),
    ("zione",  {"it": 1.2}),
    ("issimo", {"it": 1.0}),
    ("ita",    {"it": 0.3}),
    ("eux",    {"fr": 0.9}),
    ("aient",  {"fr": 1.0}),
    ("ais",    {"fr": 0.4}),
    ("ait",    {"fr": 0.5}),
    ("eur",    {"fr": 0.3}),
    ("ing",    {"en": 0.7}),
    ("ness",   {"en": 0.9}),
    ("ly",     {"en": 0.4}),
    ("ful",    {"en": 0.6}),
)

# Substrings anywhere in a token (not just the end).
_INFIX_HINTS = (
    ("sch", {"de": 0.7}),
    ("gli", {"it": 0.5}),
    ("qu",  {"fr": 0.1, "es": 0.1, "it": 0.1}),
)


# Word characters incl. accents; apostrophes split so French/Italian elisions
# ("l'homme", "dell'arte") contribute their real word rather than a stub.
_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# Scripts we have no voice for at all. If the text is mostly one of these,
# guessing between our five Latin languages is meaningless.
_NON_LATIN_RANGES = (
    (0x0400, 0x04FF),   # Cyrillic
    (0x0590, 0x05FF),   # Hebrew
    (0x0600, 0x06FF),   # Arabic
    (0x0370, 0x03FF),   # Greek
    (0x0900, 0x097F),   # Devanagari
    (0x3040, 0x30FF),   # Kana
    (0x4E00, 0x9FFF),   # CJK
    (0xAC00, 0xD7AF),   # Hangul
)

MAX_SCAN_CHARS = 2000   # plenty for a confident verdict; keeps this instant


def _strip_accents(word):
    """Fold accents for word lookup so the tables can stay plain ASCII.

    The accents themselves are still scored separately in _score(), so folding
    here loses no signal — it only makes the word tables readable and stops a
    missing accent in the user's text from hiding a match.
    """
    return "".join(
        c for c in unicodedata.normalize("NFD", word)
        if not unicodedata.combining(c)
    )


def _is_mostly_non_latin(text):
    letters = non_latin = 0
    for ch in text:
        if not ch.isalpha():
            continue
        letters += 1
        cp = ord(ch)
        for lo, hi in _NON_LATIN_RANGES:
            if lo <= cp <= hi:
                non_latin += 1
                break
    return letters > 0 and non_latin * 2 > letters


def _score(text):
    """Return {lang: score} for the given text."""
    scores = {lang: 0.0 for lang in SUPPORTED_LANGS}

    # Word evidence.
    tokens = [t.lower() for t in _TOKEN_RE.findall(text)]
    for token in tokens:
        folded = _strip_accents(token)
        hit = _WEIGHTS.get(folded)
        if hit:
            for lang, w in hit.items():
                scores[lang] += w
        for suffix, langs in _SUFFIX_HINTS:
            if len(folded) > len(suffix) and folded.endswith(suffix):
                for lang, w in langs.items():
                    scores[lang] += w
        for infix, langs in _INFIX_HINTS:
            if infix in folded:
                for lang, w in langs.items():
                    scores[lang] += w

    # Diacritic evidence, capped so it can only tip a close call.
    char_scores = {lang: 0.0 for lang in SUPPORTED_LANGS}
    for ch in text.lower():
        hit = _CHAR_HINTS.get(ch)
        if hit:
            for lang, w in hit.items():
                char_scores[lang] += w
    for lang, val in char_scores.items():
        scores[lang] += min(val, _MAX_CHAR_SCORE)

    return scores


# Confidence gates. Tuned so a wrong voice is rarer than no detection: when we
# are unsure the caller falls back to the user's default voice, which is the
# behaviour Scribe had before this feature existed.
MIN_SCORE = 0.9      # below this there is simply not enough text
MIN_RATIO = 1.30     # winner must clear the runner-up by 30%


def detect_language(text):
    """Best-guess language code for `text`, or None when unsure.

    None is a first-class answer: callers map it to their configured default
    voice, so an uncertain guess never changes behaviour.
    """
    if not text:
        return None
    sample = text[:MAX_SCAN_CHARS]
    if _is_mostly_non_latin(sample):
        return None

    scores = _score(sample)
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], SUPPORTED_LANGS.index(kv[0])))
    (best_lang, best), (_, second) = ranked[0], ranked[1]

    if best < MIN_SCORE:
        return None
    if second > 0 and best < second * MIN_RATIO:
        return None
    return best_lang


def voice_for_lang(mapping, lang, default, lang_defaults=None):
    """Pick the voice for `lang`, in order of decreasing specificity:

      1. what the user explicitly chose for this language
      2. the catalogue's own voice for this language (`lang_defaults`)
      3. the configured default voice

    Step 2 exists because step 3 alone gives the wrong result whenever the
    default belongs to some other language: Spanish text has no business being
    read by a French voice just because French is the default. Engines whose
    voices are language-neutral (OpenAI) pass no lang_defaults and go straight
    from 1 to 3.

    Shared by the app and both helpers so the rule cannot drift.
    """
    if not lang:
        return default
    if isinstance(mapping, dict) and mapping.get(lang):
        return mapping[lang]
    if isinstance(lang_defaults, dict) and lang_defaults.get(lang):
        return lang_defaults[lang]
    return default
