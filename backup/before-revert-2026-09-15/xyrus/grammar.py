"""Grammar word lists per listening mode (SPEC §4.12, §5.2, D1) + model vocabulary check (D13).

Pure except for the vocabulary cache file and the (optional) checker callback that asks the
recognizer thread `Recognizer.check_words` - vosk-model-small-en-us-0.15 ships no graph/words.txt.
Word sources: registry literal words, parsing.SLOT_TYPES[t].words, normalize.YES/NO/CANCEL_WORDS and the
meaning layer's synonym words (T1), dates.DATE_WORDS/TIME_WORDS (T2), config apps / custom phrases.
Thread: any (internally locked).
"""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

from xyrus import normalize as _norm

log = logging.getLogger("xyrus.grammar")

UNK = "[unk]"

# --------------------------------------------------------------------------- word tables
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december")
UNIT_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
              "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
              "nineteen")
TENS_WORDS = ("twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
NUMBER_WORDS = UNIT_WORDS + TENS_WORDS + ("hundred",)
ORDINAL_WORDS = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth",
                 "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth",
                 "seventeenth", "eighteenth", "nineteenth", "twentieth", "thirtieth")
# SPEC-AMBIGUITY: DATE_WORDS / TIME_WORDS belong to dates.py (T2) and are used from there as soon as it exports
# them; until then these spec/when.py values keep the `when` and `command` grammars complete.
_DATE_FALLBACK = (("today", "tonight", "tomorrow", "yesterday", "day", "days", "week", "weeks", "weekend",
                   "month", "every", "daily", "weekly", "next", "this", "last", "after", "before", "on", "the",
                   "of", "few", "couple", "several", "these", "rest") + WEEKDAYS + MONTHS)
_TIME_FALLBACK = ("am", "pm", "noon", "midnight", "morning", "afternoon", "evening", "night", "half", "past",
                  "quarter", "to", "at", "in", "from", "now", "and", "a", "an", "minute", "minutes", "hour",
                  "hours", "second", "seconds", "o'clock")

CONFIRM_WORDS = ("title", "date", "day", "time", "change", "the")        # §4.12 confirm mode
WHEN_WORDS = ("all", "day", "no", "any", "time")                          # §4.12 when mode
PICK_WORDS = ("first", "second", "third", "one", "two", "three", "all")   # §5.2 pick (sent as extra words)

_TOKEN_RE = re.compile(r"\[unk\]|[a-z0-9']+")


def tokens(text: str) -> list[str]:
    """Lowercase word tokens of a phrase (pattern punctuation such as [] | <> is ignored)."""
    return _TOKEN_RE.findall(str(text).lower())


def _split_all(items: Any) -> set[str]:
    """Every word of a string, or of every (nested) item of an iterable / dict keys+values."""
    out: set[str] = set()
    if items is None:
        return out
    if isinstance(items, str):
        return {t for t in tokens(items) if t != UNK}
    if isinstance(items, dict):
        return _split_all(list(items.keys())) | _split_all(list(items.values()))
    try:
        for item in items:
            out |= _split_all(item)
    except TypeError:
        out |= _split_all(str(items))
    return out


def _optional(module: str, name: str) -> Any:
    try:
        mod = importlib.import_module(module)
    except Exception:
        return None
    return getattr(mod, name, None)


def _cfg(config: Any, key: str, default: Any = None) -> Any:
    try:
        value = config.get(key, default)
    except Exception:
        return default
    return default if value is None else value


def _default_cache_file() -> Path:
    from xyrus import paths
    return Path(paths.DATA_DIR) / "vocab_cache.json"


