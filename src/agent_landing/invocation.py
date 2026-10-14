"""Deterministic parsing for `/landing` and `$landing` invocations.

Parsing used to happen inside the model, which made it nondeterministic, cost
tokens, and -- worst -- let a typo fail *silently* into the wrong quota floor. A
budget guard that quietly does not engage is more dangerous than no guard at all,
because the user believes they are protected. So parsing lives here, in code, and
anything ambiguous is refused rather than guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: What autocomplete teaches, so it holds only what nearly everyone needs. The
#: optional `weekly`/`5h` token still works -- the floor guards every window by
#: default, so scoping it to one is rare -- and is documented in the README.
SYNTAX = "/landing leave|spend <percent> [task]"

EXAMPLES = (
    "/landing leave 70 refactor auth and add tests",
    "/landing spend 15 fix the failing checkout tests",
    "/landing leave 5h 30 investigate the flaky integration test",
)

#: `leave` and `spend` are opposite readings of the same number, and neither is
#: inferred: a bare number is refused.
#:
#: The rule this enforces is that *the absence of a marker must not itself be a
#: valid, dangerous meaning*. With a default, dropping the word `spend` from
#: "spend 15" would silently yield "leave 15" -- still a legal invocation, but one
#: authorizing 76 points of spend instead of 15, with nothing to catch it. Making
#: the keyword mandatory removes that path structurally rather than relying on the
#: typo being unlikely.
_CJK_LEAVE = ("保留", "剩下", "留", "剩")
_CJK_SPEND = ("花费", "花", "用")

_MODE_ALIASES = {
    "leave": "preserve", "keep": "preserve", "preserve": "preserve", "reserve": "preserve",
    "spend": "max_spend", "use": "max_spend", "max": "max_spend", "budget": "max_spend",
    **{word: "preserve" for word in _CJK_LEAVE},
    **{word: "max_spend" for word in _CJK_SPEND},
}

_WINDOW_ALIASES = {
    "weekly": "seven_day", "week": "seven_day", "w": "seven_day",
    "7d": "seven_day", "seven_day": "seven_day", "sevenday": "seven_day",
    "5h": "five_hour", "5hour": "five_hour", "five_hour": "five_hour",
    "fivehour": "five_hour", "hourly": "five_hour",
    "每周": "seven_day", "一周": "seven_day", "5小时": "five_hour", "五小时": "five_hour",
}

# A floor is a bare number, optionally with a percent sign. Requiring digits only
# is what makes `5h` unambiguously a window token rather than a floor of 5 -- the
# old syntax needed a written warning telling the model not to confuse them. A
# trailing comma is punctuation: "leave 70, fix the tests".
_FLOOR = re.compile(r"^(\d{1,3}(?:\.\d+)?)[%％]?[,，;；:：]?$")

# --- natural phrasing --------------------------------------------------------
#
# "leave me 70% quota", "spend at most 15% on this", "至少留70%". Filler is
# accepted *around* the keyword and the number, never in place of either, so the
# rule that a missing keyword is not a legal meaning still holds. A qualifier
# carries a direction of its own, and one that contradicts the keyword ("leave
# at most 30") is refused: there is no sound reading of it to guess.

_LEAVE_QUALIFIERS = (("at", "least"), ("至少",), ("不少于",))
_SPEND_QUALIFIERS = (
    ("no", "more", "than"), ("at", "most"), ("up", "to"), ("only",),
    ("最多",), ("不超过",), ("只",),
)
_QUALIFIERS = tuple((words, "preserve") for words in _LEAVE_QUALIFIERS) + tuple(
    (words, "max_spend") for words in _SPEND_QUALIFIERS
)
_PRONOUNS = (("me",), ("us",))
_QUOTA_NOUNS = (("of", "my", "quota"), ("of", "the", "quota"), ("of", "quota"), ("quota",), ("额度",))

_CJK_QUALIFIER = "|".join(sorted(("至少", "不少于", "最多", "不超过", "只"), key=len, reverse=True))
_CJK_MODE = "|".join(sorted(_CJK_LEAVE + _CJK_SPEND, key=len, reverse=True))
# A Chinese keyword is often written against its number or task: `留70`,
# `最多花20%`, `留70%修测试`. Text may follow the number only after an explicit
# `%`, so task text that merely starts with a keyword -- `用3个线程跑测试` --
# is never split into a budget.
_CJK_GLUED = re.compile(
    rf"^({_CJK_QUALIFIER})?({_CJK_MODE})({_CJK_QUALIFIER})?"
    rf"(?:(\d{{1,3}}(?:\.\d+)?)([%％])?)?(.*)$"
)


def _split_glued(token: str) -> list[str]:
    match = _CJK_GLUED.match(token)
    if not match:
        return [token]
    before, mode, after, number, percent, rest = match.groups()
    if rest and not (number and percent):
        return [token]
    pieces = [before, mode, after, f"{number}{percent or ''}" if number else None, rest]
    return [piece for piece in pieces if piece]


def _match_words(tokens: list[str], at: int, choices: tuple[tuple[str, ...], ...]) -> int:
    """Length of the multi-word choice starting at `at`, or 0."""
    for words in choices:
        end = at + len(words)
        if end <= len(tokens) and tuple(token.lower() for token in tokens[at:end]) == words:
            return len(words)
    return 0


def _match_qualifier(tokens: list[str], at: int) -> tuple[int, str] | None:
    for words, direction in _QUALIFIERS:
        if _match_words(tokens, at, (words,)):
            return len(words), direction
    return None


def _match_phrase(tokens: list[str], at: int) -> tuple[int, list[str], list[str]] | None:
    """Read `[qualifier] keyword [window|me|qualifier]... number [quota]` at `at`.

    Returns where it ends, the tokens that carry meaning (keyword, window,
    number), and the directions any qualifiers imply.
    """
    index, kept, directions = at, [], []
    qualifier = _match_qualifier(tokens, index)
    if qualifier:
        index += qualifier[0]
        directions.append(qualifier[1])
    if index >= len(tokens) or not _mode_id(tokens[index]):
        return None
    kept.append(tokens[index])
    index += 1
    while index < len(tokens):
        if _window_id(tokens[index]):
            kept.append(tokens[index])
            index += 1
            continue
        pronoun = _match_words(tokens, index, _PRONOUNS)
        if pronoun:
            index += pronoun
            continue
        qualifier = _match_qualifier(tokens, index)
        if qualifier:
            index += qualifier[0]
            directions.append(qualifier[1])
            continue
        break
    if index >= len(tokens) or not _FLOOR.match(tokens[index]):
        return None
    kept.append(tokens[index])
    index += 1
    index += _match_words(tokens, index, _QUOTA_NOUNS)
    return index, kept, directions


def _normalize_segment(tokens: list[str]) -> tuple[list[str], str | None]:
    """Reduce a natural-language settings phrase at either end to canonical tokens."""
    if not tokens:
        return tokens, None
    tokens = _split_glued(tokens[0]) + tokens[1:]
    if len(tokens) > 1:
        tokens = tokens[:-1] + _split_glued(tokens[-1])

    start = 1 if _window_id(tokens[0]) else 0
    found = _match_phrase(tokens, start)
    span = (start, found) if found else None
    if span is None:
        for at in range(1, len(tokens)):
            found = _match_phrase(tokens, at)
            if found and found[0] == len(tokens):
                span = (at, found)
                break
    if span is None:
        return tokens, None

    at, (end, kept, directions) = span
    mode = _mode_id(kept[0])
    if any(direction != mode for direction in directions):
        phrase = " ".join(tokens[at:end])
        return tokens, (
            f"{phrase!r} contradicts itself: 'leave' is a floor that must remain, "
            "'spend' is a ceiling on spending, and the qualifier points the other way."
        )
    return tokens[:at] + kept + tokens[end:], None


def _normalize(tokens: list[str]) -> tuple[list[str], str | None]:
    if "--" in tokens:
        at = tokens.index("--")
        before, error = _normalize_segment(tokens[:at])
        if error:
            return tokens, error
        after, error = _normalize_segment(tokens[at + 1:])
        return before + ["--"] + after, error
    return _normalize_segment(tokens)


#: Bare verbs that end the contract rather than starting one. Recognized here as
#: well as in the `UserPromptSubmit` hook, so the meaning is identical on a host
#: that has no such hook and on one where the agent routes it through `invoke`.
_CANCEL_VERBS = frozenset({"cancel", "stop", "abort"})


@dataclass
class ParsedInvocation:
    ok: bool
    #: "start" opens a budget; "cancel" ends the contract entirely.
    action: str = "start"
    #: "auto" resolves to the longest window the account actually exposes, so a
    #: Plus account (five-hour only) and a Pro account (weekly) both work without
    #: the user knowing which plan they are on.
    window: str = "auto"
    #: "preserve" -> `percent` is the floor that must remain.
    #: "max_spend" -> `percent` is how much may be spent.
    mode: str = "preserve"
    percent: float | None = None
    task: str = ""
    error: str | None = None
    suggestion: str | None = None
    #: Offered when the intent is genuinely ambiguous and must not be guessed.
    alternatives: list[str] = field(default_factory=list)

    @property
    def means_leave(self) -> bool:
        return self.mode == "preserve"

    def to_dict(self) -> dict[str, Any]:
        if self.ok and self.action == "cancel":
            return {"ok": True, "action": "cancel"}
        if self.ok:
            return {
                "ok": True,
                "action": self.action,
                "window": self.window,
                "mode": self.mode,
                "percent": self.percent,
                "task": self.task,
            }
        return {
            "ok": False,
            "error": self.error,
            "suggestion": self.suggestion,
            "alternatives": self.alternatives,
            "syntax": SYNTAX,
            "examples": list(EXAMPLES),
        }

    def message(self) -> str:
        """Human-readable text the agent should show verbatim and then stop."""
        lines = [f"Could not start a quota budget: {self.error}", f"Syntax: {SYNTAX}"]
        if self.alternatives:
            # Both readings are shown, spelled out, because guessing between them
            # is exactly what this refusal exists to prevent.
            lines.append("Did you mean:")
            lines.extend(f"  {alternative}" for alternative in self.alternatives)
        elif self.suggestion:
            lines.append(f"Did you mean: {self.suggestion}")
        else:
            lines.append("Examples:")
            lines.extend(f"  {example}" for example in EXAMPLES)
        return "\n".join(lines)


def _window_id(token: str) -> str | None:
    return _WINDOW_ALIASES.get(token.lower().lstrip("-"))


def _mode_id(token: str) -> str | None:
    return _MODE_ALIASES.get(token.lower().lstrip("-"))


def _is_option_token(token: str) -> bool:
    return bool(_window_id(token) or _mode_id(token) or _FLOOR.match(token))


def _fail(
    error: str,
    suggestion: str | None = None,
    alternatives: list[str] | None = None,
) -> ParsedInvocation:
    return ParsedInvocation(
        ok=False, error=error, suggestion=suggestion, alternatives=alternatives or []
    )


def _suggest_from(tokens: list[str]) -> str | None:
    """Offer a correction for a near-miss, as text only -- never auto-applied."""
    for token in tokens:
        # The letter-for-digit repair is for a mistyped number like `7O`, so it
        # only applies to a token that is already trying to be one. Without that
        # guard an ordinary word qualified: `leave abc do the thing` suggested
        # `leave 0`, because "do" became "d0". A wrong suggestion is worse than
        # none -- it teaches syntax the guard will refuse.
        if not any(character.isdigit() for character in token):
            continue
        digits = re.sub(r"[^\d.]", "", token.replace("O", "0").replace("o", "0"))
        if digits and _FLOOR.match(digits) and 0 <= float(digits) <= 100:
            return f"/landing leave {float(digits):g} <task>"
    return None


def _is_complete_settings(tokens: list[str]) -> bool:
    """Do these tokens form a whole, unambiguous settings group?

    Requiring exactly one mode keyword and exactly one number is what makes it
    safe to recognize settings at either end of the invocation. The dangerous
    fields can never be inferred: a group without an explicit `leave`/`spend`
    is not a group, so trailing text can never silently decide the mode.
    """
    if not tokens:
        return False
    modes = windows = numbers = 0
    for token in tokens:
        if _mode_id(token):
            modes += 1
        elif _window_id(token):
            windows += 1
        elif _FLOOR.match(token):
            numbers += 1
        else:
            return False
    return modes == 1 and numbers == 1 and windows <= 1


def _leading_options(tokens: list[str]) -> tuple[list[str], list[str]]:
    for index, token in enumerate(tokens):
        if not _is_option_token(token):
            return tokens[:index], tokens[index:]
        # `use` and `用` are spend aliases and also how many tasks begin: "leave
        # 70 use pytest". Once the group is complete, a keyword not followed by
        # another setting starts the task. One that *is* followed by one --
        # "leave 70 spend 20" -- stays in the group and is refused as a conflict.
        followed_by_setting = index + 1 < len(tokens) and _is_option_token(tokens[index + 1])
        if _mode_id(token) and _is_complete_settings(tokens[:index]) and not followed_by_setting:
            return tokens[:index], tokens[index:]
    return list(tokens), []


def _trailing_options(tokens: list[str]) -> tuple[list[str], list[str]]:
    cut = len(tokens)
    while cut > 0 and _is_option_token(tokens[cut - 1]):
        cut -= 1
    return tokens[cut:], tokens[:cut]


def _split_options_and_task(tokens: list[str]) -> tuple[list[str], list[str]]:
    """Locate the settings group, which may sit before or after the task.

    "leave 70 fix the tests" and "fix the tests leave 70" are both natural
    ways to say the same thing, and GNU-style tools permute options and operands
    freely, so both are accepted. Settings before the task win when both ends
    could be read as a group, which keeps the documented form authoritative.
    """
    if "--" in tokens:
        at = tokens.index("--")
        before, after = tokens[:at], tokens[at + 1:]
        if _is_complete_settings(before) or not _is_complete_settings(after):
            return before, after
        return after, before

    leading, rest = _leading_options(tokens)
    if _is_complete_settings(leading):
        return leading, rest
    trailing, head = _trailing_options(tokens)
    if head and _is_complete_settings(trailing):
        return trailing, head
    # Neither end is a usable group; report against the leading run so the error
    # describes what the user actually typed first.
    return leading, rest


#: `/landing` on Claude Code, `$landing` on Codex. The lookahead keeps path
#: segments out: `~/.agents/skills/landing/` contains the letters but is not an
#: invocation, and an invocation is always followed by its arguments or nothing.
_MARKER = re.compile(r"[/$]landing\b(?=\s|$)", re.IGNORECASE)


#: Decoration that survives a copy-paste but changes no meaning: a fenced or
#: inline code span, a quoted string, a sentence-ending period. Stripped in pairs
#: from the outside in, before the whole-prompt match.
_CANCEL_WRAPPERS = ("```", "`", '"', "'", "“”", "「」")
_CANCEL_TRAILING = ".。!！"


def undecorate(prompt: str) -> str:
    """Strip formatting a user cannot help pasting, keeping the message whole.

    The rescue path is the worst place for a literal match to be brittle. An
    agent that tells a blocked user to "send `$landing cancel`" renders it as a
    code span; the user copies it, the backticks come along, and the cancel does
    nothing -- the agent manufactured the failure it was trying to talk them out
    of. Observed on Codex 2026-08-29.

    This does not loosen the safety property, which is that the *entire* message
    must be the command: "why does `/landing cancel` not work?" still carries
    other words and is still not a cancel. A trailing `?` is deliberately not
    stripped, so a question stays a question.
    """
    text = " ".join((prompt or "").split())
    changed = True
    while changed:
        changed = False
        for wrapper in _CANCEL_WRAPPERS:
            opening, closing = (wrapper[0], wrapper[-1]) if len(wrapper) == 2 else (wrapper, wrapper)
            if len(text) > len(opening) + len(closing) and text.startswith(opening) and text.endswith(closing):
                text = text[len(opening):-len(closing)].strip()
                changed = True
        while text and text[-1] in _CANCEL_TRAILING:
            text = text[:-1].strip()
            changed = True
    return text


def extract_invocation(prompt: str) -> tuple[str, bool] | None:
    """Pull a Landing invocation out of a whole user message.

    Returns ``(arguments, authoritative)``, or None when the message does not
    invoke Landing at all. Text before the marker is task text -- people write
    "继续/landing spend 20" -- and the parser already accepts settings on either
    side of the task, so the two halves are simply joined.

    `authoritative` is True only when the message *starts* with the marker. That
    distinction decides what a parse failure means, and it is the whole reason
    this returns a flag:

    - Starts with the marker: unambiguously an invocation. A failure to parse is
      a typo in a real budget request, and must be refused loudly -- silently
      not guarding is the failure this project ranks worst.
    - Marker mid-message: could just be prose about the command ("why does
      /landing cancel not work?"). Acting on a failed parse there would block
      ordinary conversation, so a failure means "this was not an invocation".

    The residual gap is a typo in a mid-message invocation, which is left alone
    rather than refused. That is the safer side of an unavoidable trade: the
    alternative makes the tool impossible to talk about.
    """
    prompt = undecorate(prompt)
    match = _MARKER.search(prompt)
    if not match:
        return None
    before = prompt[: match.start()].strip()
    after = prompt[match.end() :].strip()
    arguments = f"{before} {after}".strip()
    return arguments, match.start() == 0


def parse_invocation(raw: str) -> ParsedInvocation:
    tokens = (raw or "").strip().split()
    if not tokens:
        return _fail("no arguments were provided.")

    if len(tokens) == 1 and tokens[0].lower().lstrip("-") in _CANCEL_VERBS:
        # Only the bare verb, never a verb buried in a task description: ending
        # the contract is not something to infer from a passing mention.
        return ParsedInvocation(ok=True, action="cancel")

    tokens, contradiction = _normalize(tokens)
    if contradiction:
        return _fail(contradiction)
    option_tokens, task_tokens = _split_options_and_task(tokens)

    windows: list[str] = []
    modes: list[str] = []
    floors: list[float] = []
    unknown: list[str] = []
    for token in option_tokens:
        window = _window_id(token)
        if window:
            windows.append(window)
            continue
        mode = _mode_id(token)
        if mode:
            modes.append(mode)
            continue
        match = _FLOOR.match(token)
        if match:
            floors.append(float(match.group(1)))
            continue
        unknown.append(token)

    if unknown:
        return _fail(
            f"could not understand {', '.join(repr(token) for token in unknown)}.",
            _suggest_from(unknown),
        )
    if not floors:
        return _fail("no quota percentage was given.", _suggest_from(tokens))
    if len(floors) > 1:
        joined = ", ".join(f"{value:g}" for value in floors)
        return _fail(f"more than one quota percentage was given ({joined}); expected exactly one.")
    if len(set(windows)) > 1:
        return _fail("more than one quota window was given; expected at most one.")
    if len(set(modes)) > 1:
        return _fail(
            "'leave' and 'spend' mean opposite things and cannot both be given.",
            f"/landing leave {floors[0]:g} <task>",
        )

    percent = floors[0]
    if not 0 <= percent <= 100:
        return _fail(f"the percentage must be between 0 and 100, got {percent:g}.")

    if not modes:
        # Refused rather than defaulted. With a default, a dropped keyword stays a
        # legal invocation carrying the opposite meaning; refusing makes that path
        # impossible instead of merely unlikely.
        window_word = f"{windows[0]} " if windows else ""
        tail = " ".join(task_tokens).strip() or "<task>"
        return _fail(
            f"say whether {percent:g} is quota to leave or quota to spend.",
            alternatives=[
                f"/landing leave {window_word}{percent:g} {tail}"
                f"   (stop while {percent:g}% still remains)",
                f"/landing spend {window_word}{percent:g} {tail}"
                f"   (spend at most {percent:g} percentage points)",
            ],
        )

    mode = modes[0]
    # The task is a label, not a control: nothing in the policy or the gate reads
    # it. Demanding it meant a user who had already said what they wanted had to
    # say it again just to attach a budget -- `/landing spend 20` on its own is
    # the natural way to put a floor under work already under way.
    task = " ".join(task_tokens).strip()

    return ParsedInvocation(
        ok=True,
        window=windows[0] if windows else "auto",
        mode=mode,
        percent=percent,
        task=task,
    )
