"""Live conversation evaluation, before and after, against running servers.

Drives real `/v1/chat` servers - real model, catalog, index and Redis - with
the conversations in `cases.yaml`, and checks each final reply. It calls live
providers, so it is never part of pytest. Run with one or more labelled
servers::

    python -m evals.conversations.run before=http://127.0.0.1:8801 \\
        after=http://127.0.0.1:8802

Every server gets its own fresh session per case, so versions never share
state. Results are printed as a side-by-side table; failures are signal about
the prompt, the model or the code, not something to patch per case.

A turn is either words the customer types, or a tap the screen makes - a
question card answered or skipped, a card ticked, "Goes with", Compare, a chip,
"Show me different options", "Not this one". Taps are sent exactly as the
console sends them, reading keys and positions from the replies before them,
so a case never hard-codes a key the server made up.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import yaml
from app.agents.reply import FALLBACK_REPLY
from app.services.reply_language import writes_arabic_script

CASES_PATH = Path(__file__).parent / "cases.yaml"
STORE_ID = 50
REQUEST_TIMEOUT_S = 240.0
CONCURRENCY = 12
"""Conversations in flight per server; `--concurrency=N` overrides it. Turns
within a conversation always run in order. 12 kept a full run near 12 minutes
with no provider rate limiting; lower it if 429s appear."""
FALLBACKS = tuple(FALLBACK_REPLY.values())
"""The "please say it another way" reply, in either language - read from the
application's own tables, so the check cannot drift from what is sent."""
NO_THANKS = {"No thanks", "لا، شكرًا"}
"""Turning down what goes with a pick, in either language - now the agent's
words, so a loose check."""
_NO_MATCH = re.compile(r"\b(no|none|not|don't|do not|couldn't|could not|isn't|aren't|nothing)\b")
"""A reply that admits the request had no exact match uses one of these."""


class CaseError(Exception):
    """A tap the conversation could not make - the card has no such question,
    there is no grid to tick. Reported as the case's failure."""


@dataclass
class TurnResult:
    status: int
    elapsed_s: float
    body: dict[str, Any] = field(default_factory=dict)
    kind: str = "chat"
    """`chat` for a message or a screen action sent as a chat turn; `picks`
    for a silent tick or untick; `comparison` for the compare pop-up."""
    chosen_budget: str | None = None
    """The budget band tapped on a card, as its label, for checking prices."""

    @property
    def message(self) -> str:
        if self.status != 200:
            error = self.body.get("error", {})
            return f"[HTTP {self.status} {error.get('code', '')}] {error.get('message', '')}"
        response = self.body.get("response", {})
        text = response.get("message", "")
        question = response.get("follow_up_question")
        return f"{text} {question}" if question else text

    @property
    def products(self) -> list[dict[str, Any]]:
        presentation = self.body.get("presentation") or {}
        return list(presentation.get("products") or [])

    @property
    def combinations(self) -> list[dict[str, Any]]:
        presentation = self.body.get("presentation") or {}
        return list(presentation.get("seating_bundles") or [])

    @property
    def follow_up(self) -> str:
        return str((self.body.get("response") or {}).get("follow_up_question") or "")

    @property
    def has_comparison(self) -> bool:
        return bool((self.body.get("presentation") or {}).get("comparison"))

    @property
    def has_room(self) -> bool:
        return bool((self.body.get("presentation") or {}).get("room"))

    @property
    def room(self) -> dict[str, Any]:
        return dict((self.body.get("presentation") or {}).get("room") or {})

    @property
    def has_piece_picker(self) -> bool:
        return bool((self.body.get("presentation") or {}).get("piece_picker"))

    @property
    def presentation(self) -> dict[str, Any]:
        return dict(self.body.get("presentation") or {})

    @property
    def brief(self) -> dict[str, Any] | None:
        return self.presentation.get("brief") or None

    @property
    def picks(self) -> list[dict[str, Any]] | None:
        picks = self.body.get("picks")
        return None if picks is None else list(picks)

    @property
    def liked(self) -> list[dict[str, Any]] | None:
        liked = self.body.get("liked")
        return None if liked is None else list(liked)


@dataclass
class CaseResult:
    case_id: str
    turns: list[TurnResult]
    failures: list[str]

    @property
    def last(self) -> TurnResult:
        return self.turns[-1]


def _combination_key(combination: dict[str, Any]) -> tuple[tuple[str, int], ...]:
    return tuple(
        sorted((str(i.get("product_url")), int(i.get("quantity", 1))) for i in combination["items"])
    )


RETIRED_CHECKS = frozenset(
    {
        "card_mode",
        "narrow_down",
        "brief_chips_include",
        "brief_chips_exclude",
        "opening_never",
        "card_asks",
        "card_skips",
        "card_first",
        "card_kinds_include",
        "card_kind_first",
        "first_chip",
        "best_match",
        "chosen_seating",
        "taste_asked",
        "taste_which",
        "companion_chips_min",
        "piece_picker",
        "cards_named",
        "offer",
    }
)
"""Checks on screen elements the scripted flow drew and the agents do not - the
question card, Narrow down, the brief's chips, code-chosen chips. Skipped; a
turn left with no check of its own is still held to not falling back."""


