"""Jokes, coin, dice, pick a number (§3.7)."""
from __future__ import annotations

import random

from xyrus import persona, replies as R
from xyrus.commands import say
from xyrus.registry import command

SECTION = "Info & tools"
_rng = random.Random()


def _a(n: int) -> str:
    w = persona.number_words(n)
    return ("an " if w[0] in "aeiou" else "a ") + w


@command("joke", "tell me a joke", "joke", "tell me another", "tell me another joke", "make me laugh",
         "say something funny", "tell me something funny", section=SECTION,
         help="tells a joke (never the same one twice in a row)")
def joke(ctx, m):
    ctx.say(persona.pick("joke", R.JOKES, ctx.config))


@command("coin", "flip a coin", "heads or tails", "toss a coin", section=SECTION, help="heads or tails")
def coin(ctx, m):
    ctx.say(_rng.choice(R.COIN))


@command("dice", "roll a die", "roll a dice", "roll the dice", section=SECTION, help="rolls one die")
def dice(ctx, m):
    ctx.say(R.DICE.replace("a {n}", _a(_rng.randint(1, 6))))


@command("two_dice", "roll two dice", section=SECTION, help="rolls two dice")
def two_dice(ctx, m):
    a, b = _rng.randint(1, 6), _rng.randint(1, 6)
    ctx.say(R.DICE_TWO.replace("a {a}", _a(a)).replace("a {b}", _a(b)))


@command("pick_number", "pick a number between <number> and <number>", section=SECTION,
         help="a random whole number in a range")
def pick_number(ctx, m):
    lo, hi = sorted((int(round(m.slots["number"])), int(round(m.slots["number2"]))))
    say(ctx, R.PICK, n=_rng.randint(lo, hi))
