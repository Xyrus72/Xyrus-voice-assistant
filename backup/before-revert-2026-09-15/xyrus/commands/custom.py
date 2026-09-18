"""Custom commands and routines (§3.10): config.custom_commands -> registry ("Your commands"), a validator
for the Routines tab, and the step runner (open / run / keys / type / say / command / wait)."""
from __future__ import annotations

import itertools
import logging
import re
import time
from typing import Any

from xyrus import normalize as N
from xyrus import persona, replies as R
from xyrus.commands import say

log = logging.getLogger("xyrus.commands.custom")

SECTION = "Your commands"
ACTIONS = ("open", "run", "keys", "type", "say", "command", "wait")
MAX_WAIT_S = 30
TOTAL_TIMEOUT_S = 60
STEP_FAILED = "Step {n} of {name} failed{sir}."
ERR_SHORT = "Use at least two words, or one word of five letters or more."
ERR_WAKE = "Leave the wake word out of the phrase."
ERR_BUILTIN = "That's already a built-in command."
ERR_UNKNOWN_WORD = "The speech model doesn't know '{word}' — pick another word."
ERR_DUPLICATE = "Another routine already uses that phrase."

TEMPLATES = (
    {"phrase": "movie mode", "enabled": True, "steps": [
        {"action": "command", "arg": "set brightness to forty percent"},
        {"action": "command", "arg": "set volume to sixty percent"},
        {"action": "open", "arg": "https://www.netflix.com"}, {"action": "wait", "arg": 5},
        {"action": "keys", "arg": "f11"}]},
    {"phrase": "work mode", "enabled": True, "steps": [
        {"action": "open", "arg": "code"}, {"action": "open", "arg": "terminal"}, {"action": "open", "arg": "chrome"}]},
    {"phrase": "focus mode", "enabled": True, "steps": [
        {"action": "command", "arg": "mute"}, {"action": "say", "arg": "Focus mode on, sir."}]},
)


def clean_phrase(phrase: str) -> str:
    """Lowercase, letters/digits/apostrophes only, normalised like speech (§5.6)."""
    t = re.sub(r"[^a-z0-9' ]+", " ", (phrase or "").lower())
    return N.normalize(t)


def _expansions(pattern) -> set[str]:
    """Every literal phrase a slot-free pattern accepts (optional words on/off, each alternative)."""
    choices = []
    for kind, val in pattern.tokens:
        if kind == "lit":
            choices.append(list(val))
        elif kind == "opt":
            choices.append(list(val) + [""])
        else:
            return set()
    return {" ".join(w for w in combo if w) for combo in itertools.product(*choices)}


def builtin_phrases(registry) -> set[str]:
    out: set[str] = set()
    for c in registry.commands():
        if c.custom:
            continue
        for p in c.patterns:
            if not p.slots:
                out |= _expansions(p)
    return out


def validate(phrase: str, registry, vocab: Any = None, wake_words=None, *, others=()) -> str | None:
    """None if the phrase is usable, else the message the Routines tab shows."""
    p = clean_phrase(phrase)
    words = p.split()
    if not words or (len(words) == 1 and len(words[0]) < 5):
        return ERR_SHORT
    wakes = set(wake_words or N.WAKE_ALIASES) | N.WAKE_LIKE
    if any(w in wakes for w in words):
        return ERR_WAKE
    # SPEC-AMBIGUITY: "must not equal or contain a built-in literal phrase". Containing a ONE-word built-in
    # ("time", "status") is allowed — the longer custom phrase outranks it (§4.5 literal count) — otherwise
    # nearly every phrase would be refused. Equality with any built-in and containing a multi-word one are errors.
    for b in builtin_phrases(registry):
        if p == b or (" " in b and N.phrase_in(p, b)):
            return ERR_BUILTIN
    if p in {clean_phrase(o) for o in others}:
        return ERR_DUPLICATE
    if vocab is not None:
        try:
            unknown = vocab.unknown_words(p)
        except Exception:
            log.exception("vocabulary check failed")
            unknown = []
        if unknown:
            return ERR_UNKNOWN_WORD.format(word=unknown[0])
    return None