# --------------------------------------------------------------------------- Vocab
class Vocab:
    """Which words the Vosk model knows. Source order: graph/words.txt (absent in the small model),
    the JSON cache (data/vocab_cache.json, keyed by model path), then the attached checker
    (`Recognizer.check_words`). A word that cannot be verified yet counts as known (never dropped
    blindly) and is not cached."""

    def __init__(self, model_dir: Path, *, cache_file: Path | None = None,
                 checker: Callable[[list[str]], dict[str, bool]] | None = None):
        self.model_dir = Path(model_dir)
        self._lock = threading.RLock()
        self._checker = checker
        self._cache_file = Path(cache_file) if cache_file else _default_cache_file()
        self._cache: dict[str, bool] = {}
        self._words_txt: set[str] | None = None
        words_txt = self.model_dir / "graph" / "words.txt"
        if words_txt.is_file():
            try:
                with open(words_txt, encoding="utf8") as f:
                    self._words_txt = {line.split()[0] for line in f if line.strip()}
            except OSError as e:
                log.warning("cannot read %s: %s", words_txt, e)
        self._load_cache()

    # SPEC-AMBIGUITY: §4.12 gives Vocab(model_dir) only; the recognizer's check_words is attached
    # after both exist (app.py: vocab.attach(recognizer.check_words)).
    def attach(self, checker: Callable[[list[str]], dict[str, bool]] | None) -> None:
        with self._lock:
            self._checker = checker

    def _load_cache(self) -> None:
        try:
            data = json.loads(self._cache_file.read_text(encoding="utf8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and data.get("model") == str(self.model_dir) \
                and isinstance(data.get("words"), dict):
            self._cache = {str(k): bool(v) for k, v in data["words"].items()}

    def _save_cache(self) -> None:
        tmp = self._cache_file.with_name(self._cache_file.name + ".tmp")
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"model": str(self.model_dir), "words": self._cache}, sort_keys=True),
                           encoding="utf8")
            os.replace(tmp, self._cache_file)                          # G7
        except OSError as e:
            log.warning("vocab cache not saved: %s", e)

    def check(self, words: Iterable[str]) -> dict[str, bool]:
        """Batch lookup: word -> known. One checker round trip for all uncached words."""
        wanted = list(dict.fromkeys(w.lower() for w in words if w))
        result: dict[str, bool] = {}
        missing: list[str] = []
        with self._lock:
            for w in wanted:
                if w == UNK:
                    result[w] = True
                elif self._words_txt is not None:
                    result[w] = w in self._words_txt
                elif w in self._cache:
                    result[w] = self._cache[w]
                else:
                    missing.append(w)
            checker = self._checker
        if missing:
            answers: dict[str, bool] = {}
            if checker is not None:
                try:
                    answers = {k.lower(): bool(v) for k, v in checker(missing).items()}
                except Exception as e:               # TimeoutError while the model loads, etc.
                    log.warning("vocabulary check failed (%s); %d word(s) unverified", e, len(missing))
            with self._lock:
                fresh = {w: answers[w] for w in missing if w in answers}
                if fresh:
                    self._cache.update(fresh)
                    self._save_cache()
            for w in missing:
                result[w] = answers.get(w, True)
        return result

    def known(self, word: str) -> bool:
        return self.check([word]).get(word.lower(), True)

    def unknown_words(self, phrase: str) -> list[str]:
        toks = [t for t in dict.fromkeys(tokens(phrase)) if t != UNK]
        known = self.check(toks)
        return [t for t in toks if not known.get(t, True)]