def _check(
    checks: dict[str, Any], turn: TurnResult, previous: TurnResult | None = None
) -> list[str]:
    """Every failed check on the final reply, as a short reason."""
    live = {k: v for k, v in checks.items() if k not in RETIRED_CHECKS}
    checks = live if live or not checks else {"not_fallback": True}
    if checks.get("popup_refused"):
        code = (turn.body.get("error") or {}).get("code")
        if turn.kind == "comparison" and turn.status == 422 and code == "comparison_refused":
            return []
        return [f"expected the pop-up to refuse, got HTTP {turn.status} {code}"]
    if turn.status != 200:
        return [f"HTTP {turn.status}"]
    failures: list[str] = []
    products = turn.products
    colors = [p.get("main_color") for p in products]
    if checks.get("not_fallback") and turn.message.startswith(FALLBACKS):
        failures.append("fallback reply")
    if (minimum := checks.get("min_products")) and len(products) < minimum:
        failures.append(f"{len(products)} products < {minimum}")
    if (most := checks.get("max_products")) is not None and len(products) > most:
        failures.append(f"{len(products)} products > {most} - it should have asked first")
    if (least := checks.get("min_combinations")) and len(turn.combinations) < least:
        failures.append(f"{len(turn.combinations)} combinations < {least}")
    if checks.get("same_kind_as_previous") and previous is not None:
        kinds = {(p.get("commerce") or {}).get("subcategory") for p in products}
        before = {
            (p.get("commerce") or {}).get("subcategory")
            for p in previous.presentation.get("products") or []
        }
        if not kinds or kinds != before:
            failures.append(f"the kind changed from {sorted(before)} to {sorted(kinds)}")
    if (position := checks.get("same_kind_as_card")) and previous is not None:
        earlier = previous.presentation.get("products") or []
        if len(earlier) < position:
            failures.append(f"previous turn had no card {position}")
        else:
            tapped = (earlier[position - 1].get("commerce") or {}).get("subcategory")
            kinds = {(p.get("commerce") or {}).get("subcategory") for p in products}
            if not products or kinds != {tapped}:
                failures.append(f"product types {sorted(map(str, kinds))}, expected only {tapped}")
    if checks.get("new_combinations") and previous is not None:
        before = {_combination_key(c) for c in previous.combinations}
        repeated = [c for c in turn.combinations if _combination_key(c) in before]
        if not turn.combinations or repeated:
            failures.append(f"{len(repeated)} of {len(turn.combinations)} combinations repeat")
    if (position := checks.get("drops_previous_combination")) and previous is not None:
        earlier = previous.combinations
        if len(earlier) < position:
            failures.append(f"previous turn had no combination {position}")
        else:
            gone = _combination_key(earlier[position - 1])
            kept = [_combination_key(c) for i, c in enumerate(earlier) if i != position - 1]
            now = {_combination_key(c) for c in turn.combinations}
            if gone in now or not set(kept) <= now:
                failures.append(f"combination {position} not replaced, or the others not kept")
    if (words := checks.get("follow_up_mentions")) and not any(
        w in turn.follow_up.lower() for w in words
    ):
        failures.append(f"follow-up {turn.follow_up!r} mentions none of {words}")
    if (ceiling := checks.get("max_price")) is not None:
        over = [p["price_amount"] for p in products if Decimal(str(p["price_amount"])) > ceiling]
        if over:
            failures.append(f"prices over {ceiling}: {over}")
    if (allowed := checks.get("colors_subset")) and any(c not in allowed for c in colors):
        failures.append(f"colours {colors} outside {allowed}")
    if top := checks.get("top_colors_any"):
        head = colors[: top["n"]]
        if not any(c in top["colors"] for c in head):
            failures.append(f"top {top['n']} colours {head} match none of {top['colors']}")
    if (distinct := checks.get("min_distinct_colors")) and len(set(colors)) < distinct:
        failures.append(f"only {len(set(colors))} distinct colours - looks filtered")
    if checks.get("new_products") and previous is not None:
        before = {p.get("product_url") for p in previous.products}
        repeated = [p.get("product_url") for p in products if p.get("product_url") in before]
        if not products or repeated:
            failures.append(f"{len(repeated)} of {len(products)} cards repeat the previous turn")
    if (ordinal := checks.get("drops_previous_card")) and previous is not None:
        earlier = previous.products
        if len(earlier) < ordinal:
            failures.append(f"previous turn had no card {ordinal}")
        else:
            dropped = earlier[ordinal - 1].get("product_url")
            if not products or any(p.get("product_url") == dropped for p in products):
                failures.append(f"card {ordinal} from the previous turn is still shown")
    if (subcategory := checks.get("subcategory")) and any(
        (p.get("commerce") or {}).get("subcategory") != subcategory for p in products
    ):
        kinds = sorted({str((p.get("commerce") or {}).get("subcategory")) for p in products})
        failures.append(f"product types {kinds}, expected only {subcategory}")
    if limit := checks.get("max_dimension"):
        field, ceiling = limit["field"], Decimal(str(limit["value"]))
        sizes = [(p.get("dimensions") or {}).get(field) for p in products]
        if any(v is None or Decimal(str(v)) > ceiling for v in sizes):
            failures.append(f"{field} {sizes} not all at most {ceiling}")
    if limit := checks.get("max_floor_side"):
        # The piece's longer or shorter floor side, whichever column holds it.
        pick = max if limit["side"] == "longer" else min
        ceiling = Decimal(str(limit["value"]))
        sides = []
        for p in products:
            dims = p.get("dimensions") or {}
            pair = [dims.get("length_cm"), dims.get("width_cm")]
            sides.append(None if None in pair else pick(Decimal(str(v)) for v in pair))
        if any(v is None or v > ceiling for v in sides):
            failures.append(f"{limit['side']} sides {sides} not all at most {ceiling}")
    if pair := checks.get("pair_within"):
        # Both floor sides near the pair they gave, whichever column holds each.
        short, long, share = (Decimal(str(pair[k])) for k in ("short", "long", "share"))
        sides = []
        for p in products:
            dims = p.get("dimensions") or {}
            values = [dims.get("length_cm"), dims.get("width_cm")]
            sides.append(None if None in values else sorted(Decimal(str(v)) for v in values))
        if any(
            s is None or abs(s[0] - short) > short * share or abs(s[1] - long) > long * share
            for s in sides
        ):
            failures.append(f"sides {sides} not within {share} of {short} x {long}")
    if floor := checks.get("some_dimension_above"):
        field, bound = floor["field"], Decimal(str(floor["value"]))
        sizes = [(p.get("dimensions") or {}).get(field) for p in products]
        if not any(v is not None and Decimal(str(v)) > bound for v in sizes):
            failures.append(f"{field} {sizes} all at most {bound} - the old limit still applies")
    if checks.get("admits_no_match") and not _NO_MATCH.search(
        turn.message.lower().replace("\u2019", "'")
    ):
        failures.append("reply does not say that nothing matched")
    if checks.get("arabic_reply") and not (
        turn.body.get("reply_language") == "ar" and writes_arabic_script(turn.message)
    ):
        failures.append(f"not an Arabic reply ({turn.body.get('reply_language')})")
    if checks.get("arabic_text") and not writes_arabic_script(
        turn.message if turn.kind != "comparison" else str(turn.body.get("message") or "")
    ):
        failures.append("the text is not in Arabic")
    if checks.get("arabic_chips"):
        labels = _screen_labels(turn)
        if not labels:
            failures.append("no chips, card or piece picker to read")
        elif english := [
            # A bare number ("4", a head count) reads the same in either.
            label
            for label in labels
            if not writes_arabic_script(label) and not label.strip().isdigit()
        ]:
            failures.append(f"chips not in Arabic: {english}")
    if checks.get("english_reply") and (
        turn.body.get("reply_language") == "ar" or writes_arabic_script(turn.message)
    ):
        failures.append(f"not an English reply ({turn.body.get('reply_language')})")
    if (words := checks.get("mentions")) and not any(w in turn.message.lower() for w in words):
        failures.append(f"reply mentions none of {words}")
    if (status := checks.get("room_status")) and turn.room.get("status") != status:
        failures.append(f"room status {turn.room.get('status')!r}, expected {status!r}")
    if kinds := checks.get("room_has"):
        present = {(i.get("commerce") or {}).get("subcategory") for i in turn.room.get("items", [])}
        if not set(kinds) <= present:
            failures.append(f"room holds {sorted(map(str, present))}, missing some of {kinds}")
    if (locked := checks.get("room_locked")) is not None:
        count = sum(1 for i in turn.room.get("items", []) if i.get("locked"))
        if count != locked:
            failures.append(f"room has {count} locked pieces, expected {locked}")
    if limits := checks.get("room_type_max"):
        present: dict[str, int] = {}
        for i in turn.room.get("items", []):
            kind = str((i.get("commerce") or {}).get("subcategory"))
            present[kind] = present.get(kind, 0) + int(i.get("quantity", 1))
        over = {k: present.get(k, 0) for k, n in limits.items() if present.get(k, 0) > n}
        if over:
            failures.append(f"room has too many of {over}, limits {limits}")
    if seats := checks.get("room_seats"):
        items = turn.room.get("items", [])
        total = sum(
            int(i.get("quantity", 1)) * int((i.get("commerce") or {}).get("seating_capacity") or 1)
            for i in items
            if (i.get("commerce") or {}).get("category") == "seating"
        )
        if total != seats:
            failures.append(f"room seats {total}, expected {seats}")
    if kind := checks.get("combination_excludes"):
        used = [
            (i.get("commerce") or {}).get("subcategory")
            for c in turn.combinations
            for i in c.get("items", [])
        ]
        if kind in used:
            failures.append(f"a combination uses {kind}")
    if (words := checks.get("not_mentions")) and any(w in turn.message.lower() for w in words):
        failures.append(f"reply mentions one of {words}")
    failures.extend(_discovery_checks(checks, turn))
    if checks.get("engages"):
        asks = _asks(turn.message)
        shown = (
            products or turn.has_comparison or turn.has_room or turn.has_piece_picker or turn.brief
        )
        if not (shown or asks):
            failures.append("dead end: no products, comparison, room, chips, card or question")
    return failures


