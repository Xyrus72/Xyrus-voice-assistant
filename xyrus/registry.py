"""Commands, patterns and matching (§4.5). The ONE decorator every commands/*.py uses is `command`.

Pattern syntax: `word` literal; `[word]` optional; `a|an` alternatives (one required); `[a|an]` optional
alternatives; `<type>` slot from parsing.SLOT_TYPES.

Matching runs in two passes (full scope only for the second):
1. exact - the §4.5 regex over the normalised text (pre/post extra words allowed);
2. meaning - normalize.canonical_items() applied to both the utterance and every pattern text, so paraphrases
   ("can you lower the sound", "the volume, lower it") reach the same command as "volume down" (lead
   requirement 2026-09-13, overrides exact-phrase-only matching).
An exact match whose extra words are all fillers wins; otherwise the pass with fewer meaningful extra words.
Destructive commands never accept an unknown extra word ("shut down chrome" is not a shutdown).
"""
from __future__ import annotations

import bisect
import dataclasses
import difflib
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal

from xyrus import normalize as N
from xyrus.parsing import SLOT_TYPES, SlotEnv

log = logging.getLogger("xyrus.registry")

SECTION_ORDER = ("Basics", "Power", "Sound & media", "Display & windows", "Apps & web", "Keys & clipboard",
                 "Timers", "Calendar & reminders", "Assistant", "Info & tools", "Your commands")
SLOTS_SECTION = "Slots"
FREE_CAPABLE = ("song", "query", "text", "app")
WAKE_FREE_NOTE = " — works without the wake word"
# extra words that never change the meaning of a command
_HARMLESS = N.FILLER_WORDS | N.ARTICLES | N.WAKE_LIKE | {"[unk]", "@small", "@big", "now", "right", "away",
                                                        "immediately", "this", "that", "okay", "ok"}
_DESTRUCTIVE_OK = _HARMLESS | N.PC_WORDS
_ARTICLE_LEAD = frozenset({"the", "a", "an", "my", "your", "our", "some", "this", "that"})
# Hearing rebuild (Sep 15 2026): commands that interrupt what the user is doing without being destructive
# (the PC suspends, the screen goes dark, a window closes, the mic goes off). Marked at registration so the
# command modules stay untouched; the fuzzy tiers only ever ASK about these ("Did you mean sleep?").
DISRUPTIVE_NAMES = frozenset({"sleep", "hibernate", "lock", "screen_off", "close_window", "close_app",
                              "pause_listening"})
# fuzzy tiers (Registry.close_phrase_scored): ratio = max(raw vs raw, filler-stripped vs filler-stripped)
_FUZZY_STRIP = N.FILLER_WORDS | N.ARTICLES
# Registry.fuzzy_tier: a harmless command runs at >= FUZZY_RUN with FUZZY_MARGIN over the runner-up, is asked
# about from FUZZY_ASK; a destructive / disruptive one is only ever asked about, from FUZZY_ASK_GUARDED and
# only when the heard words sound the same (normalize.phonetic_key).
FUZZY_RUN, FUZZY_MARGIN, FUZZY_ASK, FUZZY_ASK_GUARDED = 0.90, 0.05, 0.75, 0.88
_VOWELS = frozenset("aeiou")

Scope = Literal["full", "idle", "follow_up"]


@dataclass(frozen=True)
class Pattern:
    text: str
    regex: re.Pattern
    slots: tuple[str, ...]
    literal_count: int
    free_slot: str | None
    # additive (not in §4.5): parsed tokens, used for free-slot recomputation and close_phrase
    tokens: tuple[tuple[str, Any], ...] = ()
    body: re.Pattern | None = None            # body + post only (no pre group), for re-tries at other offsets


@dataclass(frozen=True)
class Command:
    name: str
    patterns: tuple[Pattern, ...]
    handler: Callable[..., None]
    section: str
    help: str
    destructive: bool = False
    wake_free: bool = False
    context: Callable[[Any], bool] | None = None
    follow_up: bool = False
    armed_ok_single_word: bool = True
    matcher: Callable[[list[str]], dict | None] | None = None
    matcher_score: int = 0
    priority: int = 0
    custom: bool = False
    free_trigger_words: tuple[str, ...] = ()
    wake_free_note: str = ""                  # additive: Commands-tab suffix, e.g. " — works without the wake word while a timer runs"
    disruptive: bool = False                  # additive: never run from a fuzzy match (DISRUPTIVE_NAMES)


