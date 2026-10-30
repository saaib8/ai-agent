"""What M12A built, and the authority line every M12 contract answers to.

A room's design reasons about rooms and never about identity: Nora's
per-piece decisions cannot name a product, cannot see a store, and cannot
reach the state they are reasoning from. These guards are the same recursive walk the
customer-agent contracts get, applied to the design boundary — one authority
policy, not a second weaker one for the new agent.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from app.schemas.design import (
    InteriorDesignRequest,
    InteriorDesignResult,
    PieceDesign,
)
from app.schemas.geometry import RoomGeometry
from pydantic import BaseModel

APP = Path(__file__).parents[2] / "app"

MODEL_FACING_M12 = (
    InteriorDesignRequest,
    InteriorDesignResult,
    PieceDesign,  # Nora's per-piece decisions replaced the specialist's anchors
    RoomGeometry,
)

BUILT_IN_12A = {
    "app/schemas/geometry.py": "class RoomGeometry",
    "app/schemas/design.py": "class PieceDesign",
    "app/services/catalog_capability.py": "class CatalogCapabilityService",
}

NOT_YET_REACHABLE = (
    "REJECT_PRODUCT",
    "INTENTIONALLY_UNFILLED",
    "FocusedBundleItem",
)
"""What M12E-4C deliberately did not build.

E4C completed iterative refinement - replacing, re-costing, removing a role -
all without a specialist call. What is left is composition: changing what the
room is *for*, which needs the design agent to revise a plan it can currently
only create. That is E4D.