def _screen_labels(turn: TurnResult) -> list[str]:
    """Every label the customer reads beside the reply: chips, the card's
    questions and answers, and the room piece picker."""
    presentation = turn.presentation
    labels = [c["label"] for c in presentation.get("choices") or []]
    for card in (presentation.get("brief"), presentation.get("narrow_down")):
        for question in (card or {}).get("questions", []):
            labels.append(question["label"])
            labels.extend(c["label"] for c in question["choices"])
    labels.extend(chip["label"] for chip in presentation.get("brief_chips") or [])
    if picker := presentation.get("piece_picker"):
        labels.extend(p["label"] for p in picker["pieces"])
        labels.extend((picker["submit_label"], picker["choose_for_me"]["label"]))
    return labels


def _said_answers(spec: dict[str, Any]) -> str:
    """A card's answers as a customer would type them, or "just show me"."""
    words = [
        str(v)
        for value in spec.values()
        if not isinstance(value, bool)
        for v in (value if isinstance(value, list) else [value])
        if not isinstance(v, int)
    ]
    return ", ".join(words) if words else "Just show me what you have."


def _asks(text: str) -> bool:
    """A question, in either script: "?" or the Arabic "؟"."""
    return "?" in text or "\u061f" in text


def _upper_bound(label: str | None) -> Decimal | None:
    """The ceiling of a tapped budget band: "Under 1,900 SAR", "1,900-2,700
    SAR"; "Over 4,000 SAR" has none."""
    if not label or label.lower().startswith("over"):
        return None
    figures = re.findall(r"\d[\d,]*", label)
    return Decimal(figures[-1].replace(",", "")) if figures else None