@dataclass(frozen=True)
class Match:
    command: Command
    pattern: Pattern | None
    slots: dict[str, Any]
    text: str
    has_unk: bool
    extra_words: int


# ----------------------------------------------------------------------------- compilation
def _parse_tokens(text: str) -> tuple[tuple[str, Any], ...]:
    toks = []
    for raw in text.strip().lower().split():
        if raw.startswith("<") and raw.endswith(">"):
            name = raw[1:-1]
            if name not in SLOT_TYPES:
                raise ValueError(f"unknown slot type <{name}> in pattern {text!r}")
            toks.append(("slot", name))
        elif raw.startswith("[") and raw.endswith("]"):
            toks.append(("opt", tuple(raw[1:-1].split("|"))))
        else:
            toks.append(("lit", tuple(raw.split("|"))))
    if not toks:
        raise ValueError("empty pattern")
    if not any(k == "lit" for k, _ in toks):
        raise ValueError(f"pattern {text!r} needs at least one required literal word")
    if toks[0][0] == "slot" and not any(k == "lit" for k, _ in toks[1:]):
        raise ValueError(f"pattern {text!r} starts with a slot and has no literal after it")
    return tuple(toks)


def compile_pattern(text: str) -> Pattern:
    toks = _parse_tokens(text)
    body = ""
    slots: list[str] = []
    need_space = False                      # whether the next required piece needs a leading space
    last = len(toks) - 1
    for i, (kind, val) in enumerate(toks):
        if kind == "slot":
            piece = f"(?P<s{len(slots)}>.+)" if i == last else f"(?P<s{len(slots)}>.+?)"
            slots.append(val)
        else:
            alts = "|".join(re.escape(a) for a in val)
            piece = f"(?:{alts})"
        if kind == "opt":
            body += f"(?: {piece})?" if need_space else f"(?:{piece} )?"
        else:
            body += (" " if need_space else "") + piece
            need_space = True
    literal_count = sum(1 for k, _ in toks if k == "lit")
    free_slot = None
    # SPEC-AMBIGUITY: §4.5 says "if the LAST token is a free-capable slot". Generalised to: the last slot is
    # free-capable and only literal words follow it ("play <song> on spotify", "put <text> on my to do list");
    # the trailing literal words are stripped from the free re-decode when present.
    slot_positions = [i for i, (k, _) in enumerate(toks) if k == "slot"]
    if slot_positions:
        sp = slot_positions[-1]
        if toks[sp][1] in FREE_CAPABLE and all(k != "slot" for k, _ in toks[sp + 1:]):
            free_slot = toks[sp][1]
    regex = re.compile(r"^(?P<pre>(?:\S+ )*?)" + body + r"(?P<post>(?: \S+)*)$")
    body_re = re.compile(body + r"(?P<post>(?: \S+)*)$")
    return Pattern(text=text, regex=regex, slots=tuple(slots), literal_count=literal_count,
                   free_slot=free_slot, tokens=toks, body=body_re)


def _slot_keys(slots: tuple[str, ...]) -> list[str]:
    """Slot dict keys: the type name; a repeated type gets a numeric suffix (number, number2)."""
    seen: dict[str, int] = {}
    keys = []
    for s in slots:
        seen[s] = seen.get(s, 0) + 1
        keys.append(s if seen[s] == 1 else f"{s}{seen[s]}")
    return keys


def display_phrase(p: Pattern) -> str:
    """Pattern text without optional words, first alternative only ('set [the] volume' -> 'set volume')."""
    out = []
    for kind, val in p.tokens:
        if kind == "lit":
            out.append(val[0])
        elif kind == "slot":
            out.append(f"<{val}>")
    return " ".join(out)


def meaningful(tokens: Iterable[str]) -> list[str]:
    return [t for t in tokens if t not in _HARMLESS]


