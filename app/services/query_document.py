"""The query side of the semantic document contract.

Deterministic: built from what M7 already produced, with no second model call.
Its shape mirrors the product document indexed in M9B-1, so the two meet in the
same vector space rather than by coincidence.

Nothing that PostgreSQL already enforces goes in. Price wording measurably
degrades colour precision (M9A: 1.00 to 0.80), and dimensions, seat counts,
store and relaxation depth are facts a structured filter has already settled.
"""

from __future__ import annotations

import re

from app.schemas.query import ResolvedSearch

_SEPARATORS = re.compile(r"[-_]+")


def humanise(token: str) -> str:
    """Separator replacement only, exactly as the product documents use."""
    return _SEPARATORS.sub(" ", token).strip()


def has_semantic_intent(resolved: ResolvedSearch) -> bool:
    """Whether anything fuzzy was actually asked for.

    "sofas under 3000" states a type and a bound and nothing else; embedding it
    would invent an order rather than discover one, at the cost of a provider
    call. Ranking is for requests that carry something a filter cannot express.
    """
    return bool(resolved.semantic_text) or bool(resolved.semantic_preferences)


def build_query_document(resolved: ResolvedSearch) -> str:
    """The text embedded for ranking. Deterministic for a given interpretation."""
    request = resolved.request
    lines = [f"Category: {humanise(request.commerce_category)}"]
    if request.commerce_subcategory:
        lines.append(f"Product type: {humanise(request.commerce_subcategory)}")

    for preference in resolved.semantic_preferences:
        # The customer's own words lead. A canonical value is added only when
        # it differs from what they said, so a value that merely restates their
        # wording in registry casing is not doubled.
        value = preference.raw_value.strip()
        canonical = (preference.canonical_value or "").strip()
        if canonical and canonical.lower() != value.lower():
            value = f"{value} ({canonical})"
        lines.append(f"{preference.family.value.capitalize()} preference: {value}")

    if resolved.semantic_text:
        lines.append(f"Customer request: {resolved.semantic_text.strip()}")
    return "\n".join(lines)