`REJECT_PRODUCT` stays out for a concrete reason rather than for scope. "Remove
this and leave the gap" needs a durable `INTENTIONALLY_UNFILLED` state; without
one, the next optimisation would quietly refill the role, or the room would
report it as a catalog gap. `FocusedBundleItem` stays out because nothing
tracks a focused card."""


def _reachable_names(model: type[BaseModel], seen: set[type] | None = None) -> list[str]:
    seen = seen if seen is not None else set()
    if model in seen:
        return []
    seen.add(model)
    names: list[str] = []
    for name, field in model.model_fields.items():
        names.append(name)
        for arg in (field.annotation, *getattr(field.annotation, "__args__", ())):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                names.extend(_reachable_names(arg, seen))
    return names


# ── what M12A built ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(("path", "symbol"), BUILT_IN_12A.items())
def test_this_phase_built_what_it_said(path: str, symbol: str) -> None:
    assert symbol in (APP.parent / path).read_text()


@pytest.mark.parametrize("symbol", NOT_YET_REACHABLE)
def test_bundle_refinement_does_not_exist_yet(symbol: str) -> None:
    for module in APP.rglob("*.py"):
        assert symbol not in module.read_text(), f"{module.name} defines {symbol}"


# ── the authority line ──────────────────────────────────────────────────────


@pytest.mark.parametrize("model", MODEL_FACING_M12, ids=lambda m: m.__name__)
@pytest.mark.parametrize(
    "forbidden",
    [
        "product_id",
        "store_id",
        "retailer",
        "tenant",
        "bundle_revision",
        """M12E-4D narrowed this from "revision".

        What it guards is the bundle's execution counter, which a model that
        could see it could start reasoning about. `InteriorDesignRequest.
        revision` is the *design* plan being revised and carries no counter at
        all - its contents are checked by name and by type in
        `test_the_revision_request_reaches_no_identity`.
        """,
        "schema_version",
        "price_amount",
        "product_url",
        "image_url",
        "name_english",
        "pinecone",
        "sku",
    ],
)
def test_no_m12_contract_reaches_an_identity_or_a_price(
    model: type[BaseModel], forbidden: str
) -> None:
    for name in _reachable_names(model):
        assert forbidden not in name, f"{model.__name__}.{name}"


@pytest.mark.parametrize("model", MODEL_FACING_M12, ids=lambda m: m.__name__)
@pytest.mark.parametrize(
    "forbidden",
    ["AgentStateV1", "RetailerContext", "ProductCandidate", "ProductRow", "CustomerTurnResult"],
)
def test_no_m12_contract_reaches_an_application_type(
    model: type[BaseModel], forbidden: str
) -> None:
    definitions = set(model.model_json_schema().get("$defs", {}))

    assert forbidden not in definitions


def test_the_design_request_cannot_carry_retailer_scope() -> None:
    """Scope stays with application code and is passed to services, never to a
    model (CLAUDE.md 8, 20.2)."""
    assert "context" not in InteriorDesignRequest.model_fields
    assert "store_id" not in InteriorDesignRequest.model_fields


def test_each_measurement_type_fixes_its_own_authority() -> None:
    """A `Literal`, so a measurement cannot be built with the wrong
    provenance — the tag is not a field anyone remembers to set."""
    from app.schemas.geometry import RoomMeasurement

    # The anchor and guidance measurements went with the design specialist.
    for model in (RoomMeasurement,):
        annotation = str(model.model_fields["authority"].annotation)
        assert "Literal" in annotation, model.__name__




# ── the deterministic services stay deterministic ───────────────────────────


@pytest.mark.parametrize(
    "module", ["services/room_design.py", "services/catalog_capability.py"]
)
def test_the_m12a_services_consult_no_model(module: str) -> None:
    source = (APP / module).read_text()
    tree = ast.parse(source)
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }

    assert not any("llm" in name or "prompt" in name for name in imported)
    for forbidden in ("StructuredLLMClient", "parse(", "instructions"):
        assert forbidden not in source, forbidden


def test_the_design_foundations_hold_no_room_composition_table() -> None:
    """Which categories a room calls for is M12B's reasoning, not a mapping.

    Scoped to the design modules. Elsewhere a subcategory name is ordinary
    domain policy - M8's relaxation allowlist names `sofa` because that is the
    product type whose width was validated - and flagging those would be
    flagging the system working correctly.
    """
    from app.taxonomy.registry import load_taxonomy

    taxonomy = load_taxonomy()
    subcategories = {
        subcategory
        for category in taxonomy.categories
        for subcategory in taxonomy.subcategories(category)
        if subcategory != category
    }

    for module in (
        APP / "services/catalog_capability.py",
        APP / "schemas/design.py",
        APP / "schemas/geometry.py",
    ):
        named = sorted(subcategories & _data_literals(module))
        assert named == [], f"{module.name} names {named}"


def _data_literals(module: Path) -> set[str]:
    """String constants the module uses as data, excluding its prose.

    Docstrings explain *why* a rule exists and legitimately name product types
    - "a room being five metres long does not establish that a sofa fits" is
    the reasoning behind refusing that comparison. A mapping is what would be
    a composition table.
    """
    tree = ast.parse(module.read_text())
    docstrings = {
        ast.get_docstring(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    }


def test_an_unconfigured_deployment_still_starts() -> None:
    from tests.conftest import build_settings

    settings = build_settings()

    assert settings.agents.supervisor.model is None


def test_redacted_settings_expose_status_not_identifiers() -> None:
    import pydantic

    from tests.conftest import build_settings

    # The design specialist is merged into Nora: its settings are gone, and an
    # environment still naming them is refused rather than silently ignored.
    with pytest.raises(pydantic.ValidationError):
        build_settings(interior_design={"model": "some-design-model"})
    redacted = build_settings(agents={"nora": {"model": "some-design-model"}}).redacted()
    assert not any("interior_design" in key for key in redacted)
    assert "some-design-model" not in str(redacted.values())
    for key, value in redacted.items():
        if key.endswith("_configured"):
            assert isinstance(value, bool), key


# ── M12C: the bridge to product discovery ───────────────────────────────────

BUILT_IN_12C = {
    "app/schemas/design_discovery.py": "class DesignDiscoveryResult",
    "app/services/design_discovery.py": "class DesignDiscoveryService",
    "app/schemas/resolution.py": "class CandidatePoolResult",
    "app/services/search_pipeline.py": "async def execute_candidate_pool",
}


@pytest.mark.parametrize(("path", "symbol"), BUILT_IN_12C.items())
def test_m12c_built_what_it_said(path: str, symbol: str) -> None:
    assert symbol in (APP.parent / path).read_text()


BUILT_IN_12D = {
    "app/schemas/bundle.py": "class RoomBundle",
    "app/services/bundle_optimizer.py": "class BundleOptimizer",
}


@pytest.mark.parametrize(("path", "symbol"), BUILT_IN_12D.items())
def test_m12d_built_what_it_said(path: str, symbol: str) -> None:
    assert symbol in (APP.parent / path).read_text()


def test_the_optimiser_makes_no_spatial_claim() -> None:
    """M12A establishes only that an object taller than the ceiling does not go
    in. Nothing licenses a claim that a room holds what was selected."""
    source = (APP / "services/bundle_optimizer.py").read_text()

    for forbidden in ("assess_fit", "FitVerdict", "RoomGeometry", "RoomMeasurement"):
        assert forbidden not in source, forbidden


def test_design_contracts_never_import_the_application_bridge() -> None:
    """One-way dependency: the bridge knows about design, never the reverse.

    `app/schemas/design.py` is what a model is shown. If it could reach the
    discovery contracts, a verified product would be one field away from the
    specialist's request.
    """
    tree = ast.parse((APP / "schemas/design.py").read_text())

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert "design_discovery" not in module
            assert "resolution" not in module


@pytest.mark.parametrize("model", MODEL_FACING_M12, ids=lambda m: m.__name__)
def test_no_model_facing_contract_reaches_a_candidate_pool(
    model: type[BaseModel],
) -> None:
    """Candidates are for deterministic services. Nothing hands them back to
    the specialist that asked for the room (CLAUDE.md 17.2)."""
    definitions = set(model.model_json_schema().get("$defs", {}))

    for forbidden in ("CandidatePoolResult", "RankedProductCandidate", "ProductCandidate"):
        assert forbidden not in definitions


def test_the_bridge_holds_no_room_composition_knowledge() -> None:
    """Which product types a room calls for is the specialist's reasoning. A
    bridge that knew would be a second, silent designer."""
    source = (APP / "services/design_discovery.py").read_text()
    tree = ast.parse(source)

    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    } - {
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    for category in ("seating", "tables", "lighting", "sofa", "rug", "bed"):
        assert not any(category in literal for literal in literals), category
