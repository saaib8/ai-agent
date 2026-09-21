"""Hydration by id, configuration, and the boundary after M11B-3C.

The configuration question this phase had to answer: `presentation_limit` has
no approved value, so the service must not invent one — and must still start
for every deployment that is not using the search pipeline yet.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any, cast, get_args, get_origin
from uuid import uuid4

import pytest
from app.core.config import CustomerAgentSettings, Settings
from app.core.exceptions import ConfigurationError
from app.repositories.products import ProductRepository
from app.schemas.agent_decision import CustomerAgentDecision, CustomerStateProposal
from app.schemas.agent_turn import CustomerResponse, DecisionInput
from app.schemas.agent_view import AgentStateView
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions, RawDimensions
from app.schemas.grounding import GroundedProduct
from app.schemas.product import CommerceClassification, ProductRow
from app.schemas.refinement import SearchRefinementDelta
from app.schemas.resolution import ProductSearchExecutionResult
from app.schemas.retailer import RetailerContext
from app.schemas.semantic import SemanticRankedCandidate, SemanticRankingResult
from app.services.hydration import ProductHydrationService
from pydantic import BaseModel, ValidationError

from tests.conftest import build_settings

APP = Path(__file__).parents[2] / "app"
CONTEXT = RetailerContext(store_id=50)
OTHER = RetailerContext(store_id=60)


def _row(product_id: int, *, store_id: int = 50) -> ProductRow:
    return ProductRow(
        id=product_id,
        uuid=uuid4(),
        store_id=store_id,
        name_english=f"Sofa {product_id}",
        name_arabic="كنبة",
        price_amount=Decimal("1000"),
        price_unit="SAR",
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        visual_category="3-seater-sofa",
        commerce=CommerceClassification(category="seating", subcategory="sofa"),
        dimensions=RawDimensions(unit="cm"),
        main_color="Beige",
        styles=("Modern",),
        is_active=True,
    )


class FakeRepository:
    def __init__(self, rows: list[ProductRow]) -> None:
        self.rows = rows
        self.calls: list[tuple[list[int], RetailerContext]] = []

    async def get_by_ids(
        self, product_ids: Any, context: RetailerContext
    ) -> list[ProductRow]:
        self.calls.append((list(product_ids), context))
        wanted = set(product_ids)
        return [
            r for r in self.rows if r.id in wanted and r.store_id == context.store_id
        ]


def _service(rows: list[ProductRow]) -> tuple[ProductHydrationService, FakeRepository]:
    repository = FakeRepository(rows)
    return ProductHydrationService(cast(ProductRepository, repository)), repository


# ── hydrate_ids ═════════════════════════════════════════════════════════════


async def test_requested_order_is_preserved() -> None:
    service, _ = _service([_row(1), _row(2), _row(3)])

    hydrated = await service.hydrate_ids([3, 1, 2], CONTEXT)

    assert [p.product_id for p in hydrated] == [3, 1, 2]


async def test_a_missing_row_is_dropped_never_substituted() -> None:
    service, _ = _service([_row(1), _row(3)])

    hydrated = await service.hydrate_ids([1, 2, 3], CONTEXT)

    assert [p.product_id for p in hydrated] == [1, 3]


async def test_another_retailers_row_is_simply_absent() -> None:
    service, _ = _service([_row(1), _row(2, store_id=60)])

    hydrated = await service.hydrate_ids([1, 2], CONTEXT)

    assert [p.product_id for p in hydrated] == [1]


async def test_every_read_carries_the_request_scope() -> None:
    service, repository = _service([_row(1)])

    await service.hydrate_ids([1], CONTEXT)

    assert repository.calls[0][1] is CONTEXT


async def test_an_empty_request_reads_nothing() -> None:
    service, repository = _service([_row(1)])

    assert await service.hydrate_ids([], CONTEXT) == ()
    assert repository.calls == []


async def test_facts_come_from_the_catalog_row() -> None:
    service, _ = _service([_row(7)])

    hydrated = await service.hydrate_ids([7], CONTEXT)

    assert hydrated[0].name_english == "Sofa 7"
    assert hydrated[0].main_color == "Beige"


# ── the older API still behaves ═════════════════════════════════════════════


async def test_hydrate_still_accepts_a_ranking_and_delegates() -> None:
    """The M9B-2 contract is preserved, with one drop-stale implementation."""
    service, _ = _service([_row(1), _row(3)])
    ranking = SemanticRankingResult(
        candidates=tuple(
            SemanticRankedCandidate(product_id=i, relaxation_depth=0, semantic_rank=n)
            for n, i in enumerate([3, 2, 1])
        ),
        semantic_used=True,
    )

    hydrated = await service.hydrate(ranking, CONTEXT)

    assert [p.product_id for p in hydrated] == [3, 1]


def test_hydrate_delegates_rather_than_duplicating() -> None:
    body = ast.parse((APP / "services/hydration.py").read_text())
    hydrate = next(
        node
        for node in ast.walk(body)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "hydrate"
    )
    statements = [n for n in hydrate.body if not isinstance(n, ast.Expr)]

    assert len(statements) == 1
    assert "hydrate_ids" in ast.unparse(statements[0])


# ── the execution result ════════════════════════════════════════════════════


def _grounding(count: int) -> Any:
    from app.schemas.grounding import SearchExecutionGrounding, SearchOutcome
    from app.schemas.relaxation import StopReason

    products = tuple(
        GroundedProduct(
            grounding_ref=i,
            presented_ordinal=i,
            name_english=f"Sofa {i}",
            price_amount=Decimal("1000"),
            price_unit="SAR",
            image_url="https://example.test/x.jpg",
            product_url="https://example.test/x",
            commerce=CommerceClassification(category="seating"),
            dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
            relaxation_depth=0,
        )
        for i in range(1, count + 1)
    )
    return SearchExecutionGrounding(
        outcome=SearchOutcome.RESULTS if count else SearchOutcome.ZERO_RESULTS,
        products=products,
        eligible_count=max(count, 1),
        ranked_count=max(count, 1),
        selected_count=count,
        presented_count=count,
        exact_candidate_count=count,
        stop_reason=StopReason.EXACT_SUFFICIENT,
    )


def test_the_result_pairs_ids_with_grounded_products() -> None:
    result = ProductSearchExecutionResult(
        presented_product_ids=(11, 22), grounding=_grounding(2)
    )

    assert len(result.presented_product_ids) == len(result.grounding.products)


def test_a_count_mismatch_is_refused() -> None:
    with pytest.raises(ValidationError, match="every presented product"):
        ProductSearchExecutionResult(
            presented_product_ids=(11,), grounding=_grounding(2)
        )


def test_a_repeated_id_is_refused() -> None:
    with pytest.raises(ValidationError, match="at most once"):
        ProductSearchExecutionResult(
            presented_product_ids=(11, 11), grounding=_grounding(2)
        )


def test_the_grounding_still_carries_no_product_id() -> None:
    result = ProductSearchExecutionResult(
        presented_product_ids=(11, 22), grounding=_grounding(2)
    )

    assert "product_id" not in GroundedProduct.model_fields
    assert "11" not in result.grounding.model_dump_json()


# ── model authority ═════════════════════════════════════════════════════════

MODEL_FACING = (
    DecisionInput,
    AgentStateView,
    CustomerAgentDecision,
    CustomerStateProposal,
    SearchRefinementDelta,
    CustomerResponse,
)


def _walk(model: type[BaseModel], seen: set[type] | None = None) -> list[Any]:
    seen = seen if seen is not None else set()
    if model in seen:
        return []
    seen.add(model)
    found: list[Any] = []
    for name, field in model.model_fields.items():
        found.append((name, field.annotation))
        for nested in _nested(field.annotation):
            found.extend(_walk(nested, seen))
    return found


def _nested(annotation: Any) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    models: list[type[BaseModel]] = []
    for argument in get_args(annotation):
        models.extend(_nested(argument))
    if get_origin(annotation) is not None and not get_args(annotation):
        return models
    return models


def test_the_execution_result_does_carry_ids() -> None:
    """The guard below means nothing unless there is something to contain."""
    assert "presented_product_ids" in ProductSearchExecutionResult.model_fields


@pytest.mark.parametrize("model", MODEL_FACING, ids=lambda m: m.__name__)
def test_the_execution_result_is_unreachable_from_model_contracts(
    model: type[BaseModel],
) -> None:
    rendered = {str(annotation) for _, annotation in _walk(model)}

    assert not any("ProductSearchExecutionResult" in text for text in rendered)
    for name, _ in _walk(model):
        assert "product_id" not in name, f"{model.__name__}.{name}"


# ── configuration ═══════════════════════════════════════════════════════════


def test_the_presentation_limit_has_no_default() -> None:
    """No number is approved, and one invented here decides what a customer sees."""
    assert CustomerAgentSettings().presentation_limit is None


def test_a_configured_limit_must_show_at_least_one_product() -> None:
    with pytest.raises(ValidationError):
        CustomerAgentSettings(presentation_limit=0)


def test_a_configured_limit_is_accepted() -> None:
    assert CustomerAgentSettings(presentation_limit=3).presentation_limit == 3


def test_the_limit_is_not_bounded_by_the_discovery_ceiling() -> None:
    """Unrelated concepts: the pool this selects from is unbounded."""
    settings = build_settings(
        customer_agent={"presentation_limit": 500},
        discovery={"default_candidate_limit": 50, "max_candidate_limit": 200},
    )

    assert settings.customer_agent.presentation_limit == 500


def test_startup_settings_build_without_a_presentation_limit() -> None:
    """A deployment not using the pipeline must still start."""
    settings = build_settings()

    assert settings.customer_agent.presentation_limit is None
    assert settings.redacted()["presentation_configured"] is False


def test_the_redacted_summary_reports_only_whether_it_is_configured() -> None:
    settings = build_settings(customer_agent={"presentation_limit": 3})

    assert settings.redacted()["presentation_configured"] is True


def test_the_dependency_refuses_to_build_an_unconfigured_pipeline() -> None:
    from app.api.dependencies import product_search_pipeline

    resources = type("R", (), {"settings": build_settings()})()

    with pytest.raises(ConfigurationError, match="presentation_limit"):
        product_search_pipeline(cast(Any, None), cast(Any, resources))


def test_nothing_builds_the_pipeline_at_startup() -> None:
    """Lifespan must not require configuration for a capability nobody asked for."""
    lifespan = (APP / "core/lifespan.py").read_text()

    assert "ProductSearchPipeline" not in lifespan
    assert "presentation_limit" not in lifespan


def test_settings_is_declared_the_way_the_project_declares_optional_values() -> None:
    annotation = str(
        CustomerAgentSettings.model_fields["presentation_limit"].annotation
    )

    assert annotation == "int | None"
    assert Settings.model_fields["customer_agent"].annotation is CustomerAgentSettings


# ── phase boundary ══════════════════════════════════════════════════════════


def test_the_phases_capabilities_exist() -> None:
    assert (APP / "services/search_pipeline.py").exists()
    assert "hydrate_ids" in (APP / "services/hydration.py").read_text()
    assert "presentation_limit" in (APP / "core/config.py").read_text()


@pytest.mark.parametrize(
    "symbol",
    [
                    "LangGraph",
    ],
)
def test_no_later_phase_capability_exists(symbol: str) -> None:
    for module in APP.rglob("*.py"):
        assert symbol not in module.read_text(), f"{module.name} defines {symbol}"


def test_no_agent_module_or_route_exists() -> None:
    """The customer decision prompt is M11B-4's and now exists; these do not."""
    assert not (APP / "agents").exists()
    assert "chat.py" not in {p.name for p in (APP / "api/routes").glob("*.py")}


def test_the_pipeline_owns_no_policy() -> None:
    source = (APP / "services/search_pipeline.py").read_text()

    for forbidden in (
        "QueryUnderstandingService",
        "SearchRefinementComposer",
        "ProductReferenceResolver",
        "RelativePriceResolver",
        "apply_update",
        "commit_search_results",
        "revision",
    ):
        assert forbidden not in source, forbidden


def test_the_pipeline_reads_no_global_settings() -> None:
    """The limit arrives resolved, so `execute` cannot consult configuration."""
    source = (APP / "services/search_pipeline.py").read_text()

    assert "get_settings" not in source
    assert "Settings" not in source.replace("PineconeSettings", "")