def _discovery_checks(checks: dict[str, Any], turn: TurnResult) -> list[str]:
    """The question card, picks, cross-sell and comparison checks."""
    failures: list[str] = []
    products = turn.products
    # The question card is gone: an agent that asks first asks in words, with
    # nothing searched yet; "card": false (or "opening": 0) means it showed
    # products straight away.
    asks_first = checks.get("card")
    if (count := checks.get("opening")) is not None:
        asks_first = count > 0
    if asks_first is not None:
        asked = _asks(turn.message) and not products
        if asks_first and not asked:
            failures.append("it showed products without asking first")
        if not asks_first and asked:
            failures.append("it asked first though nothing was left to ask")
    if (want := checks.get("product_source")) is not None:
        got = turn.presentation.get("product_source")
        if got != want:
            failures.append(f"the cards are {got}, expected {want}")
    if (count := checks.get("liked")) is not None:
        got = len(turn.liked or [])
        if got != count:
            failures.append(f"{got} liked, expected {count}")
    if (count := checks.get("picks")) is not None:
        got = len(turn.picks or [])
        if got != count:
            failures.append(f"{got} picks in the tray, expected {count}")
    # What goes with a pick: cards of another kind than the newest pick.
    for name in ("cross_sell", "shows_with_pick"):
        if (want := checks.get(name)) is not None:
            picked = ((turn.picks or [{}])[-1].get("kind") or "").replace("-", " ")
            shown = [
                ((p.get("commerce") or {}).get("subcategory") or "").replace("-", " ")
                for p in products
            ]
            got = bool(picked) and bool(products) and picked not in shown
            if got != want:
                failures.append(
                    f"no products shown with the pick (pick {picked}, cards {shown})"
                    if want
                    else "products were shown with the pick"
                )
            break
    if checks.get("silent_tick") and (turn.kind != "picks" or turn.body.get("goes_with")):
        failures.append("the tick was not silent - it started a turn")
    if (seats := checks.get("seats")) is not None:
        counts = [(p.get("commerce") or {}).get("seating_capacity") for p in products]
        if not products or any(c != seats for c in counts):
            failures.append(f"seat counts {counts}, expected all {seats}")
    if (least := checks.get("min_seats")) is not None:
        counts = [(p.get("commerce") or {}).get("seating_capacity") for p in products]
        if not products or any(c is None or c < least for c in counts):
            failures.append(f"seat counts {counts}, expected all at least {least}")
    if checks.get("comparison") and not turn.has_comparison:
        failures.append("no comparison table")
    if checks.get("popup"):
        columns = (turn.body.get("comparison") or {}).get("products") or []
        wanted = checks.get("popup_columns", 2)
        if turn.kind != "comparison" or len(columns) != wanted or not turn.body.get("message"):
            failures.append(f"no pop-up comparison of {wanted} products with a take")
    if checks.get("next_step"):
        choices = turn.presentation.get("choices") or []
        if not _asks(turn.message) or not choices:
            failures.append("dead end: the reply does not close on a question with chips")
    if checks.get("no_follow_up") and turn.follow_up:
        failures.append(f"an extra question: {turn.follow_up!r}")
    if wanted := checks.get("chips_include"):
        labels = [c.get("label") for c in turn.presentation.get("choices") or []]
        if missing := [w for w in wanted if w not in labels]:
            failures.append(f"chips {labels} lack {missing}")
    if allowed := checks.get("subcategory_in"):
        found = [(p.get("commerce") or {}).get("subcategory") for p in products]
        if not products or any(k not in allowed for k in found):
            failures.append(f"product types {found}, expected only {allowed}")
    if wanted_kinds := checks.get("kinds_include"):
        found_kinds = {(p.get("commerce") or {}).get("subcategory") for p in products}
        if missing_kinds := [k for k in wanted_kinds if k not in found_kinds]:
            failures.append(f"product types {sorted(map(str, found_kinds))} lack {missing_kinds}")
    if (fewest := checks.get("min_seats_all")) is not None:
        seats = [(p.get("commerce") or {}).get("seating_capacity") for p in products]
        if not products or any(s is None or s < fewest for s in seats):
            failures.append(f"seat counts {seats}, expected every card to seat {fewest}+")
    if checks.get("price_ascending"):
        prices = [Decimal(str(p["price_amount"])) for p in products]
        if prices != sorted(prices):
            failures.append(f"prices {[str(p) for p in prices]} are not cheapest first")
    if (banned := checks.get("not_subcategory")) and any(
        (p.get("commerce") or {}).get("subcategory") == banned for p in products
    ):
        failures.append(f"shows {banned}, which it should not")
    if checks.get("within_chosen_budget"):
        ceiling = _upper_bound(turn.chosen_budget)
        over = [
            p["price_amount"]
            for p in products
            if ceiling is not None and Decimal(str(p["price_amount"])) > ceiling
        ]
        if over:
            failures.append(f"prices {over} over the band {turn.chosen_budget!r}")
    return failures