# ----------------------------------------------------------------------------- registry
class Registry:
    def __init__(self):
        self._lock = threading.RLock()
        self._cmds: dict[str, Command] = {}
        self._order: dict[str, int] = {}
        self._seq = 0
        self._version = 0
        self._canon_cache: dict[str, Pattern | None] = {}
        self._fuzzy_cache: tuple[int, list] | None = None

    # ---- registration
    def command(self, name: str, *phrases: str, section: str, help: str, **opts) -> Callable:
        def deco(fn):
            pats = tuple(compile_pattern(p) for p in phrases)
            self.register(Command(name=name, patterns=pats, handler=fn, section=section, help=help, **opts))
            return fn
        return deco

    def register(self, cmd: Command) -> None:
        if not cmd.patterns and cmd.matcher is None:
            raise ValueError(f"command {cmd.name} has neither patterns nor a matcher")
        if cmd.name in DISRUPTIVE_NAMES and not cmd.disruptive:
            cmd = dataclasses.replace(cmd, disruptive=True)
        with self._lock:
            if cmd.name not in self._order:          # re-registering keeps the original order (idempotent reload)
                self._seq += 1
                self._order[cmd.name] = self._seq
            self._cmds[cmd.name] = cmd
            self._version += 1

    def unregister(self, name: str) -> None:
        with self._lock:
            if self._cmds.pop(name, None) is not None:
                self._order.pop(name, None)
                self._version += 1

    def unregister_custom(self) -> None:
        with self._lock:
            names = [n for n, c in self._cmds.items() if c.custom]
            for n in names:
                del self._cmds[n]
                self._order.pop(n, None)
            if names:
                self._version += 1

    # ---- read side
    def commands(self) -> list[Command]:
        with self._lock:
            return sorted(self._cmds.values(), key=lambda c: self._order[c.name])

    def get(self, name: str) -> Command | None:
        with self._lock:
            return self._cmds.get(name)

    def version(self) -> int:
        with self._lock:
            return self._version

    def counts(self) -> tuple[int, int]:
        """(commands, patterns) for the Commands tab label 'N commands · M ways to say them'."""
        cmds = self.commands()
        return len(cmds), sum(len(c.patterns) for c in cmds)

    def literal_words(self) -> set[str]:
        """Every literal/optional word of every pattern + every word the meaning layer understands."""
        words: set[str] = set(N.SYNONYM_WORDS)
        for c in self.commands():
            for p in c.patterns:
                for kind, val in p.tokens:
                    if kind in ("lit", "opt"):
                        for alt in val:
                            words.update(alt.split())
        return words

    def slot_types_used(self) -> set[str]:
        return {s for c in self.commands() for p in c.patterns for s in p.slots}

    def free_triggers(self) -> list[tuple[str, ...]]:
        """Literal prefixes of patterns whose last slot is free (song/query/text; app is handled by the
        recognizer's open|launch|start + [unk] rule), their first word alone, every free_trigger_words entry,
        and the meaning-layer synonyms of play/search. 'play' also yields every PLAY_LIKE sound-alike."""
        out: list[tuple[str, ...]] = []
        seen: set[tuple[str, ...]] = set()

        def add(t: tuple[str, ...]):
            if t and t not in seen:
                seen.add(t)
                out.append(t)

        for c in self.commands():
            for p in c.patterns:
                if p.free_slot in ("song", "query", "text"):
                    prefix: list[str] = []
                    for kind, val in p.tokens:
                        if kind == "slot":
                            break
                        if kind == "lit":
                            prefix.append(val[0])
                    add(tuple(prefix))
                    if prefix:
                        add((prefix[0],))
                    if prefix and prefix[0] == "play":
                        for alt in sorted(N.PLAY_LIKE):
                            add((alt,) + tuple(prefix[1:]))
            for w in c.free_trigger_words:
                add(tuple(w.split()))
        for syn in (("put", "on"), ("listen", "to"), ("look", "up"), ("search", "up")):
            add(syn)
        return out

    # ---- Commands tab
    def sections(self, cfg=None) -> list[tuple[str, list[tuple[str, str]]]]:
        by: dict[str, list[tuple[str, str]]] = {s: [] for s in SECTION_ORDER}
        extra: dict[str, list[tuple[str, str]]] = {}
        for c in self.commands():
            if not c.patterns:
                continue
            phrases = " / ".join(p.text for p in c.patterns)
            help_text = c.help
            if c.wake_free:
                help_text += c.wake_free_note or WAKE_FREE_NOTE
            target = by if c.section in by else extra
            target.setdefault(c.section, []).append((phrases, help_text))
        ordered = [(s, rows) for s, rows in by.items() if rows and s != "Your commands"]
        ordered += [(s, rows) for s, rows in extra.items() if rows]
        if by["Your commands"]:
            ordered.append(("Your commands", by["Your commands"]))
        legend = [(f"<{name}>", SLOT_TYPES[name].doc) for name in SLOT_TYPES if name in self.slot_types_used()]
        if legend:
            ordered.append((SLOTS_SECTION, legend))
        return ordered

    # ---- matching
    def _eligible(self, scope: Scope, engine: Any, follow_up: Iterable[str] | None):
        wl = set(follow_up or ())
        for c in self.commands():
            if scope == "full":
                yield c, None
            elif scope == "idle":
                if not c.wake_free:
                    continue
                try:
                    ok = c.context is None or bool(c.context(engine))
                except Exception:
                    log.exception("context predicate of %s failed", c.name)
                    ok = False
                if ok:
                    yield c, None
            elif scope == "follow_up":
                if not c.follow_up:
                    continue
                # whitelist entries are command names (all patterns live) or pattern texts (only those)
                if c.name in wl:
                    yield c, None
                else:
                    allowed = {p.text for p in c.patterns if p.text in wl}
                    if allowed:
                        yield c, allowed

    def match(self, text: str, *, free_text: str | None = None, scope: Scope = "full",
              env: SlotEnv | None = None, engine: Any = None,
              follow_up: Iterable[str] | None = None) -> Match | None:
        """Best match for (normalised, wake-stripped) text. `engine` feeds the idle-scope context predicates;
        `follow_up` is the engine's current whitelist (command names or pattern texts)."""
        text = " ".join(text.split())
        if not text:
            return None
        if env is None:
            import datetime as _dt
            env = SlotEnv(config=None, now=_dt.datetime(2000, 1, 1), free_text=free_text)
        tokens = text.split()
        step = N.step_modifier(tokens)
        exact, exact_meaningful = self._best_exact(text, tokens, free_text, scope, env, engine, follow_up)
        if scope != "full" or (exact is not None and exact_meaningful == 0):
            return self._with_step(exact, step)
        canon, canon_meaningful = self._best_canonical(text, tokens, free_text, env)
        if canon is None:
            best = exact
        elif exact is None or canon_meaningful < exact_meaningful:
            best = canon
        else:
            best = exact
        return self._with_step(best, step)

    @staticmethod
    def _with_step(m: Match | None, step: str | None) -> Match | None:
        if m is None or step is None or "step" in m.slots:
            return m
        return Match(m.command, m.pattern, {**m.slots, "step": step}, m.text, m.has_unk, m.extra_words)

    def _best_exact(self, text, tokens, free_text, scope, env, engine, follow_up):
        has_unk = "[unk]" in tokens
        variants = [text]
        if has_unk:
            stripped = " ".join(w for w in tokens if w != "[unk]")
            if stripped:
                variants.append(stripped)
        best_key = None
        best: Match | None = None
        best_meaningful = 0
        for cmd, allowed in self._eligible(scope, engine, follow_up):
            order = self._order.get(cmd.name, 0)
            for pat in cmd.patterns:
                if allowed is not None and pat.text not in allowed:
                    continue
                for vtext in variants:
                    hit = self._try(pat, vtext, free_text, env)
                    if hit is None:
                        continue
                    slots, extra_toks = hit
                    if cmd.destructive and any(t not in _DESTRUCTIVE_OK for t in extra_toks):
                        break                   # "shut down chrome": an unknown object is never a shutdown
                    key = (pat.literal_count, -len(extra_toks), cmd.priority, -order)
                    if best_key is None or key > best_key:
                        best_key = key
                        best = Match(cmd, pat, slots, text, has_unk, len(extra_toks))
                        best_meaningful = len(meaningful(extra_toks))
                    break                       # the as-is variant wins over the [unk]-stripped one
            if cmd.matcher is not None and allowed is None:
                try:
                    slots = cmd.matcher(list(tokens))
                except Exception:
                    log.exception("matcher of %s failed", cmd.name)
                    slots = None
                if slots is not None:
                    slots = dict(slots)
                    if free_text is not None:
                        slots.setdefault("free_text", free_text)
                    key = (cmd.matcher_score, 0, cmd.priority, -order)
                    if best_key is None or key > best_key:
                        best_key = key
                        best = Match(cmd, None, slots, text, has_unk, 0)
                        best_meaningful = 0
        return best, best_meaningful

    def _try(self, pat: Pattern, text: str, free_text: str | None, env: SlotEnv):
        """-> (slots, extra tokens) or None."""
        m = pat.regex.match(text)
        if m is None:
            return None
        slots = self._parse_slots(pat, [m.group(f"s{i}").split() for i in range(len(pat.slots))], free_text, env)
        if slots is not None:
            return slots, m.group("pre").split() + m.group("post").split()
        # the first regex split failed slot parsing: retry the body at later word offsets
        words = text.split()
        for k in range(1, len(words)):
            m2 = pat.body.match(" ".join(words[k:]))
            if m2 is None:
                continue
            caps = [m2.group(f"s{i}").split() for i in range(len(pat.slots))]
            slots = self._parse_slots(pat, caps, free_text, env)
            if slots is not None:
                return slots, words[:k] + m2.group("post").split()
        return None

    def _parse_slots(self, pat: Pattern, captures: list[list[str]], free_text: str | None,
                     env: SlotEnv) -> dict | None:
        out: dict[str, Any] = {}
        keys = _slot_keys(pat.slots)
        last_slot = len(pat.slots) - 1
        for idx, (stype, key) in enumerate(zip(pat.slots, keys)):
            toks = list(captures[idx])
            if (idx == last_slot and pat.free_slot == stype and free_text
                    and (stype != "app" or "[unk]" in toks)):
                free_toks = self._free_value(pat, free_text)
                if free_toks:
                    toks = free_toks
            toks = [t for t in toks if t != "[unk]"]
            if not toks:
                return None
            try:
                val = SLOT_TYPES[stype].parse(toks, env)
            except Exception:
                log.exception("slot parser <%s> failed on %r", stype, toks)
                val = None
            if val is None:
                return None
            out[key] = val
        return out

    # ---- meaning pass
    def _canon(self, pat: Pattern) -> Pattern | None:
        with self._lock:
            if pat.text in self._canon_cache:
                return self._canon_cache[pat.text]
        cp = None
        if len(pat.slots) <= 1:
            words = []
            for kind, val in pat.tokens:
                if kind == "slot":
                    words.append(f"<{val}>")
                elif kind == "opt":
                    if all(a in N.FILLER_WORDS or a in N.ARTICLES for a in val):
                        continue
                    words.append("[" + "|".join(val) + "]")
                else:
                    words.append(val[0] if len(val) == 1 else "|".join(val))
            ctext = " ".join(t for t, _ in N.canonical_items(words))
            if ctext:
                try:
                    cp = compile_pattern(ctext)
                except ValueError:
                    cp = None
            if cp is not None and cp.slots != pat.slots:
                cp = None
        with self._lock:
            self._canon_cache[pat.text] = cp
        return cp

    def _best_canonical(self, text: str, tokens: list[str], free_text: str | None, env: SlotEnv):
        orig = [t for t in tokens if t != "[unk]"]
        items = N.canonical_items(orig)
        if not items:
            return None, 0
        ctoks = [t for t, _ in items]
        ctext = " ".join(ctoks)
        starts, pos = [], 0
        for t in ctoks:
            starts.append(pos)
            pos += len(t) + 1
        has_unk = "[unk]" in tokens
        best_key, best, best_meaningful = None, None, 0
        for cmd, _ in self._eligible("full", None, None):
            order = self._order.get(cmd.name, 0)
            for pat in cmd.patterns:
                cp = self._canon(pat)
                if cp is None:
                    continue
                m = cp.regex.match(ctext)
                if m is None:
                    continue
                extra_toks = m.group("pre").split() + m.group("post").split()
                if cmd.destructive and any(t not in _DESTRUCTIVE_OK for t in extra_toks):
                    continue
                captures = []
                for idx in range(len(cp.slots)):
                    s, e = m.span(f"s{idx}")
                    ti = bisect.bisect_right(starts, s) - 1
                    tj = bisect.bisect_right(starts, e - 1) - 1
                    a, b = items[ti][1][0], items[tj][1][1]
                    prev_end = items[ti - 1][1][1] if ti > 0 else 0
                    while a > prev_end and orig[a - 1] in _ARTICLE_LEAD:
                        a -= 1                   # keep "the" in "search for the best pizza"
                    if cp.tokens and cp.tokens[-1][0] == "slot" and idx == len(cp.slots) - 1:
                        b = len(orig)
                    captures.append(orig[a:b])
                slots = self._parse_slots(pat, captures, free_text, env)
                if slots is None:
                    continue
                key = (cp.literal_count, -len(extra_toks), cmd.priority, -order)
                if best_key is None or key > best_key:
                    best_key = key
                    best = Match(cmd, pat, slots, text, has_unk, len(extra_toks))
                    best_meaningful = len(meaningful(extra_toks))
        return best, best_meaningful

    @staticmethod
    def _free_value(pat: Pattern, free_text: str) -> list[str]:
        """Slot tokens recomputed from the unrestricted re-decode (§4.5 'Free slots')."""
        words = N.normalize(free_text).split()
        while words and words[0] in N.WAKE_LIKE:
            words = words[1:]
        prefix: list[tuple[str, Any]] = []
        suffix: list[str] = []
        seen_slot = False
        for kind, val in pat.tokens:
            if kind == "slot":
                if seen_slot or val == pat.free_slot:
                    seen_slot = True
                    continue
            if seen_slot:
                if kind == "lit":
                    suffix.append(val[0])
                continue
            if kind != "slot":
                prefix.append((kind, val))
        req = [v for k, v in prefix if k == "lit"]
        first_alts = set(req[0]) if req else set()
        if req and "play" in first_alts:
            first_alts |= N.PLAY_LIKE
        start = None
        for i, w in enumerate(words[:3]):
            if w in first_alts:
                start = i
                break
        if start is None:
            # No literal anchor in the re-decode: the free text is some other sentence ("put on shape of
            # you" heard against "play <song>" gave the slot "on shape of you" when the first literal was
            # blindly dropped - hearing rebuild). The grammar's own slot words stay.
            return []
        j = start
        for kind, val in prefix:
            if j >= len(words):
                break
            if words[j] in val or (kind == "lit" and val is req[0] and words[j] in first_alts):
                j += 1
            elif kind == "lit":
                j += 1                          # misheard literal: consume one word
        rest = words[j:]
        if suffix and len(rest) > len(suffix) and rest[-len(suffix):] == suffix:
            rest = rest[: -len(suffix)]
        return rest

    def close_phrase(self, text: str) -> str | None:
        """Best literal (slot-free) phrase with difflib ratio >= 0.75, or None."""
        t = " ".join(w for w in text.split() if w != "[unk]")
        if not t:
            return None
        best, best_r = None, 0.0
        for c in self.commands():
            for p in c.patterns:
                if p.slots:
                    continue
                phrase = display_phrase(p)
                r = difflib.SequenceMatcher(None, t, phrase).ratio()
                if r > best_r:
                    best, best_r = phrase, r
        return best if best_r >= 0.75 else None

    # ---- fuzzy tiers (hearing rebuild, Sep 15 2026)
    def _fuzzy_candidates(self) -> list[tuple[str, Command, bool]]:
        """(phrase, command, is_prefix): every slot-free display phrase, plus the literal prefix of every slot
        pattern ("start" of "start <app>") - prefixes compete for the ranking (so "the start" is not a fuzzy
        "restart") but can never run or be asked about, they are not whole commands."""
        with self._lock:
            cached = getattr(self, "_fuzzy_cache", None)
            if cached is not None and cached[0] == self._version:
                return cached[1]
        out: list[tuple[str, Command, bool]] = []
        seen: set[tuple[str, str]] = set()
        for c in self.commands():
            for p in c.patterns:
                if p.slots:
                    prefix: list[str] = []
                    for kind, val in p.tokens:
                        if kind == "slot":
                            break
                        if kind == "lit":
                            prefix.append(val[0])
                    phrase, is_prefix = " ".join(prefix), True
                else:
                    phrase, is_prefix = display_phrase(p), False
                if phrase and (phrase, c.name) not in seen:
                    seen.add((phrase, c.name))
                    out.append((phrase, c, is_prefix))
        with self._lock:
            self._fuzzy_cache = (self._version, out)
        return out

    @staticmethod
    def _fuzzy_ratio(text: str, stripped: str, phrase: str) -> float:
        r = difflib.SequenceMatcher(None, text, phrase).ratio()
        ps = " ".join(w for w in phrase.split() if w not in _FUZZY_STRIP)
        if stripped and ps:
            r = max(r, difflib.SequenceMatcher(None, stripped, ps).ratio())
        return r

    def close_phrase_scored(self, text: str, limit: int = 8) -> list[tuple[str, float, Command]]:
        """Fuzzy ranking of an unmatched utterance: [(phrase, ratio, Command)] best first. ratio = max(raw vs
        raw, filler-stripped vs filler-stripped) over the slot-free display phrases and the literal prefixes
        of slot patterns (see _fuzzy_candidates; a prefix entry's phrase is not a whole command - the engine
        checks `phrase in registry.fuzzy_prefixes()`). Whole phrases rank before prefixes at equal ratio."""
        t = " ".join(w for w in text.split() if w != "[unk]")
        if not t:
            return []
        stripped = " ".join(w for w in t.split() if w not in _FUZZY_STRIP)
        scored = []
        for i, (phrase, cmd, is_prefix) in enumerate(self._fuzzy_candidates()):
            r = self._fuzzy_ratio(t, stripped, phrase)
            if r > 0:
                scored.append((-r, is_prefix, i, phrase, cmd))
        scored.sort()
        return [(phrase, -nr, cmd) for nr, _, _, phrase, cmd in scored[:limit]]

    def fuzzy_tier(self, text: str) -> tuple[str, str, Command] | None:
        """What to do with an unmatched utterance: ("run" | "ask", phrase, Command) or None.
        The best entry decides (a lower one never takes over: "we start" is not asked as "when is the restart").
        - a slot-pattern prefix on top ("the start" -> "start <app>") -> None: the request is incomplete;
        - destructive / disruptive -> "ask" only, at >= FUZZY_ASK_GUARDED with the same phonetic key as one of
          the command's phrases ("shot down" -> shut down, "asleep" -> sleep; "power of" is not "power off");
        - otherwise "run" at >= FUZZY_RUN with FUZZY_MARGIN over the runner-up (same-letters phrases such as
          "take a screenshot" / "take a screen shot" count once) when no letter of the phrase was lost - a
          dropped letter can make another word ("flip a con"), a split word or one swapped vowel can't ("show
          desk top", "take a screen shut"); "ask" at >= FUZZY_ASK with FUZZY_MARGIN over any other command."""
        scored = self.close_phrase_scored(text, limit=40)
        if not scored:
            return None
        t = " ".join(w for w in text.split() if w != "[unk]")
        prefixes = self.fuzzy_prefixes()
        top_p, top_r, top_c = scored[0]
        if (top_p, top_c.name) in prefixes:
            return None
        if top_c.destructive or top_c.disruptive:
            key = N.phonetic_key(t)
            for p, r, c in scored:
                if r < FUZZY_ASK_GUARDED:
                    break
                if c.name == top_c.name and (p, c.name) not in prefixes and N.phonetic_key(p) == key:
                    return "ask", p, c
            return None
        flat = top_p.replace(" ", "")
        runner_any = max((r for p, r, _c in scored[1:] if p.replace(" ", "") != flat), default=0.0)
        runner_other = max((r for _p, r, c in scored[1:] if c.name != top_c.name), default=0.0)
        if top_r >= FUZZY_RUN and top_r - runner_any >= FUZZY_MARGIN and _letters_kept(t, top_p):
            return "run", top_p, top_c
        if top_r >= FUZZY_ASK and top_r - runner_other >= FUZZY_MARGIN:
            return "ask", top_p, top_c
        return None

    def fuzzy_prefixes(self) -> set[tuple[str, str]]:
        """(phrase, command name) of the prefix-only entries of close_phrase_scored - never runnable on their
        own ("shut down" is a whole phrase of `shutdown` but only the prefix of close_app's "shut down <app>")."""
        return {(phrase, c.name) for phrase, c, is_prefix in self._fuzzy_candidates() if is_prefix}


def _letters_kept(text: str, phrase: str) -> bool:
    """Every letter of the phrase is in the heard text in order (a split word, an extra sound: "volume sup"),
    or the two differ by exactly one vowel ("take a screen shut"). Raw or filler-stripped."""
    def ok(a: str, b: str) -> bool:
        a, b = a.replace(" ", ""), b.replace(" ", "")
        if not a or not b:
            return False
        if len(a) == len(b):
            diff = [(x, y) for x, y in zip(a, b) if x != y]
            return len(diff) <= 1 and all(x in _VOWELS and y in _VOWELS for x, y in diff)
        it = iter(a)
        return all(ch in it for ch in b)

    def strip(s: str) -> str:
        return " ".join(w for w in s.split() if w not in _FUZZY_STRIP)
    return ok(text, phrase) or ok(strip(text), strip(phrase))


REGISTRY = Registry()
command = REGISTRY.command