# --------------------------------------------------------------------------- GrammarBuilder
class GrammarBuilder:
    """Word list per ListenMode (§4.12). `words()` is memoised per (mode, extra, version())."""

    def __init__(self, registry: Any, config: Any, vocab: Vocab):
        self.registry = registry
        self.config = config
        self.vocab = vocab
        self._lock = threading.RLock()
        self._memo: dict[tuple, tuple[list[str], list[str]]] = {}
        self._memo_version: tuple | None = None
        self._app_memo: dict[str, tuple[list[str], dict[str, str]]] = {}
        self._logged: set[str] = set()

    # ---- version ---------------------------------------------------------
    def version(self) -> tuple:
        rv = getattr(self.registry, "version", 0)
        try:
            rv = rv() if callable(rv) else rv
        except Exception:
            rv = 0
        blob = json.dumps([_cfg(self.config, "wake_words", []), _cfg(self.config, "apps", {}),
                           _cfg(self.config, "custom_commands", [])], sort_keys=True, default=str)
        return (rv, hashlib.md5(blob.encode("utf8")).hexdigest()[:12])

    # ---- word sets -------------------------------------------------------
    def wake_words(self) -> list[str]:
        return [w.lower() for w in _cfg(self.config, "wake_words", list(_norm.WAKE_ALIASES))]

    @staticmethod
    def answer_words() -> set[str]:
        return _split_all(_norm.YES_WORDS) | _split_all(_norm.NO_WORDS) | _split_all(_norm.CANCEL_WORDS)

    @staticmethod
    def synonym_words() -> set[str]:
        # T1's meaning layer maps heard synonyms onto registered phrases; those words must be decodable.
        # SPEC-AMBIGUITY: the export name is not fixed yet - SYNONYM_WORDS (iterable) or SYNONYMS (mapping).
        for name in ("SYNONYM_WORDS", "SYNONYMS"):
            value = getattr(_norm, name, None)
            if value is not None:
                return _split_all(value)
        return set()

    @staticmethod
    def date_words() -> set[str]:
        return _split_all(_optional("xyrus.dates", "DATE_WORDS") or _DATE_FALLBACK)

    @staticmethod
    def time_words() -> set[str]:
        return _split_all(_optional("xyrus.dates", "TIME_WORDS") or _TIME_FALLBACK)

    def _slot_words(self) -> set[str]:
        from xyrus.parsing import SLOT_TYPES
        try:
            used = set(self.registry.slot_types_used())
        except Exception:
            log.exception("registry.slot_types_used failed")
            used = set()
        out: set[str] = set()
        for name in sorted(used):
            st = SLOT_TYPES.get(name)
            if st is None:
                if f"slot:{name}" not in self._logged:
                    self._logged.add(f"slot:{name}")
                    log.warning("grammar: slot type %r has no SLOT_TYPES entry", name)
                continue
            out |= _split_all(st.words)
        return out

    def _app_words(self) -> set[str]:
        return _split_all(list((_cfg(self.config, "apps", {}) or {}).keys()))

    def _custom_words(self) -> set[str]:
        phrases = []
        for c in _cfg(self.config, "custom_commands", []) or []:
            if isinstance(c, dict) and c.get("enabled", True) and c.get("phrase"):
                phrases.append(c["phrase"])
        return _split_all(phrases)

    def raw_words(self, mode: str, extra: Iterable[str] = ()) -> set[str]:
        """The mode's word set before the vocabulary filter (no "[unk]")."""
        if mode in ("free", "paused"):
            return set()
        words = set(self.wake_words()) | _split_all(list(extra))
        if mode == "wake":
            pass
        elif mode == "command":
            try:
                literal = set(self.registry.literal_words())
            except Exception:
                log.exception("registry.literal_words failed")
                literal = set()
            words |= _split_all(literal) | self._slot_words() | self._app_words() | self._custom_words()
            words |= self.date_words() | self.answer_words() | self.synonym_words()
        elif mode == "confirm":
            words |= self.answer_words() | set(CONFIRM_WORDS)
        elif mode == "when":
            words |= self.date_words() | self.time_words() | set(NUMBER_WORDS) | set(ORDINAL_WORDS)
            words |= _split_all(_norm.CANCEL_WORDS) | set(WHEN_WORDS)
        else:
            raise ValueError(f"unknown listen mode {mode!r}")
        words.discard(UNK)
        return words

    def _compute(self, mode: str, extra: Iterable[str]) -> tuple[list[str], list[str]]:
        extra_key = tuple(sorted(_split_all(list(extra))))
        version = self.version()
        with self._lock:
            if version != self._memo_version:
                self._memo.clear()
                self._memo_version = version
            hit = self._memo.get((mode, extra_key))
        if hit is not None:
            return hit
        raw = self.raw_words(mode, extra_key)
        known = self.vocab.check(raw) if raw else {}
        dropped = sorted(w for w in raw if not known.get(w, True))
        for w in dropped:
            if w not in self._logged:
                self._logged.add(w)
                log.warning("grammar: %r is not in the speech model - dropped (mode %s)", w, mode)
        kept = sorted(w for w in raw if known.get(w, True))
        result = (kept + [UNK] if raw else [], dropped)
        with self._lock:
            self._memo[(mode, extra_key)] = result
        return result

    def app_grammar(self, names: Iterable[str], prefix_words: Iterable[str] = ()) -> tuple[list[str], dict[str, str]]:
        """Grammar for the <app> re-decode (lead, round 2): wake words + the app-trigger words + every app name as
        a phrase (words the model doesn't know are removed from it), "[unk]" last.
        Returns (grammar, heard phrase -> app name)."""
        names = sorted({str(n).lower().strip() for n in names if str(n).strip()})
        prefix = sorted(_split_all(list(prefix_words)) | set(self.wake_words()))
        key = hashlib.md5(json.dumps([names, prefix, self.version()], default=str).encode("utf8")).hexdigest()
        with self._lock:
            hit = self._app_memo.get(key)
        if hit is not None:
            return list(hit[0]), dict(hit[1])
        known = self.vocab.check(set(prefix) | _split_all(names))
        phrase_to_name: dict[str, str] = {}
        for name in names:
            toks = [t for t in tokens(name) if t != UNK and known.get(t, True)]
            if toks:
                phrase_to_name.setdefault(" ".join(toks), name)
        kept_prefix = [w for w in prefix if known.get(w, True)]
        grammar = kept_prefix + sorted(set(phrase_to_name) - set(kept_prefix)) + [UNK]
        with self._lock:
            if len(self._app_memo) > 8:
                self._app_memo.clear()
            self._app_memo[key] = (grammar, phrase_to_name)
        return list(grammar), dict(phrase_to_name)

    def words(self, mode: str, extra: Iterable[str] = ()) -> list[str]:
        """Sorted JSON-ready list ending with "[unk]"; unknown words dropped and logged once. free -> []"""
        return list(self._compute(mode, extra)[0])

    def dropped(self, mode: str, extra: Iterable[str] = ()) -> list[str]:
        """Words of the mode that the model does not know (they were silently removed from `words`)."""
        return list(self._compute(mode, extra)[1])