class Conversation:
    """One case's session, sending turns the way the console does."""

    def __init__(self, client: httpx.AsyncClient, base: str, session: str) -> None:
        self.client, self.base, self.session = client, base, session
        self.turns: list[TurnResult] = []

    @property
    def chats(self) -> list[TurnResult]:
        return [t for t in self.turns if t.kind == "chat" and t.status == 200]

    def latest_brief(self, field: str = "brief") -> dict[str, Any]:
        for turn in reversed(self.chats):
            if card := turn.presentation.get(field):
                return card  # type: ignore[no-any-return]
        raise CaseError(f"no {field.replace('_', ' ')} to answer")

    def grid(self, index: int) -> dict[str, Any]:
        grids = [
            t.presentation
            for t in self.chats
            if t.presentation.get("product_source") == "search"
            and t.presentation.get("list_revision") is not None
        ]
        if len(grids) < abs(index):
            raise CaseError(f"no result grid {index} to tick")
        return grids[index]

    def current_picks(self) -> list[dict[str, Any]]:
        for turn in reversed(self.turns):
            if turn.status == 200 and turn.picks is not None:
                return turn.picks
        return []

    def pick_name(self, number: int) -> str:
        pick = next((p for p in self.current_picks() if p.get("pick") == number), None)
        if pick is None:
            raise CaseError(f"no pick {number} in the tray")
        return str(pick.get("name_english"))

    async def _post(self, path: str, body: dict[str, Any], kind: str) -> TurnResult:
        started = time.perf_counter()
        try:
            reply = await self.client.post(
                f"{self.base}{path}",
                json={"session_id": self.session, "store_id": STORE_ID, **body},
                timeout=REQUEST_TIMEOUT_S,
            )
            data = reply.json() if reply.content else {}
            result = TurnResult(reply.status_code, time.perf_counter() - started, data, kind)
        except httpx.HTTPError as exc:
            result = TurnResult(
                0, time.perf_counter() - started, {"error": {"code": type(exc).__name__}}, kind
            )
        self.turns.append(result)
        return result

    async def chat(self, message: str, **action: Any) -> TurnResult:
        return await self._post("/v1/chat", {"message": message, **action}, "chat")

    async def play(self, turn: Any) -> TurnResult:
        if isinstance(turn, str):
            return await self.chat(turn)
        if "say" in turn:
            # The storefront's language, when the case sets one: per request,
            # exactly as the frontend sends it.
            locale = {"locale": turn["locale"]} if "locale" in turn else {}
            return await self.chat(turn["say"], **locale)
        if "search" in turn:
            # Asked by someone who only wants to see results: a card of
            # questions that comes first is skipped, as its "Show me ..."
            # button does with nothing tapped. No card, and it is just said.
            result = await self.chat(turn["search"])
            if result.status == 200 and (result.brief or {}).get("mode") == "ask":
                return await self._answer_card({})
            if result.status == 200 and not result.products and _asks(result.message):
                # The agent asked before showing: the customer who only wants
                # to see says so, in words.
                return await self.chat("Just show me what you have.")
            return result
        if "narrow" in turn:
            return await self._answer_card(turn["narrow"], "narrow_down")
        if "drop" in turn:
            chips = self.chats[-1].presentation.get("brief_chips") or [] if self.chats else []
            chip = next((c for c in chips if c["facet"] == turn["drop"]), None)
            if chip is None:
                # No brief chips: the agents read it in words.
                return await self.chat(f"Drop the {turn['drop']} requirement, please.")
            return await self.chat(
                f"Without {chip['label']}", search_action={"kind": "drop", "facet": chip["facet"]}
            )
        if "answer_card" in turn or "skip_card" in turn:
            return await self._answer_card(turn.get("answer_card") or {})
        if "tick" in turn:
            return await self._tick(turn["tick"])
        if "like" in turn:
            ordinal, grid = self._card(turn["like"])
            return await self._post(
                "/v1/picks",
                {
                    "action": {
                        "kind": "like",
                        "ordinal": ordinal,
                        "list_revision": grid["list_revision"],
                    }
                },
                "picks",
            )
        if "more_like_this" in turn:
            ordinal, grid = self._card(turn["more_like_this"])
            name = grid["products"][ordinal - 1]["name_english"]
            return await self.chat(
                f"More like the {name}",
                product_action={
                    "kind": "more_like_this",
                    "card": {"list_revision": grid["list_revision"], "ordinal": ordinal},
                },
            )
        if "untick" in turn:
            return await self._post(
                "/v1/picks", {"action": {"kind": "deselect", "pick": turn["untick"]}}, "picks"
            )
        if "goes_with" in turn:
            number = int(turn["goes_with"])
            return await self.chat(
                f"What goes with the {self.pick_name(number)}?",
                product_action={"kind": "goes_with", "pick": number},
            )
        if "compare" in turn:
            first, second = (int(n) for n in turn["compare"])
            return await self.chat(
                f"Compare the {self.pick_name(first)} and the {self.pick_name(second)}",
                product_action={"kind": "compare", "picks": [first, second]},
            )
        if "chip" in turn:
            chip = turn["chip"]
            return await self._chip(chip if isinstance(chip, int) else str(chip))
        if "compare_cards" in turn:
            cards = []
            for spec in turn["compare_cards"]:
                grid = self.grid(int(spec.get("grid", -1)))
                cards.append(
                    {"list_revision": grid["list_revision"], "ordinal": int(spec["ordinal"])}
                )
            return await self._post("/v1/comparisons", {"cards": cards}, "comparison")
        if "more_options" in turn:
            return await self.chat(
                "Show me different options", search_action={"kind": "more_options"}
            )
        if "combination" in turn:
            spec = turn["combination"]
            position = spec.get("position")
            words = {"choose": f"I'll take option {position}", "dismiss": f"Not option {position}"}
            return await self.chat(
                words.get(spec["op"], "Show me more seating options"),
                search_action={"kind": "combination", "op": spec["op"], "position": position},
            )
        if "not_this_one" in turn:
            return await self.chat(
                "Not this one",
                search_action={"kind": "exclude", "ordinal": int(turn["not_this_one"])},
            )
        raise CaseError(f"unknown turn {turn!r}")

    async def _answer_card(self, spec: dict[str, Any], field: str = "brief") -> TurnResult:
        if not self.chats or not (self.chats[-1].body.get("presentation") or {}).get(field):
            # No card: the agents ask in words, so the answers are said.
            return await self.chat(_said_answers(spec))
        brief = self.latest_brief(field)
        questions = {q["kind"]: q for q in brief.get("questions", [])}
        if spec.get("each_first"):
            # An opening's questions are the writer's choice, so the case taps
            # the first answer of whichever it asked.
            fields = {"type": "piece", "colour": "colours", "style": "styles"}
            spec = {
                fields.get(kind, kind): [1] if kind in ("colour", "style") else 1
                for kind in questions
            }

        def choose(kind: str, wanted: Any) -> tuple[str, str]:
            question = questions.get(kind)
            if question is None:
                raise CaseError(f"the card has no {kind} question")
            choices = question["choices"]
            if isinstance(wanted, int):
                if not 1 <= wanted <= len(choices):
                    raise CaseError(f"the card's {kind} question has no choice {wanted}")
                chosen = choices[wanted - 1]
            else:
                chosen = next(
                    (c for c in choices if c["label"].casefold() == str(wanted).casefold()), None
                )
                if chosen is None:
                    offered = [c["label"] for c in choices]
                    raise CaseError(f"the card offers no {kind} {wanted!r} (offers {offered})")
            return chosen["key"], chosen["label"]

        answer: dict[str, Any] = {
            "kind": "brief",
            "card": int(brief["card"]) + (50 if spec.get("stale") else 0),
        }
        said: list[str] = []
        budget_label = None
        single = (
            ("piece", "type"),
            ("room", "room"),
            ("people", "people"),
            ("space", "space"),
            ("budget", "budget"),
            ("feel", "feel"),
        )
        for field_name, kind in single:
            if field_name in spec:
                key, label = choose(kind, spec[field_name])
                answer[field_name] = key
                said.append(label)
                if field_name == "budget":
                    budget_label = label
        for field_name, kind in (("colours", "colour"), ("styles", "style")):
            if field_name in spec:
                chosen = [choose(kind, value) for value in spec[field_name]]
                answer[field_name] = [key for key, _ in chosen]
                said.append(" or ".join(label for _, label in chosen))
        message = " · ".join(said) or str(brief.get("skip_label", "Just show me"))
        result = await self.chat(message, search_action=answer)
        result.chosen_budget = budget_label
        return result

    def _card(self, spec: Any) -> tuple[int, dict[str, Any]]:
        """A card named as its position on the latest grid, or `{ordinal, grid}`."""
        if isinstance(spec, int):
            return spec, self.grid(-1)
        return int(spec["ordinal"]), self.grid(int(spec.get("grid", -1)))

    async def _tick(self, spec: Any) -> TurnResult:
        ordinal, grid_index = (
            (int(spec), -1)
            if isinstance(spec, int)
            else (int(spec["ordinal"]), int(spec.get("grid", -1)))
        )
        grid = self.grid(grid_index)
        ticked = await self._post(
            "/v1/picks",
            {
                "action": {
                    "kind": "select",
                    "ordinal": ordinal,
                    "list_revision": grid["list_revision"],
                }
            },
            "picks",
        )
        goes_with = ticked.body.get("goes_with") if ticked.status == 200 else None
        if goes_with:
            # The console opens every new pick as a turn of its own.
            return await self.chat(
                f"I like the {self.pick_name(int(goes_with))}",
                product_action={"kind": "goes_with", "pick": int(goes_with)},
            )
        return ticked

    async def _chip(self, label: str | int) -> TurnResult:
        choices = self.chats[-1].presentation.get("choices") or [] if self.chats else []
        if isinstance(label, int):
            # A chip by its position: for kinds that depend on what the
            # designer chose to show, which the case cannot know in advance.
            if not 1 <= label <= len(choices):
                raise CaseError(f"no chip {label} (offers {[c['label'] for c in choices]})")
            return await self._tap(choices[label - 1])
        chip = next((c for c in choices if c["label"].casefold() == label.casefold()), None)
        if chip is None:
            # A chip whose label carries a live figure ("Separate sofas · from
            # 3,700 SAR") is named by its start, when exactly one starts so.
            starting = [c for c in choices if c["label"].casefold().startswith(label.casefold())]
            chip = starting[0] if len(starting) == 1 else None
        if chip is None:
            raise CaseError(f"no chip {label!r} (offers {[c['label'] for c in choices]})")
        return await self._tap(chip)

    async def _tap(self, chip: dict[str, Any]) -> TurnResult:
        if chip.get("product_action"):
            return await self.chat(chip["value"], product_action=chip["product_action"])
        if chip.get("bundle_action"):
            return await self.chat(chip["value"], bundle_action=chip["bundle_action"])
        if chip.get("search_action"):
            return await self.chat(chip["value"], search_action=chip["search_action"])
        return await self.chat(chip["value"])


