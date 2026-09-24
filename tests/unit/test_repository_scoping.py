"""Store scoping is structural, so it is tested structurally.

The registry below must name every public method on the repository. Adding a
query without registering it fails the coverage test, and every registered
method's compiled SQL is checked for the ``store_id`` and ``is_active``
predicates. A new unscoped query therefore cannot reach main
(CLAUDE.md 8, 20.2).
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal
from typing import Any, cast

import pytest
from app.repositories.products import ProductRepository
from app.repositories.stores import StoreRepository
from app.schemas.discovery import (
    PriceConstraint,
    ProductSearchRequest,
    SeatingCapacityConstraint,
)
from app.schemas.retailer import RetailerContext
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.ext.asyncio import AsyncSession

STORE_ID = 7
OTHER_STORE_ID = 8
CONTEXT = RetailerContext(store_id=STORE_ID)

# SQLAlchemy's dialect factory carries no annotations.
_PG: Dialect = postgresql.dialect()  # type: ignore[no-untyped-call]


class _FakeResult:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def __iter__(self) -> Any:
        return iter(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalar_one(self) -> Any:
        return self._rows[0] if self._rows else 0

    def all(self) -> list[Any]:
        return list(self._rows)


class RecordingSession:
    """Captures the statements a repository issues without touching a database."""

    def __init__(self, rows: Sequence[Any] = ()) -> None:
        self.statements: list[Select[Any]] = []
        self._rows = rows

    async def execute(self, statement: Select[Any], *_: Any, **__: Any) -> _FakeResult:
        self.statements.append(statement)
        return _FakeResult(self._rows)


def _sql(statement: Select[Any]) -> str:
    compiled = statement.compile(dialect=_PG, compile_kwargs={"literal_binds": True})
    return str(compiled)


def _repository(session: RecordingSession) -> ProductRepository:
    return ProductRepository(cast(AsyncSession, session))


# name -> a call that exercises the method against the recording session.
PRODUCT_QUERIES: dict[str, Callable[[ProductRepository], Awaitable[Any]]] = {
    "get_by_ids": lambda repo: repo.get_by_ids([1, 2], CONTEXT),
    "count_active": lambda repo: repo.count_active(CONTEXT),
    "search": lambda repo: repo.search(
        ProductSearchRequest(commerce_category="seating"), CONTEXT, limit=10
    ),
    "search_eligible_ids": lambda repo: repo.search_eligible_ids(
        ProductSearchRequest(commerce_category="seating"), CONTEXT
    ),
    "search_eligible_pool": lambda repo: repo.search_eligible_pool(
        ProductSearchRequest(commerce_category="seating"), CONTEXT
    ),
    "supported_commerce_types": lambda repo: repo.supported_commerce_types(CONTEXT),
    "visual_categories": lambda repo: repo.visual_categories(CONTEXT),
    "ids_for_visual_matches": lambda repo: repo.ids_for_visual_matches(
        ["vector-1"], ["https://example.test/1"], CONTEXT
    ),
}


def test_every_public_product_query_is_registered_here() -> None:
    """A new repository method must be added to the scoping checks below."""
    public = {
        name
        for name, member in inspect.getmembers(ProductRepository, inspect.isfunction)
        if not name.startswith("_")
    }
    assert public == set(PRODUCT_QUERIES), (
        "unregistered repository method(s): "
        f"{sorted(public.symmetric_difference(PRODUCT_QUERIES))}"
    )


@pytest.mark.parametrize("method_name", sorted(PRODUCT_QUERIES))
async def test_every_product_query_is_scoped_to_store_and_active(method_name: str) -> None:
    session = RecordingSession()
    await PRODUCT_QUERIES[method_name](_repository(session))

    assert session.statements, f"{method_name} issued no statement"
    for statement in session.statements:
        sql = _sql(statement)
        assert f"core_product.store_id = {STORE_ID}" in sql, sql
        assert "core_product.is_active IS true" in sql, sql


async def test_a_different_context_scopes_to_a_different_store() -> None:
    session = RecordingSession()
    await _repository(session).count_active(RetailerContext(store_id=OTHER_STORE_ID))

    sql = _sql(session.statements[0])
    assert f"core_product.store_id = {OTHER_STORE_ID}" in sql
    assert f"core_product.store_id = {STORE_ID}" not in sql


async def test_get_by_ids_with_no_ids_issues_no_query() -> None:
    session = RecordingSession()
    assert await _repository(session).get_by_ids([], CONTEXT) == []
    assert session.statements == []


async def test_get_by_ids_deduplicates_and_orders_its_filter() -> None:
    session = RecordingSession()
    await _repository(session).get_by_ids([3, 1, 3], CONTEXT)

    sql = _sql(session.statements[0])
    assert "core_product.id IN (1, 3)" in sql


async def test_repository_takes_no_store_id_argument() -> None:
    """Scope arrives as a RetailerContext; a caller cannot pass a bare id."""
    for name in PRODUCT_QUERIES:
        parameters = inspect.signature(getattr(ProductRepository, name)).parameters
        assert "store_id" not in parameters, name
        assert "context" in parameters, name
        assert parameters["context"].annotation == "RetailerContext", name


async def test_store_lookup_requires_an_active_store() -> None:
    session = RecordingSession()
    await StoreRepository(cast(AsyncSession, session)).get_active(STORE_ID)

    sql = _sql(session.statements[0])
    assert f"core_store.id = {STORE_ID}" in sql
    assert "core_store.active_status IS true" in sql


# ── the eligible-id pool must match search() exactly ────────────────────────


def _request(**kwargs: Any) -> ProductSearchRequest:
    return ProductSearchRequest(commerce_category="seating", **kwargs)


REQUESTS = [
    _request(),
    _request(commerce_subcategory="sofa"),
    _request(price=PriceConstraint.at_most(Decimal("5000"), "SAR")),
    _request(seating_capacity=SeatingCapacityConstraint.exactly(3)),
    _request(colors_any_of=("Beige",)),
    _request(styles_all_of=("Modern",)),
    _request(
        commerce_subcategory="sofa",
        price=PriceConstraint.between(Decimal("1000"), Decimal("5000"), "SAR"),
        seating_capacity=SeatingCapacityConstraint.at_least(2),
        colors_any_of=("Beige", "Grey"),
        styles_all_of=("Modern", "Minimalist"),
    ),
]


def _where(statement: Select[Any]) -> str:
    """The WHERE clause alone: what decides eligibility, without presentation."""
    sql = " ".join(_sql(statement).split())
    return sql[sql.index("WHERE ") :].split(" ORDER BY ")[0].split(" LIMIT ")[0]


@pytest.mark.parametrize("request_", REQUESTS, ids=lambda r: str(r.applied_filters()))
async def test_the_eligible_pool_uses_identical_predicates_to_search(
    request_: ProductSearchRequest,
) -> None:
    """One definition of eligibility, or the ranked set is not the searchable set."""
    full, ids = RecordingSession(), RecordingSession()
    await _repository(full).search(request_, CONTEXT, limit=10)
    await _repository(ids).search_eligible_ids(request_, CONTEXT)

    assert _where(full.statements[0]) == _where(ids.statements[0])


async def test_the_eligible_pool_carries_no_limit() -> None:
    """A presentation limit must not be able to shrink the ranking pool."""
    session = RecordingSession()
    await _repository(session).search_eligible_ids(_request(), CONTEXT)

    assert " LIMIT " not in _sql(session.statements[0])


async def test_the_eligible_pool_selects_only_ids() -> None:
    session = RecordingSession()
    await _repository(session).search_eligible_ids(_request(), CONTEXT)

    sql = " ".join(_sql(session.statements[0]).split())
    assert sql.startswith("SELECT core_product.id FROM core_product")


async def test_the_eligible_pool_cannot_be_asked_for_another_store() -> None:
    session = RecordingSession()
    await _repository(session).search_eligible_ids(_request(), CONTEXT)

    sql = _sql(session.statements[0])
    assert f"core_product.store_id = {STORE_ID}" in sql
    assert str(OTHER_STORE_ID) not in sql
    assert "limit" not in inspect.signature(ProductRepository.search_eligible_ids).parameters


async def test_visual_matches_with_nothing_to_look_up_issue_no_query() -> None:
    session = RecordingSession()
    assert await _repository(session).ids_for_visual_matches([], [""], CONTEXT) == ({}, {})
    assert session.statements == []
