"""Figures in a reply are references to facts, written by code.

An agent never types a price, a size or a total. Every number in the session
brief and in this turn's tool results has a key - `cards_on_screen.card2.price`,
`search_products.products.card1.price`, `calculate.result` - and the agent
writes `{key}` where the figure goes. Code puts the number in, formatted the
same way everywhere, so a figure in a reply is one a service produced by
construction rather than one a checker failed to catch. A key that names no
figure is broken arguments, corrected in the same hop.

Keys follow the facts' own shape: an object in a list is named by what it is
(`card2`, `pick1`, `room_length`, a wall's name, a currency) rather than its
place in the list, so a key reads the way the customer would say it.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from app.agents.tools.common import RespondArgs
from app.agents.work import ToolRecord, ToolStatus, TurnWork
from app.integrations.llm import schema_violations

REFERENCE = re.compile(r"\{([^{}\n]{1,120})\}")
"""Anything between braces is a reference attempt - `{cards_on_screen.card2.price}`
- so a malformed one is corrected, never sent as braces."""

_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_SLUG = re.compile(r"[^a-z0-9]+")
_IDENTITY = ("card", "pick", "piece", "goal", "option", "position", "applies_to")
"""What names an object in a list, in order: its card on screen, its place in
the cart, its piece of a room, its goal, its option, its position, what a budget
bounds."""
_LABELS = ("label", "measures", "wall", "kind", "currency")
"""What else can name an object - used only where it is unique in its list."""
_NOT_FIGURES = frozenset(
    {"card", "pick", "piece", "goal", "option", "position", "matching_in_store", "exact_matches"}
)
"""Fields that are not figures to say: an object's own number, and how many
products the store holds (never said, CLAUDE.md 10.2)."""
_LISTS_CARDS = "cards_on_screen"


def figure_table(work: TurnWork) -> dict[str, Decimal]:
    """Every figure an agent may cite now, by key: the newest brief, then each
    tool's facts in order. The brief's cards give way once a tool has put a
    new list on screen, so "card 2" is never the card that was replaced."""
    table: dict[str, Decimal] = {}
    if work.views:
        table.update(flatten(work.views[-1]))
        since = work.view_marks[-1] if work.view_marks else 0
        if any(_lists_cards(r) for r in work.records[since:]):
            table = {k: v for k, v in table.items() if not k.startswith(_LISTS_CARDS + ".")}
    for root, record in record_roots(work.records):
        table.update(flatten(record.facts, root))
    return table


def record_roots(records: Sequence[ToolRecord]) -> Iterable[tuple[str, ToolRecord]]:
    """Each successful record with facts, keyed by its tool - and `_2`, `_3`
    for the same tool again this turn. A refused call takes no name, so a
    later good result keeps the plain one."""
    seen: Counter[str] = Counter()
    for record in records:
        if not record.facts or record.status is not ToolStatus.OK:
            continue
        seen[record.tool] += 1
        count = seen[record.tool]
        yield (record.tool if count == 1 else f"{record.tool}_{count}"), record


def root_of(records: Sequence[ToolRecord], record: ToolRecord) -> str | None:
    """The key root of one record - the one just run - or None when it
    names no figures."""
    return next((root for root, r in record_roots(records) if r is record), None)


def flatten(value: Any, prefix: str = "") -> dict[str, Decimal]:
    """Every number in the facts, by its key."""
    found: dict[str, Decimal] = {}
    if isinstance(value, bool) or value is None:
        return found
    if isinstance(value, int | float | Decimal):
        found[prefix] = Decimal(str(value))
    elif isinstance(value, str):
        if prefix and _NUMBER.match(value.strip()):
            # The pattern admits only a plain decimal, so it always reads.
            found[prefix] = Decimal(value.strip())
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in _NOT_FIGURES:
                continue
            found.update(flatten(item, _join(prefix, _slug(str(key)))))
    elif isinstance(value, list | tuple):
        for name, item in zip(_item_keys(value), value, strict=True):
            found.update(flatten(item, _join(prefix, name)))
    return found


def shown(table: Mapping[str, Decimal]) -> dict[str, str]:
    """The table as an agent reads it: each key with its figure as written."""
    return {key: written(figure) for key, figure in table.items() if key}


def written(figure: Decimal) -> str:
    """A figure as the customer reads it: 3,600 - 4,250.50 - 12.345. Never
    rounded: the figure written is the figure the facts hold."""
    if figure == figure.to_integral_value():
        return f"{int(figure):,}"
    return format(figure, ",f")


def render(text: str, table: Mapping[str, Decimal]) -> tuple[str, tuple[str, ...]]:
    """The text with each `{key}` written as its figure, and every brace
    reference that names no figure."""
    unknown: list[str] = []

    def put(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        figure = table.get(key)
        if figure is None:
            unknown.append(key)
            return match.group(0)
        return written(figure)

    return REFERENCE.sub(put, text), tuple(dict.fromkeys(unknown))


def _lists_cards(record: ToolRecord) -> bool:
    products = record.facts.get("products") if record.facts else None
    return isinstance(products, list) and any(
        isinstance(p, Mapping) and isinstance(p.get("card"), int) for p in products
    )


def _item_keys(items: Sequence[Any]) -> list[str]:
    """A key for each object in a list: its identity, else a label unique in
    the list, else its place - never two objects under one key."""
    names = [_named(item) for item in items]
    counts = Counter(n for n in names if n is not None)
    keys = [
        name if name is not None and (counts[name] == 1 or _identity(item)) else str(index)
        for index, (name, item) in enumerate(zip(names, items, strict=True), start=1)
    ]
    seen: Counter[str] = Counter()
    unique: list[str] = []
    for key in keys:
        seen[key] += 1
        unique.append(key if seen[key] == 1 else f"{key}_{seen[key]}")
    return unique


def _identity(item: Any) -> str | None:
    if not isinstance(item, Mapping):
        return None
    for name in _IDENTITY:
        value = item.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return f"{name}{value}"
        if name == "applies_to" and isinstance(value, str) and value:
            return _slug(value)
    return None


def _named(item: Any) -> str | None:
    identity = _identity(item)
    if identity is not None or not isinstance(item, Mapping):
        return identity
    for name in _LABELS:
        label = item.get(name)
        if isinstance(label, str) and (slug := _slug(label)) != "_":
            return slug
    return None


def _slug(text: str) -> str:
    return _SLUG.sub("_", text.casefold()).strip("_") or "_"


def _join(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def render_reply(
    reply: RespondArgs, table: Mapping[str, Decimal]
) -> tuple[RespondArgs, tuple[str, ...]]:
    """The reply with every figure written in - message, question and each
    choice - checked again as a reply, and every rule it broke: a reference
    that names no figure, or chips that no longer read as distinct."""
    message, unknown = render(reply.message, table)
    question, missing = (
        render(reply.follow_up_question, table) if reply.follow_up_question else (None, ())
    )
    broken = [*unknown, *missing]
    choices = []
    for choice in reply.choices:
        label, a = render(choice.label, table)
        value, b = render(choice.value, table)
        broken.extend((*a, *b))
        choices.append({"label": label, "value": value})
    rules = [f"{{{key}}} names no figure: use a key from figures" for key in dict.fromkeys(broken)]
    try:
        written_reply = RespondArgs.model_validate(
            {
                **reply.model_dump(),
                "message": message,
                "follow_up_question": question,
                "choices": choices,
            }
        )
    except ValidationError as exc:
        rules.extend(schema_violations(exc))
        written_reply = reply
    return written_reply, tuple(rules)


def without_references(text: str) -> str:
    """The text without the sentences - or lines - that still hold a brace
    reference."""
    return keep_sentences(text, lambda part: REFERENCE.search(part) is None)


_BREAK = re.compile(r"((?<=[.!?\u061f])[ \t]+|\n+)")
"""A sentence end, or a line break - kept, so lists stay lists."""


def keep_sentences(text: str, keep: Callable[[str], bool]) -> str:
    """The text with only the sentences `keep` accepts, each with the break
    that followed it - a bulleted line is a sentence of its own."""
    parts = _BREAK.split(text.strip())
    kept: list[str] = []
    for index in range(0, len(parts), 2):
        sentence = parts[index]
        if sentence.strip() and keep(sentence):
            kept.append(sentence + (parts[index + 1] if index + 1 < len(parts) else ""))
    return "".join(kept).strip()
