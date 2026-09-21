"""Live evaluation of query understanding against the golden set.

Calls the configured provider, so it is never part of pytest. Run with::

    ZORY_LLM__API_KEY=... ZORY_LLM__MODEL=... python -m evals.query_understanding.run

Reports what the model actually produced. Failures are signal about the prompt
or the model, and must not be patched with per-query special cases.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from app.core.config import get_settings
from app.core.exceptions import TaxonomyValidationError
from app.integrations.llm import OpenAIStructuredClient
from app.schemas.query import (
    ClarificationRequired,
    QueryInterpretation,
    ResolvedSearch,
    UnresolvedStrictRequirement,
    UnsupportedDimensionRequirement,
    UnsupportedRequirement,
)
from app.services.query_understanding import QueryUnderstandingService
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.dimensions import load_dimension_semantics
from app.taxonomy.registry import load_taxonomy

GOLDEN_PATH = Path(__file__).parent / "golden_v2.yaml"


STRENGTHS = {
    "commerce_subcategory_strength": "subcategory",
    "price_min_strength": "price_min",
    "price_max_strength": "price_max",
    "seating_capacity_min_strength": "seating_min",
    "seating_capacity_max_strength": "seating_max",
}


def _render_preferences(outcome: QueryInterpretation) -> list[str]:
    if not isinstance(outcome, ResolvedSearch):
        return []
    return sorted(
        f"{p.family.value}:{p.canonical_value or '-'}:{p.strength.value}"
        for p in outcome.semantic_preferences
    )


def _num(value: Any) -> str:
    """Compare numbers by value, so 220 and 220.0 are the same expectation.

    "-" is the absent-value placeholder used in the golden file and passes
    through unchanged.
    """
    if value is None or value == "-":
        return "-"
    # Plain decimal form: normalize() alone renders 220 as 2.2E+2.
    return format(Decimal(str(value)).normalize(), "f")


def _render_dimensions(outcome: QueryInterpretation) -> list[str]:
    if not isinstance(outcome, ResolvedSearch | UnsupportedDimensionRequirement):
        return []
    return sorted(
        f"{d.role.value}:{d.kind.value}:{_num(d.min_cm)}:{_num(d.max_cm)}:{_num(d.target_cm)}"
        for d in outcome.request.dimensions
    )


def _render_dimension_strengths(outcome: QueryInterpretation) -> list[str]:
    """What the semantics recorded, keyed by role - never read from the request."""
    if isinstance(outcome, ClarificationRequired):
        return []
    return sorted(
        f"{entry.role.value}:{entry.strength.value}"
        for entry in outcome.semantics.dimensions
    )


def _render_planar_strength(outcome: QueryInterpretation) -> str | None:
    if isinstance(outcome, ClarificationRequired):
        return None
    strength = outcome.semantics.planar_dimension_strength()
    return None if strength is None else strength.value


def _render_planar(outcome: QueryInterpretation) -> str | None:
    if not isinstance(outcome, ResolvedSearch):
        return None
    planar = outcome.request.planar_dimensions
    if planar is None:
        return None
    low, high = planar.sides
    return f"{_num(low)}x{_num(high)}"


def _expected_dimensions(case: dict[str, Any]) -> list[str]:
    return sorted(
        ":".join(_num(p) if i >= 2 else p for i, p in enumerate(entry.split(":")))
        for entry in (case.get("dimensions") or [])
    )


def _render_refused(outcome: UnsupportedDimensionRequirement) -> list[str]:
    """Role, reason and the strength preserved as provenance."""
    return sorted(
        f"{d.role.value}:{d.reason.value}:{d.strength.value}"
        for d in outcome.unsupported_dimensions
    )


def _expected_planar(case: dict[str, Any]) -> str | None:
    raw = case.get("planar")
    if raw is None:
        return None
    a, b = (Decimal(x) for x in raw.split("x"))
    low, high = (a, b) if a <= b else (b, a)
    return f"{_num(low)}x{_num(high)}"


def _expected(case: dict[str, Any]) -> str:
    if case["outcome"] == "any":
        return f"anything except {case['forbids']}"
    if case["outcome"] == "clarification":
        return f"clarification({case['reason']})"
    parts = [f"{case['commerce_category']}/{case.get('commerce_subcategory')}"]
    for key in ("seating_capacity_min", "seating_capacity_max", "price_min", "price_max"):
        if case.get(key) is not None:
            parts.append(f"{key}={case[key]}")
    for key, label in STRENGTHS.items():
        if case.get(key) is not None:
            parts.append(f"{label}:{case[key]}")
    if case.get("sort") and case["sort"] != "default":
        parts.append(f"sort={case['sort']}")
    if case["outcome"] == "unsupported":
        parts.append(f"unsupported={case['unsupported']}")
    if case["outcome"] == "unresolved":
        parts.append(f"unresolved={case['unresolved']}")
    if case["outcome"] == "unsupported_dimension":
        parts.append(f"unsupported_dimensions={sorted(case['unsupported_dimensions'])}")
    if case.get("dimensions"):
        parts.append(f"dimensions={_expected_dimensions(case)}")
    if case.get("dimension_strengths"):
        parts.append(f"dimension_strengths={sorted(case['dimension_strengths'])}")
    if case.get("planar"):
        parts.append(f"planar={_expected_planar(case)}")
    if case.get("planar_strength"):
        parts.append(f"planar_strength={case['planar_strength']}")
    for key in ("colors_any_of", "styles_all_of"):
        if case.get(key):
            parts.append(f"{key}={sorted(case[key])}")
    if case.get("preferences"):
        parts.append(f"preferences={sorted(case['preferences'])}")
    return " ".join(parts)


def _actual(outcome: QueryInterpretation) -> str:
    if isinstance(outcome, ClarificationRequired):
        return f"clarification({outcome.reason})"
    request, semantics = outcome.request, outcome.semantics
    parts = [f"{request.commerce_category}/{request.commerce_subcategory}"]
    if request.seating_capacity is not None:
        parts.append(
            f"capacity={request.seating_capacity.min_capacity}"
            f"..{request.seating_capacity.max_capacity}"
        )
    if request.price is not None:
        parts.append(
            f"price={request.price.min_amount}..{request.price.max_amount}"
            f" {request.price.currency}"
        )
    for label in ("subcategory", "price_min", "price_max", "seating_min", "seating_max"):
        if (strength := getattr(semantics, label)) is not None:
            parts.append(f"{label}:{strength}")
    if request.sort.value != "default":
        parts.append(f"sort={request.sort.value}")
    if isinstance(outcome, UnsupportedRequirement):
        parts.append(f"unsupported={[f.value for f in outcome.unsupported]}")
    if isinstance(outcome, UnresolvedStrictRequirement):
        parts.append(f"unresolved={sorted({u.family.value for u in outcome.unresolved})}")
    if isinstance(outcome, UnsupportedDimensionRequirement):
        parts.append(f"unsupported_dimensions={_render_refused(outcome)}")
    if rendered := _render_dimensions(outcome):
        parts.append(f"dimensions={rendered}")
    if rendered_strengths := _render_dimension_strengths(outcome):
        parts.append(f"dimension_strengths={rendered_strengths}")
    if rendered_planar := _render_planar(outcome):
        parts.append(f"planar={rendered_planar}")
    if (planar_strength := _render_planar_strength(outcome)) is not None:
        parts.append(f"planar_strength={planar_strength}")
    if request.colors_any_of:
        parts.append(f"colors_any_of={sorted(request.colors_any_of)}")
    if request.styles_all_of:
        parts.append(f"styles_all_of={sorted(request.styles_all_of)}")
    if preferences := _render_preferences(outcome):
        parts.append(f"preferences={preferences}")
    return " ".join(parts)


def _forbidden(case: dict[str, Any], rendered: str) -> bool:
    return any(value in rendered for value in case.get("forbids", []))


def _matches(case: dict[str, Any], outcome: QueryInterpretation) -> bool:
    if case["outcome"] == "any":
        return not _forbidden(case, _actual(outcome))
    if case["outcome"] == "clarification":
        return (
            isinstance(outcome, ClarificationRequired)
            and str(outcome.reason) == case["reason"]
        )
    if case["outcome"] == "unsupported":
        if not isinstance(outcome, UnsupportedRequirement):
            return False
        if [f.value for f in outcome.unsupported] != case["unsupported"]:
            return False
    elif case["outcome"] == "unsupported_dimension":
        if not isinstance(outcome, UnsupportedDimensionRequirement):
            return False
        if _render_refused(outcome) != sorted(case["unsupported_dimensions"]):
            return False
    elif case["outcome"] == "unresolved":
        if not isinstance(outcome, UnresolvedStrictRequirement):
            return False
        families = sorted({u.family.value for u in outcome.unresolved})
        if families != sorted(case["unresolved"]):
            return False
    elif not isinstance(outcome, ResolvedSearch):
        return False

    request, semantics = outcome.request, outcome.semantics
    if not case.get("skip_category"):
        if request.commerce_category != case["commerce_category"]:
            return False
        if request.commerce_subcategory != case.get("commerce_subcategory"):
            return False

    if case.get("dimensions") is not None and _render_dimensions(
        outcome
    ) != _expected_dimensions(case):
        return False
    if case.get("dimension_strengths") is not None and _render_dimension_strengths(
        outcome
    ) != sorted(case["dimension_strengths"]):
        return False
    if _render_planar(outcome) != _expected_planar(case):
        return False
    if case.get("planar_strength") is not None and _render_planar_strength(
        outcome
    ) != case["planar_strength"]:
        return False
    if sorted(request.colors_any_of) != sorted(case.get("colors_any_of") or []):
        return False
    if sorted(request.styles_all_of) != sorted(case.get("styles_all_of") or []):
        return False
    if case.get("preferences") is not None and _render_preferences(outcome) != sorted(
        case["preferences"]
    ):
        return False

    capacity = request.seating_capacity
    low = capacity.min_capacity if capacity else None
    high = capacity.max_capacity if capacity else None
    if (low, high) != (
        case.get("seating_capacity_min"),
        case.get("seating_capacity_max"),
    ):
        return False

    price = request.price
    for bound in ("price_min", "price_max"):
        want = case.get(bound)
        got = getattr(price, f"{bound.split('_')[1]}_amount", None) if price else None
        if want is None:
            if got is not None:
                return False
        elif got is None or str(got) != str(want):
            return False
    if case.get("price_currency") and (price is None or price.currency != case["price_currency"]):
        return False

    # Strengths are only asserted where the case states one.
    for key, label in STRENGTHS.items():
        want_strength = case.get(key)
        if want_strength is not None and str(getattr(semantics, label)) != want_strength:
            return False

    return request.sort.value == (case.get("sort") or "default")


async def main() -> int:
    settings = get_settings()
    cases: list[dict[str, Any]] = yaml.safe_load(GOLDEN_PATH.read_text())["cases"]
    client = OpenAIStructuredClient(settings.llm)
    taxonomy = load_taxonomy()
    service = QueryUnderstandingService(
        client,
        taxonomy,
        load_catalog_attributes(),
        load_dimension_semantics(taxonomy=taxonomy),
    )

    print(f"model={settings.llm.model}  cases={len(cases)}\n")
    passed = 0
    latencies: list[float] = []
    try:
        for case in cases:
            started = time.perf_counter()
            try:
                outcome = await service.interpret(case["message"])
                actual = _actual(outcome)
                ok = _matches(case, outcome)
            except TaxonomyValidationError as exc:
                # The validator refused the model's value: for the injection
                # case that is a pass-by-rejection, not a crash.
                actual = f"REJECTED by validation ({exc.code})"
                # Rejection means nothing unapproved survived, which satisfies
                # a prohibition case and a clarification expectation alike.
                ok = case["outcome"] in {"clarification", "any"}
            latency = (time.perf_counter() - started) * 1000
            latencies.append(latency)
            passed += ok
            print(f"[{'PASS' if ok else 'FAIL'}] {case['message']!r}  ({latency:.0f} ms)")
            print(f"       expected: {_expected(case)}")
            print(f"       actual:   {actual}")
    finally:
        await client.close()

    mean = sum(latencies) / len(latencies) if latencies else 0.0
    print(f"\n{passed}/{len(cases)} passed   mean latency {mean:.0f} ms")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