async def _run_case(
    client: httpx.AsyncClient, base: str, case: dict[str, Any], gate: asyncio.Semaphore
) -> CaseResult:
    safe_id = "".join(ch if ch.isalnum() else "-" for ch in case["id"])
    conversation = Conversation(client, base, f"eval-{safe_id}-{uuid.uuid4().hex[:8]}")
    problem: str | None = None
    async with gate:
        for step, turn in enumerate(case["turns"], start=1):
            try:
                result = await conversation.play(turn)
            except CaseError as exc:
                problem = f"step {step}: {exc}"
                break
            if result.status != 200:
                break
    turns = conversation.turns or [TurnResult(0, 0.0, {"error": {"code": "no turn"}})]
    if problem is not None:
        return CaseResult(case["id"], turns, [problem])
    chats = [t for t in turns if t.kind == "chat"]
    last = turns[-1]
    earlier_chats = [t for t in chats if t is not last]
    previous = earlier_chats[-1] if earlier_chats else None
    return CaseResult(case["id"], turns, _check(case.get("checks", {}), last, previous))


async def _run_server(base: str, cases: list[dict[str, Any]], concurrency: int) -> list[CaseResult]:
    """Every case, at most `concurrency` at a time, longest first.

    The run takes as long as its slowest conversation, so the many-turn cases
    start first rather than holding up the end. Results come back in case
    order whatever order they ran in.
    """
    gate = asyncio.Semaphore(concurrency)
    longest_first = sorted(range(len(cases)), key=lambda i: -len(cases[i]["turns"]))
    async with httpx.AsyncClient() as client:
        tasks = {
            i: asyncio.create_task(_run_case(client, base, cases[i], gate)) for i in longest_first
        }
        await asyncio.gather(*tasks.values())
    return [tasks[i].result() for i in range(len(cases))]


