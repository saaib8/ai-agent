"""Architectural boundaries around controlled relaxation.

M8 is deterministic policy over M6's results. It must not acquire a dependency
on language models, vector search or SQL, and M6 must not learn about
constraint strength.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).parents[2] / "app"

M8_MODULES = (
    APP / "services/relaxation.py",
    APP / "services/controlled_search.py",
    APP / "schemas/relaxation.py",
)
M6_MODULES = (
    APP / "services/discovery.py",
    APP / "repositories/products.py",
    APP / "schemas/discovery.py",
)

FORBIDDEN_IN_M8 = (
    "openai",
    "langgraph",
    "pinecone",
    "sqlalchemy",
)


def _imported_roots(module: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(module.read_text())):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _imported_modules(module: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(module.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("module", M8_MODULES, ids=lambda m: m.name)
def test_relaxation_imports_no_model_or_vector_or_sql_library(module: Path) -> None:
    roots = _imported_roots(module)
    for forbidden in FORBIDDEN_IN_M8:
        assert forbidden not in roots, f"{module.name} imports {forbidden}"


@pytest.mark.parametrize("module", M8_MODULES, ids=lambda m: m.name)
def test_relaxation_does_not_reach_the_llm_integration(module: Path) -> None:
    assert "app.integrations.llm" not in _imported_modules(module)


def test_relaxation_depends_on_discovery_not_the_repository() -> None:
    """The service orchestrates searches; it owns no SQL."""
    imported = _imported_modules(APP / "services/controlled_search.py")

    assert "app.services.discovery" in imported
    assert "app.repositories.products" not in imported
    assert not any(name.startswith("app.repositories") for name in imported)
    assert not any(name.startswith("app.db") for name in imported)


def test_the_planner_touches_no_infrastructure_at_all() -> None:
    """Pure policy: no discovery, no repository, no database."""
    imported = _imported_modules(APP / "services/relaxation.py")

    assert not any(
        name.startswith(("app.repositories", "app.db", "app.integrations"))
        for name in imported
    )
    assert "app.services.discovery" not in imported


@pytest.mark.parametrize("module", M6_MODULES, ids=lambda m: m.name)
def test_discovery_never_learns_about_relaxation(module: Path) -> None:
    """M6 stays exact discovery: it must not import the policy or its vocabulary."""
    source = module.read_text()

    assert "ConstraintStrength" not in source, module.name
    assert "ConstraintSemantics" not in source, module.name
    assert "RelaxationPlanner" not in source, module.name
    assert "app.schemas.relaxation" not in _imported_modules(module), module.name
    assert "app.services.relaxation" not in _imported_modules(module), module.name


def test_query_understanding_does_not_depend_on_relaxation() -> None:
    """M7 understands; M8 decides what may widen. The arrow points one way."""
    imported = _imported_modules(APP / "services/query_understanding.py")

    assert "app.services.relaxation" not in imported
    assert "app.services.controlled_search" not in imported


def test_the_policy_carries_no_literal_thresholds() -> None:
    """Target, percentages and seat delta all come from settings."""
    for module in (APP / "services/relaxation.py", APP / "services/controlled_search.py"):
        source = module.read_text()
        assert "0.10" not in source, module.name
        assert "0.20" not in source, module.name
        # The only bare integers permitted are the structural floor of one seat
        # and zero money, both named constants.
        assert "target_candidates = 5" not in source, module.name


def test_no_subcategory_alternative_map_exists() -> None:
    """A sibling or fallback map is exactly what V1 must not contain.

    Checks for taxonomy *values* rather than prose: a docstring explaining why
    there is no alternative policy should stay, a hardcoded mapping should not.
    """
    from app.taxonomy.registry import load_taxonomy

    taxonomy = load_taxonomy()
    values = {
        subcategory
        for category in taxonomy.categories
        for subcategory in taxonomy.subcategories(category)
    } | set(taxonomy.categories)

    for module in M8_MODULES:
        source = module.read_text()
        found = sorted(value for value in values if f'"{value}"' in source)
        assert found == [], f"{module.name} hardcodes taxonomy values: {found}"


def test_relaxation_never_writes_the_subcategory_field() -> None:
    """Every widened request carries the customer's own category and subcategory.

    Asserted on the planned requests rather than on the source text: the
    reconstruction now carries non-widened fields generically, so there is no
    per-field line to grep for, and behaviour is the thing that matters
    anyway.
    """
    from decimal import Decimal

    from app.core.config import RelaxationSettings
    from app.schemas.discovery import PriceConstraint, ProductSearchRequest
    from app.schemas.query import (
        ConstraintSemantics,
        ConstraintStrength,
        ResolvedSearch,
    )
    from app.services.relaxation import RelaxationPlanner

    resolved = ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category="seating",
            commerce_subcategory="sofa",
            price=PriceConstraint(currency="SAR", max_amount=Decimal("5000")),
        ),
        semantics=ConstraintSemantics(price_max=ConstraintStrength.APPROXIMATE),
    )
    planned = RelaxationPlanner(RelaxationSettings()).plan(resolved)

    assert planned
    assert all(p.request.commerce_category == "seating" for p in planned)
    assert all(p.request.commerce_subcategory == "sofa" for p in planned)
    assert "SUBCATEGORY" not in (APP / "services/relaxation.py").read_text()
