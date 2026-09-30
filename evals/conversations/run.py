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

CASES_PATH = Path(__file__).parent / "cases.yaml"
STORE_ID = 50
REQUEST_TIMEOUT_S = 240.0
CONCURRENCY = 4
FALLBACK_PREFIX = "Sorry, I didn't quite catch that"
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


def _check(
    checks: dict[str, Any], turn: TurnResult, previous: TurnResult | None = None
) -> list[str]:
    """Every failed check on the final reply, as a short reason."""
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
    if checks.get("not_fallback") and turn.message.startswith(FALLBACK_PREFIX):
        failures.append("fallback reply")
    if (minimum := checks.get("min_products")) and len(products) < minimum:
        failures.append(f"{len(products)} products < {minimum}")
    if (most := checks.get("max_products")) is not None and len(products) > most:
        failures.append(f"{len(products)} products > {most} - it should have asked first")
    if (least := checks.get("min_combinations")) and len(turn.combinations) < least:
        failures.append(f"{len(turn.combinations)} combinations < {least}")
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
    if floor := checks.get("some_dimension_above"):
        field, bound = floor["field"], Decimal(str(floor["value"]))
        sizes = [(p.get("dimensions") or {}).get(field) for p in products]
        if not any(v is not None and Decimal(str(v)) > bound for v in sizes):
            failures.append(f"{field} {sizes} all at most {bound} - the old limit still applies")
    if checks.get("admits_no_match") and not _NO_MATCH.search(
        turn.message.lower().replace("\u2019", "'")
    ):
        failures.append("reply does not say that nothing matched")
    if (words := checks.get("mentions")) and not any(w in turn.message.lower() for w in words):
        failures.append(f"reply mentions none of {words}")
    if checks.get("piece_picker") and not turn.has_piece_picker:
        failures.append("no piece chips shown")
    if (status := checks.get("room_status")) and turn.room.get("status") != status:
        failures.append(f"room status {turn.room.get('status')!r}, expected {status!r}")
    if kinds := checks.get("room_has"):
        present = {(i.get("commerce") or {}).get("subcategory") for i in turn.room.get("items", [])}
        if not set(kinds) <= present:
            failures.append(f"room holds {sorted(map(str, present))}, missing some of {kinds}")
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
        asks = "?" in turn.message
        shown = (
            products or turn.has_comparison or turn.has_room or turn.has_piece_picker or turn.brief
        )
        if not (shown or asks):
            failures.append("dead end: no products, comparison, room, chips, card or question")
    return failures


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
    brief = turn.brief
    kinds = [q.get("kind") for q in (brief or {}).get("questions", [])]
    if (want := checks.get("card")) is not None:
        asking = brief is not None and brief.get("mode") == "ask"
        if want and not asking:
            failures.append("no question card - it searched straight away")
        if not want and asking:
            failures.append("a question card was shown")
    if (mode := checks.get("card_mode")) and (brief or {}).get("mode") != mode:
        failures.append(f"card mode {(brief or {}).get('mode')!r}, expected {mode!r}")
    if (asks := checks.get("card_asks")) and not set(asks) <= set(kinds):
        failures.append(f"card asks {kinds}, missing some of {asks}")
    if (skips := checks.get("card_skips")) and set(skips) & set(kinds):
        failures.append(f"card asks {sorted(set(skips) & set(kinds))} it was already told")
    if (first := checks.get("card_first")) and (not kinds or kinds[0] != first):
        failures.append(f"card's first question is {kinds[:1]}, expected {first}")
    if labels := checks.get("card_kinds_include"):
        kind_question: dict[str, Any] = next(
            (q for q in (brief or {}).get("questions", []) if q.get("kind") == "type"), {}
        )
        offered = [c.get("label") for c in kind_question.get("choices", [])]
        missing = [label for label in labels if label not in offered]
        if missing:
            failures.append(f"card offers kinds {offered}, missing {missing}")
    if first_kind := checks.get("card_kind_first"):
        kind_question = next(
            (q for q in (brief or {}).get("questions", []) if q.get("kind") == "type"), {}
        )
        labels = [c.get("label") for c in kind_question.get("choices", [])]
        if not labels or labels[0] != first_kind:
            failures.append(f"card's kinds start {labels[:2]}, expected {first_kind!r} first")
    if first_chip := checks.get("first_chip"):
        chips = [c.get("label") for c in turn.presentation.get("choices") or []]
        if not chips or chips[0] != first_chip:
            failures.append(f"chips start {chips[:2]}, expected {first_chip!r} first")
    if (want := checks.get("best_match")) is not None and bool(
        turn.presentation.get("best_match")
    ) != want:
        failures.append(
            f"best_match is {bool(turn.presentation.get('best_match'))}, expected {want}"
        )
    if (count := checks.get("picks")) is not None:
        got = len(turn.picks or [])
        if got != count:
            failures.append(f"{got} picks in the tray, expected {count}")
    if (want := checks.get("cross_sell")) is not None:
        got = bool(turn.presentation.get("focus"))
        if got != want:
            failures.append("no cross-sell after the pick" if want else "an unexpected cross-sell")
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
        if turn.kind != "comparison" or len(columns) != 2 or not turn.body.get("message"):
            failures.append("no pop-up comparison of two products with a take")
    if checks.get("no_follow_up") and turn.follow_up:
        failures.append(f"an extra question: {turn.follow_up!r}")
    if (least := checks.get("companion_chips_min")) is not None:
        chips = [c for c in turn.presentation.get("choices") or [] if c.get("product_action")]
        if len(chips) < least:
            failures.append(f"{len(chips)} companion chips < {least}")
    if allowed := checks.get("subcategory_in"):
        found = [(p.get("commerce") or {}).get("subcategory") for p in products]
        if not products or any(k not in allowed for k in found):
            failures.append(f"product types {found}, expected only {allowed}")
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

    def latest_brief(self) -> dict[str, Any]:
        for turn in reversed(self.chats):
            if turn.brief:
                return turn.brief
        raise CaseError("no question card to answer")

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
            return await self.chat(turn["say"])
        if "search" in turn:
            # Asked by someone who only wants to see results: a card of
            # questions that comes first is skipped, as its "Show me ..."
            # button does with nothing tapped. No card, and it is just said.
            result = await self.chat(turn["search"])
            if result.status == 200 and (result.brief or {}).get("mode") == "ask":
                return await self._answer_card({})
            return result
        if "answer_card" in turn or "skip_card" in turn:
            return await self._answer_card(turn.get("answer_card") or {})
        if "tick" in turn:
            return await self._tick(turn["tick"])
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
            return await self._chip(str(turn["chip"]))
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
        if "not_this_one" in turn:
            return await self.chat(
                "Not this one",
                search_action={"kind": "exclude", "ordinal": int(turn["not_this_one"])},
            )
        raise CaseError(f"unknown turn {turn!r}")

    async def _answer_card(self, spec: dict[str, Any]) -> TurnResult:
        brief = self.latest_brief()
        questions = {q["kind"]: q for q in brief.get("questions", [])}

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
        for field_name, kind in (("piece", "type"), ("budget", "budget"), ("feel", "feel")):
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
        message = " · ".join(said) or f"Just {str(brief.get('submit_label', 'show me')).lower()}"
        result = await self.chat(message, search_action=answer)
        result.chosen_budget = budget_label
        return result

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
            # The console asks what goes with the first pick of its kind.
            return await self.chat(
                f"I like the {self.pick_name(int(goes_with))}",
                product_action={"kind": "goes_with", "pick": int(goes_with)},
            )
        return ticked

    async def _chip(self, label: str) -> TurnResult:
        choices = self.chats[-1].presentation.get("choices") or [] if self.chats else []
        chip = next((c for c in choices if c["label"].casefold() == label.casefold()), None)
        if chip is None:
            raise CaseError(f"no chip {label!r} (offers {[c['label'] for c in choices]})")
        if chip.get("product_action"):
            return await self.chat(chip["value"], product_action=chip["product_action"])
        if chip.get("bundle_action"):
            return await self.chat(chip["value"], bundle_action=chip["bundle_action"])
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


async def _run_server(base: str, cases: list[dict[str, Any]]) -> list[CaseResult]:
    gate = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient() as client:
        return list(await asyncio.gather(*(_run_case(client, base, c, gate) for c in cases)))


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
        )
    cases = yaml.safe_load(CASES_PATH.read_text())["cases"]
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

    async def run_all() -> dict[str, list[CaseResult]]:
        runs = await asyncio.gather(*(_run_server(servers[label], cases) for label in labels))
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
