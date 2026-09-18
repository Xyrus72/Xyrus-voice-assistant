"""Bengali script -> Banglish (romanised Bangla, the way people type it): "আমার ভিনদেশী তারা" -> "amar bhindeshi tara".

Rule-based, no dictionary: inherent vowel "o" with word-final and (simple) medial schwa deletion, vowel signs,
hasanta conjuncts (ব/য/ম-phala doubling, ক্ষ = kkh/kh, জ্ঞ = gg/g, ঙ্গ = ng), chandrabindu/anusvara/visarga,
Bengali digits. Good enough for a YouTube search and for SAPI to say a Bangla song name (SAPI can't read
Bengali script). Also: the Bangla "play" carrier words and speakable() for spoken replies.

Pure functions; any thread.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

BENGALI_RE = re.compile(r"[ঀ-৿]")
_NOT_BENGALI_RE = re.compile(r"[ঀ-৿।॥]+")

# ----------------------------------------------------------------------------- letters
_CONS = {
    "ক": "k", "খ": "kh", "গ": "g", "ঘ": "gh", "ঙ": "ng", "চ": "ch", "ছ": "ch", "জ": "j", "ঝ": "jh", "ঞ": "n",
    "ট": "t", "ঠ": "th", "ড": "d", "ঢ": "dh", "ণ": "n", "ত": "t", "থ": "th", "দ": "d", "ধ": "dh", "ন": "n",
    "প": "p", "ফ": "f", "ব": "b", "ভ": "bh", "ম": "m", "য": "j", "র": "r", "ল": "l", "শ": "sh", "ষ": "sh",
    "স": "s", "হ": "h", "ৎ": "t", "ড়": "r", "ঢ়": "rh", "য়": "y", "ৰ": "r", "ৱ": "w",
}
_VOWELS = {"অ": "o", "আ": "a", "ই": "i", "ঈ": "i", "উ": "u", "ঊ": "u", "ঋ": "ri", "ঌ": "li", "এ": "e",
           "ঐ": "oi", "ও": "o", "ঔ": "ou"}
_SIGNS = {"া": "a", "ি": "i", "ী": "i", "ু": "u", "ূ": "u", "ৃ": "ri", "ৄ": "ri", "ে": "e", "ৈ": "oi",
          "ো": "o", "ৌ": "ou", "ৗ": "ou"}
HASANTA = "্"
NUKTA = "়"
ANUSVARA, CHANDRABINDU, VISARGA = "ং", "ঁ", "ঃ"
_DIGITS = {chr(0x09E6 + i): str(i) for i in range(10)}
_PRECOMPOSED = {"ড়": "ড়", "ঢ়": "ঢ়", "য়": "য়"}
_NUKTA_FORMS = {"ড": "ড়", "ঢ": "ঢ়", "য": "য়"}
# two-letter conjuncts with their own sound: (medial, word-initial)
_PAIRS = {("ক", "ষ"): ("kkh", "kh"), ("জ", "ঞ"): ("gg", "g"), ("ঙ", "গ"): ("ng", "ng"),
          ("ঙ", "ক"): ("nk", "nk"), ("ঞ", "চ"): ("nch", "nch"), ("ঞ", "জ"): ("nj", "nj")}
_KEEP_M_AFTER = frozenset({"ন", "ল", "র"})        # ম-phala is pronounced after these (জন্ম = jonmo)


def has_bengali(text: str) -> bool:
    return bool(BENGALI_RE.search(text or ""))


class _Syl:
    """One syllable: a consonant cluster (source letters), its vowel and trailing marks."""
    __slots__ = ("cluster", "vowel", "explicit_halant", "marks", "indep")

    def __init__(self):
        self.cluster: list[str] = []
        self.vowel: str | None = None          # romanised vowel sign; None = inherent
        self.explicit_halant = False           # cluster ends in a hasanta with nothing after it
        self.marks: list[str] = []             # ং ঁ ঃ after this syllable
        self.indep: str | None = None          # independent vowel (no cluster)


def _parse(word: str) -> list[_Syl]:
    out: list[_Syl] = []
    i, n = 0, len(word)
    cur: _Syl | None = None
    while i < n:
        c = word[i]
        if c in _CONS or (c in _NUKTA_FORMS and i + 1 < n and word[i + 1] == NUKTA):
            letter = c
            if c in _NUKTA_FORMS and i + 1 < n and word[i + 1] == NUKTA:
                letter = _NUKTA_FORMS[c]
                i += 1
            if cur is not None and cur.cluster and cur.explicit_halant:
                cur.cluster.append(letter)        # C + hasanta + C: the conjunct grows
                cur.explicit_halant = False
            else:
                cur = _Syl()
                cur.cluster.append(letter)
                out.append(cur)
            i += 1
            continue
        if c == NUKTA:
            i += 1
            continue
        if c in _SIGNS and cur is not None and cur.cluster and cur.vowel is None and not cur.explicit_halant:
            cur.vowel = _SIGNS[c]
        elif c == HASANTA and cur is not None and cur.cluster:
            cur.explicit_halant = True
        elif c in (ANUSVARA, CHANDRABINDU, VISARGA):
            if cur is None:
                cur = _Syl()
                out.append(cur)
            cur.marks.append(c)
        elif c in _VOWELS:
            cur = _Syl()
            cur.indep = _VOWELS[c]
            out.append(cur)
        i += 1
    return out


def _double(rom: str) -> str:
    """kh -> kkh, sh -> ssh, t -> tt (what a phala or a visarga does to the consonant before it)."""
    return rom[:1] + rom if rom else rom


def _cluster(letters: list[str], initial: bool) -> str:
    out: list[str] = []                       # romanised members
    i = 0
    while i < len(letters):
        a = letters[i]
        pair = (a, letters[i + 1]) if i + 1 < len(letters) else None
        if pair in _PAIRS:
            out.append(_PAIRS[pair][1 if initial and i == 0 else 0])
            i += 2
            continue
        if i > 0:
            prev = letters[i - 1]
            if a in ("ব", "য") and prev != "র" and not (a == "ব" and prev == "ম"):
                if initial and i == 1:        # স্বপ্ন = shopno, ব্যাগ = bag: the phala only colours the sound
                    if prev == "স" and out and out[-1] == "s":
                        out[-1] = "sh"
                elif len(letters) == 2 and out:
                    out[-1] = _double(out[-1])     # ঈশ্বর = isshor, বিদ্যা = bidda
                i += 1
                continue
            if a == "ম" and prev not in _KEEP_M_AFTER:
                if not initial and len(letters) == 2 and out:
                    out[-1] = _double(out[-1])     # পদ্মা = podda, আত্মা = atta
                elif not initial and not (out and out[-1] == "kkh"):     # লক্ষ্মী = lokkhi (silent)
                    out.append("m")
                i += 1                             # word-initial স্মরণ = soron: silent too
                continue
        out.append(_CONS.get(a, ""))
        i += 1
    return "".join(out)


def _word(word: str) -> str:
    syls = _parse(word)
    if not syls:
        return ""
    res: list[str] = []
    double_next = False
    first_cons = next((k for k, s in enumerate(syls) if s.cluster), None)
    for k, s in enumerate(syls):
        nxt = syls[k + 1] if k + 1 < len(syls) else None
        if s.indep is not None:
            res.append(s.indep)
        elif s.cluster:
            rom = _cluster(s.cluster, initial=(k == 0))
            if double_next:
                rom = _double(rom)
            conjunct = len(s.cluster) > 1 or double_next
            double_next = False
            if s.vowel is not None:
                vowel = s.vowel
            elif s.explicit_halant:
                vowel = ""
            elif s.marks and s.marks[0] in (ANUSVARA, VISARGA):
                vowel = "o"                   # সংগীত = songit: the mark needs the vowel before it
            elif nxt is None:                 # word-final schwa deletion (not for 1-letter words/conjuncts/হ)
                last_is_y_after_i = s.cluster == ["য়"] and res and res[-1].endswith("i")
                vowel = "o" if (k == first_cons and len(syls) == 1) or conjunct or s.cluster == ["হ"] \
                    or last_is_y_after_i else ""
            elif k == 0:                      # the word's first syllable keeps it (after a vowel: আমরা = amra)
                vowel = "o"
            elif nxt.cluster and nxt.vowel is not None and not conjunct:
                vowel = ""                    # ভিনদেশী = bhindeshi, আমরা = amra
            else:
                vowel = "o"
            res.append(rom + vowel)
        for m in s.marks:
            if m == ANUSVARA:
                g = nxt is not None and nxt.cluster and nxt.cluster[0] in ("গ", "ক")
                res.append("n" if g else "ng")
            elif m == CHANDRABINDU:
                if nxt is not None and nxt.cluster:
                    res.append("n")           # চাঁদ = chand
            elif m == VISARGA:
                if nxt is not None and nxt.cluster:
                    double_next = True        # দুঃখ = dukkho
                else:
                    res.append("h")
    return "".join(res)


def transliterate(text: str) -> str:
    """Bengali script (mixed text is fine) -> lowercase Banglish; Latin words pass through unchanged."""
    t = unicodedata.normalize("NFC", text or "")
    for a, b in _PRECOMPOSED.items():
        t = t.replace(a, b)
    t = "".join(_DIGITS.get(c, c) for c in t)
    t = t.replace("।", " ").replace("॥", " ").replace("‌", "").replace("‍", "")
    out = []
    for tok in t.split():
        # a token may mix Bengali letters with punctuation/Latin: romanise each Bengali run in place
        parts = re.split(r"([ঀ-৿]+)", tok)
        out.append("".join(_word(p) if has_bengali(p) else p for p in parts))
    return " ".join(w for w in " ".join(out).split())


# ----------------------------------------------------------------------------- speaking
def latin_part(title: str) -> str:
    """'আমার ভিনদেশী তারা | Amar Bhindeshi Tara | Chandrabindoo' -> 'Amar Bhindeshi Tara | Chandrabindoo'."""
    t = _NOT_BENGALI_RE.sub(" ", title or "")
    t = re.sub(r"\s*([|\-–—:/])(\s*[|\-–—:/])+", r" \1 ", t)       # separators left next to each other
    t = re.sub(r"[\(\[]\s*[\)\]]", " ", t)                        # brackets that held only Bengali
    t = " ".join(t.split()).strip(" |-–—:/,.")
    return t if len(re.findall(r"[A-Za-z]", t)) >= 2 else ""


def speakable(text: str) -> str:
    """Text SAPI can say: unchanged without Bengali script; else its Latin part, or the Banglish."""
    if not has_bengali(text):
        return text
    return latin_part(text) or transliterate(text)


# ----------------------------------------------------------------------------- "play" in Bangla
PLAY_VERBS = frozenset({"বাজাও", "বাজান", "বাজাবে", "বাজিয়ে", "চালাও", "চালান", "চালিয়ে", "শোনাও", "শুনাও",
                        "শোনান", "প্লে", "play", "bajao", "chalao", "shonao"})
_END_FILLERS = frozenset({"করো", "কর", "করুন", "করে", "দাও", "দিন", "দে", "তো", "একটু", "প্লিজ", "প্লীজ", "please",
                          "গান", "গানটা", "গানটি", "গানটাকে", "গানটাও", "সং", "সংটা", "song", "টা", "টি",
                          "স্যার", "sir", "এখন", "now"})
_START_FILLERS = frozenset({"একটা", "একটি", "একটু", "প্লিজ", "please", "গান", "গানটা", "এই", "ওই"})
# how Whisper tends to write "Xyrus" in Bengali script / the English names it hears
_NAME_FORMS = ("sairas", "jairas", "sairus", "jairus", "sirus", "sirias", "saeras", "cyrus", "xyrus", "zyrus",
               "sirius", "serious", "zairas", "saiyaras")


def _is_name(tok: str) -> bool:
    rom = transliterate(tok).strip(",.!?").lower()
    return bool(rom) and bool(difflib.get_close_matches(rom, _NAME_FORMS, n=1, cutoff=0.72))


def song_request(text: str, *, hinted: bool = False) -> str | None:
    """A Bangla transcript -> the song title (Bengali script) when it asks to play something, "" when it asks
    to play without a name ("একটা গান বাজাও"), None when it isn't a play request. hinted=True: the utterance
    is already known to be a song request (the English pass heard "play …", or "What should I play?")."""
    toks = [w.strip(",.!?।") for w in (text or "").split()]
    toks = [w for w in toks if w]
    while len(toks) > 1 and _is_name(toks[0]):
        toks = toks[1:]
    found = hinted
    changed = True
    while toks and changed:
        changed = False
        if toks[0].lower() in PLAY_VERBS:
            toks, found, changed = toks[1:], True, True
        elif toks[0] in _START_FILLERS and len(toks) > 1:
            toks, changed = toks[1:], True
    changed = True
    while toks and changed:
        changed = False
        last = toks[-1].lower()
        if last in PLAY_VERBS:
            toks, found, changed = toks[:-1], True, True
        elif last in _END_FILLERS:
            toks, changed = toks[:-1], True
    if not found:
        return None
    title = " ".join(toks).strip()
    if title and not has_bengali(title) and len(title.split()) == 1 and title.lower() in _END_FILLERS:
        return ""
    return title
