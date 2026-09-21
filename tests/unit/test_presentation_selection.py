"""Presentation selection: the last thing that happens, and only that.

The invariant under test is architectural rather than arithmetic. Taking the
first few of a ranked list is trivial; what matters is that nothing which
decides eligibility or order can see the limit, because a presentation bound
applied earlier would choose the customer's options before anything had judged
them (CLAUDE.md 16.1).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from app.core.exceptions import InvalidRequestError
from app.services.presentation import select_for_presentation

APP = Path(__file__).parents[2] / "app"

# Everything that decides eligibility or order. None of it may learn how many
# products will be shown.
RETRIEVAL_MODULES = (
    "repositories/products.py",
    "services/discovery.py",
    "services/controlled_search.py",
    "services/relaxation.py",
    "services/dimension_policy.py",
    "services/semantic_ranking.py",
    "services/query_document.py",
)


def test_the_first_products_survive_in_ranked_order() -> None:
    assert select_for_presentation([7, 3, 9, 1, 5], limit=3) == (7, 3, 9)


def test_order_is_never_recomputed() -> None:
    """The caller ranked these. Re-sorting would silently overrule M9."""
    assert select_for_presentation([9, 1, 5], limit=3) == (9, 1, 5)


def test_a_shorter_pool_is_an_ordinary_outcome() -> None:
    assert select_for_presentation([4, 2], limit=10) == (4, 2)


def test_an_empty_pool_selects_nothing() -> None:
    assert select_for_presentation([], limit=3) == ()


@pytest.mark.parametrize("limit", [0, -1])
def test_a_limit_below_one_is_refused(limit: int) -> None:
    """A limit of zero would present nothing while a search had succeeded."""
    with pytest.raises(InvalidRequestError):
        select_for_presentation([1, 2, 3], limit=limit)


def test_selection_does_not_mutate_the_ranked_list() -> None:
    ranked = [5, 4, 3]
    select_for_presentation(ranked, limit=1)

    assert ranked == [5, 4, 3]


# ── the leak guard ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("module", RETRIEVAL_MODULES)
def test_retrieval_never_imports_presentation_selection(module: str) -> None:
    tree = ast.parse((APP / module).read_text())

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert "presentation" not in (node.module or ""), module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert "presentation" not in alias.name, module


@pytest.mark.parametrize("module", RETRIEVAL_MODULES)
def test_retrieval_never_mentions_a_presentation_limit(module: str) -> None:
    """Named so the next person cannot reintroduce the bound under a new name."""
    source = (APP / module).read_text()

    assert "presentation_limit" not in source, module
    assert "select_for_presentation" not in source, module


def test_selection_knows_nothing_about_ranking() -> None:
    """It takes ids, so it cannot start ordering by similarity or depth."""
    source = (APP / "services/presentation.py").read_text()

    for forbidden in ("similarity", "relaxation_depth", "semantic", "score"):
        assert forbidden not in source.split('"""')[2], forbidden