def _cards(turn: TurnResult) -> str:
    cards = "; ".join(
        f"{(p.get('commerce') or {}).get('subcategory')} {p.get('main_color')} "
        f"{p.get('price_amount')}"
        for p in turn.products[:5]
    )
    if turn.brief:
        asked = ",".join(q.get("kind", "?") for q in turn.brief.get("questions", []))
        cards = f"{cards + ' | ' if cards else ''}card[{turn.brief.get('mode')}: {asked}]"
    if turn.kind == "picks":
        cards = f"picks={len(turn.picks or [])} goes_with={turn.body.get('goes_with')}"
    return cards or "-"


def _describe(turn: Any) -> str:
    return (
        repr(turn)
        if isinstance(turn, str)
        else "<" + ", ".join(f"{k}={v}" for k, v in turn.items()) + ">"
    )


def _report(
    labels: list[str], cases: list[dict[str, Any]], results: dict[str, list[CaseResult]]
) -> None:
    by_id = {label: {r.case_id: r for r in results[label]} for label in labels}
    for case in cases:
        print(f"\n### {case['id']}  ({case['issue']})")
        print(f"customer: {' -> '.join(_describe(t) for t in case['turns'])}")
        for label in labels:
            result = by_id[label][case["id"]]
            verdict = "PASS" if not result.failures else "FAIL: " + "; ".join(result.failures)
            print(f"  [{label}] {verdict}  ({result.last.elapsed_s:.1f}s)")
            print(f"      reply: {result.last.message[:300]}")
            print(f"      cards: {_cards(result.last)}")
    print("\n## Summary")
    for label in labels:
        passed = sum(1 for r in results[label] if not r.failures)
        errors = sum(1 for r in results[label] for t in r.turns if t.status != 200)
        print(f"  {label}: {passed}/{len(cases)} passed, {errors} error responses")