def _slug(phrase: str) -> str:
    return "custom_" + re.sub(r"[^a-z0-9]+", "_", phrase).strip("_")


def _help(entry: dict) -> str:
    steps = entry.get("steps") or []
    parts = [f"{s.get('action', '?')} {s.get('arg', '')}".strip() for s in steps[:4]]
    return "routine: " + " → ".join(parts) + (" …" if len(steps) > 4 else "") if parts else "routine (no steps)"


def register_custom(registry, config, vocab: Any = None) -> list[tuple[str, str]]:
    """(Re)register every enabled, valid config.custom_commands entry. Returns [(phrase, error)] for the
    entries that were skipped. Called at startup and on config.on_change("custom_commands")."""
    registry.unregister_custom()
    errors: list[tuple[str, str]] = []
    seen: list[str] = []
    wake_words = config.get("wake_words")
    for entry in config.get("custom_commands", []) or []:
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        raw = str(entry.get("phrase", ""))
        err = validate(raw, registry, vocab, wake_words, others=seen)
        if err:
            log.warning("custom command %r skipped: %s", raw, err)
            errors.append((raw, err))
            continue
        phrase = clean_phrase(raw)
        seen.append(phrase)
        snapshot = {"phrase": phrase, "steps": list(entry.get("steps") or [])}

        def handler(ctx, m, _entry=snapshot):
            run_routine(ctx, _entry)
        registry.command(_slug(phrase), phrase, section=SECTION, help=_help(entry), custom=True)(handler)
    return errors


def install(registry, config, engine, vocab: Any = None) -> None:
    """Register now and re-register (on the engine thread) whenever custom_commands changes."""
    register_custom(registry, config, vocab)
    config.on_change("custom_commands",
                     lambda key, value: engine.post(lambda: register_custom(registry, config, vocab)))


# ----------------------------------------------------------------------------- runner
def _open_target(ctx, arg: str) -> str:
    apps = ctx.config.get("apps", {}) or {}
    key = arg.strip().lower()
    return apps.get(key, arg.strip())


def run_routine(ctx, entry: dict) -> None:
    """Steps run one after another: executor steps chain through ctx.do(then=...), say/command steps run on
    the engine thread. A failing step logs, says "Step N of <phrase> failed" and stops."""
    steps = list(entry.get("steps") or [])
    name = entry.get("phrase", "the routine")
    start = ctx.clock.mono()
    spoke = [False]

    def fail(i: int, e: BaseException | None = None) -> None:
        log.warning("routine %r step %d failed: %r", name, i + 1, e)
        say(ctx, STEP_FAILED, n=i + 1, name=name)

    def step(i: int) -> None:
        if i >= len(steps):
            if not spoke[0]:
                say(ctx, R.DONE)
            return
        if ctx.clock.mono() - start > TOTAL_TIMEOUT_S:
            fail(i, TimeoutError("routine took longer than 60 s"))
            return
        st = steps[i] if isinstance(steps[i], dict) else {}
        action = str(st.get("action", "")).strip().lower()
        arg = st.get("arg", "")

        def go(_=None):
            step(i + 1)

        def err(e):
            fail(i, e)
        try:
            if action == "open":
                ctx.do(ctx.actions.open_target, _open_target(ctx, str(arg)), then=go, error=err)
            elif action == "run":
                run = getattr(ctx.actions, "run_command", None)
                if run is None:
                    raise RuntimeError("SystemActions.run_command is not available")
                ctx.do(run, str(arg), then=go, error=err)
            elif action == "keys":
                ctx.do(ctx.actions.keys, str(arg), then=go, error=err)
            elif action == "type":
                ctx.do(ctx.actions.type_text, str(arg), then=go, error=err)
            elif action == "say":
                spoke[0] = True
                ctx.say(persona.render(str(arg), ctx.config), then=go)
            elif action == "command":
                ctx.handle(str(arg), "routine")
                go()
            elif action == "wait":
                secs = max(0.0, min(float(MAX_WAIT_S), float(arg or 0)))
                ctx.do(time.sleep, secs, then=go, error=err, timeout=secs + 5)
            else:
                raise ValueError(f"unknown step action {action!r}")
        except Exception as e:
            fail(i, e)

    step(0)
