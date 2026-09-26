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


@dataclass
class TurnResult:
    status: int
    elapsed_s: float
    body: dict[str, Any] = field(default_factory=dict)

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
    if checks.get("engages"):
        asks = "?" in turn.message
        if not (products or turn.has_comparison or turn.has_room or turn.has_piece_picker or asks):
            failures.append("dead end: no products, comparison, room, chips or question")
    return failures


async def _run_case(
    client: httpx.AsyncClient, base: str, case: dict[str, Any], gate: asyncio.Semaphore
) -> CaseResult:
    safe_id = "".join(ch if ch.isalnum() else "-" for ch in case["id"])
    session = f"eval-{safe_id}-{uuid.uuid4().hex[:8]}"
    turns: list[TurnResult] = []
    async with gate:
        for message in case["turns"]:
            started = time.perf_counter()
            try:
                reply = await client.post(
                    f"{base}/v1/chat",
                    json={"session_id": session, "store_id": STORE_ID, "message": message},
                    timeout=REQUEST_TIMEOUT_S,
                )
                body = reply.json() if reply.content else {}
                turns.append(TurnResult(reply.status_code, time.perf_counter() - started, body))
            except httpx.HTTPError as exc:
                turns.append(
                    TurnResult(
                        0, time.perf_counter() - started, {"error": {"code": type(exc).__name__}}
                    )
                )
                break
            if reply.status_code != 200:
                break
    previous = turns[-2] if len(turns) > 1 else None
    return CaseResult(case["id"], turns, _check(case.get("checks", {}), turns[-1], previous))


async def _run_server(base: str, cases: list[dict[str, Any]]) -> list[CaseResult]:
    gate = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient() as client:
        return list(await asyncio.gather(*(_run_case(client, base, c, gate) for c in cases)))


def _cards(turn: TurnResult) -> str:
    return (
        "; ".join(f"{p.get('main_color')} {p.get('price_amount')}" for p in turn.products[:5])
        or "-"
    )


def _report(
    labels: list[str], cases: list[dict[str, Any]], results: dict[str, list[CaseResult]]
) -> None:
    by_id = {label: {r.case_id: r for r in results[label]} for label in labels}
    for case in cases:
        print(f"\n### {case['id']}  ({case['issue']})")
        print(f"customer: {' -> '.join(repr(t) for t in case['turns'])}")
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