def main(argv: list[str]) -> None:
    options = {a[2:].split("=", 1)[0]: a.split("=", 1)[1] for a in argv if a.startswith("--")}
    servers = dict(arg.split("=", 1) for arg in argv if not arg.startswith("--"))
    if not servers:
        raise SystemExit(
            "usage: run.py label=http://host:port [label=...] [--only=id,id] [--repeat=N]"
            " [--concurrency=N] [--opening=off]"
        )
    cases = yaml.safe_load(CASES_PATH.read_text())["cases"]
    # Cases tagged `opening: off` test the question card that the designer-led
    # opening replaces; they run only against a server with it switched off.
    opening_off = options.get("opening") == "off"
    cases = [c for c in cases if (c.get("opening") == "off") == opening_off]
    if only := options.get("only"):
        wanted = set(only.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    repeat = int(options.get("repeat", "1"))
    # The model is not deterministic: repeating a case shows whether a fix
    # holds or merely got lucky once.
    cases = [
        {**case, "id": f"{case['id']}#{n}"} if repeat > 1 else case
        for case in cases
        for n in range(1, repeat + 1)
    ]
    labels = list(servers)
    concurrency = int(options.get("concurrency", str(CONCURRENCY)))
    if concurrency < 1:
        raise SystemExit("--concurrency must be at least 1")

    async def run_all() -> dict[str, list[CaseResult]]:
        runs = await asyncio.gather(
            *(_run_server(servers[label], cases, concurrency) for label in labels)
        )
        return dict(zip(labels, runs, strict=True))

    results = asyncio.run(run_all())
    _report(labels, cases, results)
    dump = {
        label: [
            {"case": r.case_id, "failures": r.failures, "turns": [t.body for t in r.turns]}
            for r in results[label]
        ]
        for label in labels
    }
    Path(__file__).with_name("last_run.json").write_text(json.dumps(dump, indent=1, default=str))


if __name__ == "__main__":
    main(sys.argv[1:])
