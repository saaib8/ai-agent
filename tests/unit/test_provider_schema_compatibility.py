"""The decision contract must survive the provider's strict-schema conversion.

`Field(discriminator="kind")` renders as JSON Schema `oneOf`, and the OpenAI
Responses strict format rejects that outright:

    400 invalid_json_schema
    "In context=('properties', 'reference'), 'oneOf' is not permitted."

So `ProductReferenceSelector` is a plain union. These tests hold both halves of
that trade in place: the emitted schema stays provider-compatible, and the
accepted selector language does not change, because each member carries a
distinct `kind` Literal and that is what actually selects the branch.

The schema is generated through the installed SDK's own conversion path rather
than a local imitation of it. Nothing here rewrites emitted JSON: the models
must generate the accepted shape themselves.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from app.schemas.agent_decision import (
    AgentAction,
    CustomerAgentDecision,
    NewSearchProposal,
    ProductInteractionIntent,
    ProductInteractionOp,
)
from app.schemas.product_reference import (
    ExtremumDirection,
    FocusedProduct,
    PresentedAttributeMatch,
    PresentedExtremum,
    PresentedOrdinal,
    ProductReferenceSelector,
    SoleSelectedProduct,
)
from app.schemas.refinement import (
    PriceRefinement,
    PriceRefinementOp,
    PriceRelation,
    RelativePriceRefinement,
    SearchRefinementDelta,
    SemanticIntentOp,
    SemanticIntentRefinement,
)
from app.taxonomy.attributes import AttributeFamily
from openai.lib._pydantic import to_strict_json_schema
from pydantic import TypeAdapter, ValidationError

SELECTOR_MEMBERS = (
    "PresentedOrdinal",
    "FocusedProduct",
    "SoleSelectedProduct",
    "PresentedAttributeMatch",
    "PresentedExtremum",
)

# Where a selector appears in the decision contract. Every one of these is a
# place the provider would reject a `oneOf`.
SELECTOR_SITES = (
    "#/properties/reference",
    "#/properties/comparison_references/items",
    "#/$defs/ProductInteractionIntent/properties/reference",
    "#/$defs/RelativePriceRefinement/properties/reference",
)

_selector: TypeAdapter[ProductReferenceSelector] = TypeAdapter(ProductReferenceSelector)


def _plain() -> dict[str, Any]:
    return CustomerAgentDecision.model_json_schema()


def _strict() -> dict[str, Any]:
    """Exactly what `responses.parse(text_format=...)` sends."""
    schema: dict[str, Any] = dict(to_strict_json_schema(CustomerAgentDecision))
    return schema


def _count(schema: dict[str, Any], keyword: str) -> int:
    return json.dumps(schema).count(f'"{keyword}"')


def _walk(node: Any, path: str = "#") -> Any:
    """Every (path, node) pair in a schema, so a site can be located."""
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}/{index}")


def _resolve(schema: dict[str, Any], path: str) -> dict[str, Any]:
    node: Any = schema
    for part in path.removeprefix("#/").split("/"):
        node = node[int(part)] if part.isdigit() else node[part]
    assert isinstance(node, dict), path
    return node


def _member_names(node: dict[str, Any]) -> set[str]:
    """The selector members a union node references, by `$defs` name."""
    branches = node.get("anyOf", node.get("oneOf", []))
    return {
        ref.rsplit("/", 1)[-1]
        for branch in branches
        if isinstance(branch, dict) and (ref := branch.get("$ref"))
    }


# ── the emitted schema ──────────────────────────────────────────────────────


def test_the_selector_alias_carries_no_discriminator_metadata() -> None:
    schema = _selector.json_schema()

    assert "discriminator" not in json.dumps(schema)
    assert "oneOf" not in json.dumps(schema)
    assert _member_names(schema) == set(SELECTOR_MEMBERS)


def test_the_plain_decision_schema_has_no_oneof_and_no_discriminator() -> None:
    schema = _plain()

    assert _count(schema, "oneOf") == 0
    assert _count(schema, "discriminator") == 0


def test_the_strict_provider_schema_generates_successfully() -> None:
    """The conversion the SDK performs must not raise, and must stay an object."""
    schema = _strict()

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert len(schema["$defs"]) >= len(SELECTOR_MEMBERS)


def test_the_strict_provider_schema_has_no_oneof_and_no_discriminator() -> None:
    """The exact assertion the live 400 was about."""
    schema = _strict()

    assert _count(schema, "oneOf") == 0
    assert _count(schema, "discriminator") == 0


@pytest.mark.parametrize("path", SELECTOR_SITES)
def test_every_selector_site_is_a_provider_compatible_union(path: str) -> None:
    """All four locations, in both the plain and the strict schema."""
    for schema in (_plain(), _strict()):
        node = _resolve(schema, path)
        # An optional selector is `anyOf[union, null]`; unwrap to the union.
        if SELECTOR_MEMBERS[0] not in json.dumps(node.get("anyOf", node)):
            pytest.fail(f"{path} does not reference the selector")
        union = node
        if not _member_names(union):
            union = next(
                branch
                for branch in node["anyOf"]
                if isinstance(branch, dict) and _member_names(branch)
            )
        assert "oneOf" not in union
        assert "discriminator" not in union
        assert _member_names(union) == set(SELECTOR_MEMBERS)


def test_no_selector_site_was_missed() -> None:
    """The site list is derived, not trusted: any union of the five members
    anywhere in the schema must be one of the four known locations."""
    schema = _plain()
    found = {
        path
        for path, node in _walk(schema)
        if isinstance(node, dict) and _member_names(node) == set(SELECTOR_MEMBERS)
    }

    assert found == {
        "#/properties/reference",
        "#/properties/comparison_references/items",
        "#/$defs/ProductInteractionIntent/properties/reference",
        "#/$defs/RelativePriceRefinement/properties/reference",
    }


def test_the_strict_schema_exposes_no_product_or_retailer_identity() -> None:
    """The authority boundary, asserted on what the provider actually sees."""
    emitted = json.dumps(_strict())

    for forbidden in ("product_id", "store_id", "retailer", "pinecone_id", "salla"):
        assert forbidden not in emitted, forbidden


# ── the accepted language is unchanged ──────────────────────────────────────

VALID_SELECTORS = (
    PresentedOrdinal(position=1),
    FocusedProduct(),
    SoleSelectedProduct(),
    PresentedAttributeMatch(family=AttributeFamily.COLOR, value="Beige"),
    PresentedExtremum(direction=ExtremumDirection.LOWEST),
)


@pytest.mark.parametrize("selector", VALID_SELECTORS, ids=lambda s: s.kind)
def test_every_variant_still_validates_from_its_payload(
    selector: ProductReferenceSelector,
) -> None:
    """Round-tripped as the provider would send it: JSON, no Python types."""
    restored = _selector.validate_python(selector.model_dump(mode="json"))

    assert restored == selector
    assert type(restored) is type(selector)


@pytest.mark.parametrize("selector", VALID_SELECTORS, ids=lambda s: s.kind)
def test_the_kind_literal_still_selects_the_intended_member(
    selector: ProductReferenceSelector,
) -> None:
    """Without discriminator metadata the union resolves by Literal instead.

    The payload is padded with every other member's `kind`-free fields, so a
    union that matched on shape rather than tag would pick the wrong branch.
    """
    payload: dict[str, Any] = {
        "position": 3,
        "family": AttributeFamily.STYLE.value,
        "value": "Modern",
        "field": "price",
        "direction": ExtremumDirection.HIGHEST.value,
        **selector.model_dump(mode="json"),
    }
    with pytest.raises(ValidationError):
        # extra="forbid" means the padded payload is rejected outright, which
        # is itself the guarantee: no member silently absorbs another's fields.
        _selector.validate_python(payload)

    assert type(_selector.validate_python(selector.model_dump(mode="json"))) is type(
        selector
    )


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"kind": "by_product_id", "product_id": 165645}, "a product-id selector"),
        ({"kind": "product", "id": 165645}, "a product id under another name"),
        ({"kind": "presented_ordinal", "position": 0}, "a zeroth position"),
        ({"kind": "presented_ordinal", "position": -1}, "a negative position"),
        ({"kind": "presented_ordinal"}, "a missing required field"),
        ({"kind": "presented_attribute_match", "family": "color"}, "no value"),
        ({"kind": "presented_attribute_match", "family": "color", "value": ""}, "empty"),
        ({"kind": "presented_extremum"}, "no direction"),
        ({"kind": "presented_extremum", "direction": "cheapest"}, "an unknown direction"),
        (
            {"kind": "presented_extremum", "direction": "lowest", "field": "style"},
            "a non-price extremum",
        ),
        ({"kind": "focused_product", "product_id": 1}, "an extra field"),
        ({"kind": "sole_selected_product", "position": 1}, "another member's field"),
        ({"kind": "unknown_selector"}, "an unknown kind"),
    ],
)
def test_invalid_selectors_are_still_rejected(payload: dict[str, Any], why: str) -> None:
    with pytest.raises(ValidationError):
        _selector.validate_python(payload)


# ── a raw selector must carry its own tag ───────────────────────────────────
#
# Dropping the discriminator removed more than `oneOf`: a discriminated union
# also *requires* the discriminator key, and a plain union does not. Since each
# member defaults its own `kind`, `{}` briefly read as `FocusedProduct` - an
# empty object silently becoming whatever product the customer is looking at.
#
# A `BeforeValidator` on the alias restores that requirement without restoring
# `oneOf`. The union is tagged because the contract says so, not because the
# provider's strict transformer happens to mark `kind` required.


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({}, "nothing at all"),
        ({"position": 1}, "an ordinal with no tag"),
        ({"direction": "lowest"}, "an extremum with no tag"),
        ({"field": "price", "direction": "lowest"}, "a fuller extremum with no tag"),
        ({"family": "color", "value": "Beige"}, "an attribute match with no tag"),
    ],
)
def test_a_raw_selector_without_a_kind_is_rejected(
    payload: dict[str, Any], why: str
) -> None:
    """No payload may acquire a selector identity from a field default."""
    with pytest.raises(ValidationError):
        _selector.validate_python(payload)


@pytest.mark.parametrize(
    "raw",
    ['{}', '{"position": 1}', '{"direction": "lowest"}'],
)
def test_the_tag_requirement_holds_on_json_input_too(raw: str) -> None:
    """The path that actually matters: the SDK validates from JSON, not from a
    Python dict, so a check that only ran on `validate_python` would miss the
    one caller it exists for."""
    with pytest.raises(ValidationError):
        _selector.validate_json(raw)


@pytest.mark.parametrize("selector", VALID_SELECTORS, ids=lambda s: s.kind)
def test_a_typed_instance_still_passes_through_untouched(
    selector: ProductReferenceSelector,
) -> None:
    """Raw input and constructed objects are different cases, deliberately.

    An instance reached here through the type system and carries its tag by
    construction, so the check does not apply to it and application code keeps
    writing `FocusedProduct()`.
    """
    assert _selector.validate_python(selector) == selector
    assert _selector.validate_python(selector.model_dump(mode="json")) == selector


def test_the_constructors_still_supply_their_own_kind() -> None:
    """The defaults are unchanged; only untrusted mappings must be explicit."""
    assert FocusedProduct().kind == "focused_product"
    assert SoleSelectedProduct().kind == "sole_selected_product"
    assert PresentedOrdinal(position=1).kind == "presented_ordinal"
    assert PresentedExtremum(direction=ExtremumDirection.LOWEST).kind == (
        "presented_extremum"
    )
    assert PresentedAttributeMatch(
        family=AttributeFamily.COLOR, value="Beige"
    ).kind == "presented_attribute_match"


@pytest.mark.parametrize("member", SELECTOR_MEMBERS)
def test_the_strict_schema_also_requires_kind_on_every_branch(member: str) -> None:
    """Defence in depth, not the enforcement itself.

    Strict conversion adds every property to `required`. That is a second
    reason the provider cannot omit the tag - the contract's own check is the
    first, and is what holds for any other caller.
    """
    definition = _strict()["$defs"][member]

    assert "kind" in definition["required"]
    assert definition["additionalProperties"] is False


def test_the_tag_check_does_not_leak_into_the_emitted_schema() -> None:
    """A validator is validation. It must not add keywords the provider would
    reject, and must not change the union's shape."""
    for schema in (_plain(), _strict()):
        assert _count(schema, "oneOf") == 0
        assert _count(schema, "discriminator") == 0
    assert _member_names(_selector.json_schema()) == set(SELECTOR_MEMBERS)


# ── the four sites, exercised through the real contract ─────────────────────


def test_the_decision_reference_path_accepts_a_selector() -> None:
    decision = CustomerAgentDecision.model_validate(
        {"action": "product_detail", "reference": {"kind": "focused_product"}}
    )

    assert isinstance(decision.reference, FocusedProduct)


def test_the_comparison_references_path_accepts_selectors() -> None:
    decision = CustomerAgentDecision.model_validate(
        {
            "action": "compare",
            "comparison_references": [
                {"kind": "presented_ordinal", "position": 1},
                {"kind": "presented_extremum", "direction": "lowest"},
            ],
        }
    )

    assert [type(r) for r in decision.comparison_references] == [
        PresentedOrdinal,
        PresentedExtremum,
    ]


def test_the_interaction_reference_path_accepts_a_selector() -> None:
    decision = CustomerAgentDecision.model_validate(
        {
            "action": "answer",
            "interaction": {
                "op": "select",
                "reference": {"kind": "presented_ordinal", "position": 2},
            },
        }
    )

    assert decision.interaction is not None
    assert decision.interaction.reference == PresentedOrdinal(position=2)


def test_the_relative_price_reference_path_accepts_a_selector() -> None:
    """The deepest nesting in the contract, validated from raw JSON."""
    decision = CustomerAgentDecision.model_validate(
        {
            "action": "refine_search",
            "refinement": {
                "price": {
                    "op": "set_relative",
                    "relative": {
                        "relation": "percent_cheaper",
                        "reference": {"kind": "presented_ordinal", "position": 2},
                        "percent": "20",
                    },
                }
            },
        }
    )

    price = decision.refinement.price if decision.refinement else None
    assert price is not None and price.relative is not None
    assert price.relative.reference == PresentedOrdinal(position=2)


def test_the_deepest_path_survives_construction_too() -> None:
    """The same path built from typed objects rather than parsed JSON."""
    decision = CustomerAgentDecision(
        action=AgentAction.REFINE_SEARCH,
        refinement=SearchRefinementDelta(
            price=PriceRefinement(
                op=PriceRefinementOp.SET_RELATIVE,
                relative=RelativePriceRefinement(
                    relation=PriceRelation.PERCENT_CHEAPER,
                    reference=PresentedOrdinal(position=2),
                    percent="20",
                ),
            )
        ),
    )

    assert decision.refinement is not None
    assert decision.refinement.price is not None
    relative = decision.refinement.price.relative
    assert relative is not None
    assert isinstance(relative.reference, PresentedOrdinal)


def test_the_deepest_path_requires_the_tag_too() -> None:
    """The check travels with the alias, so it reaches every nesting level."""
    payload = {
        "action": "refine_search",
        "refinement": {
            "price": {
                "op": "set_relative",
                "relative": {
                    "relation": "percent_cheaper",
                    "reference": {"position": 2},
                    "percent": "20",
                },
            }
        },
    }
    with pytest.raises(ValidationError):
        CustomerAgentDecision.model_validate(payload)

    tagged = {"kind": "presented_ordinal", "position": 2}
    payload["refinement"]["price"]["relative"]["reference"] = tagged  # type: ignore[index]
    decision = CustomerAgentDecision.model_validate(payload)

    assert decision.refinement is not None
    price = decision.refinement.price
    assert price is not None and price.relative is not None
    assert price.relative.reference == PresentedOrdinal(position=2)


@pytest.mark.parametrize(
    "path",
    ["reference", "comparison_references", "interaction"],
)
def test_every_decision_site_requires_the_tag(path: str) -> None:
    """All four locations enforce it, not only the one that was tested first."""
    payloads: dict[str, dict[str, Any]] = {
        "reference": {"action": "product_detail", "reference": {"position": 1}},
        "comparison_references": {
            "action": "compare",
            "comparison_references": [{"position": 1}, {"position": 2}],
        },
        "interaction": {
            "action": "answer",
            "interaction": {"op": "select", "reference": {"position": 1}},
        },
    }
    with pytest.raises(ValidationError):
        CustomerAgentDecision.model_validate(payloads[path])


def test_a_relative_price_still_rejects_a_product_id_reference() -> None:
    with pytest.raises(ValidationError):
        CustomerAgentDecision.model_validate(
            {
                "action": "refine_search",
                "refinement": {
                    "price": {
                        "op": "set_relative",
                        "relative": {
                            "relation": "percent_cheaper",
                            "reference": {"kind": "by_product_id", "product_id": 1},
                            "percent": "20",
                        },
                    }
                },
            }
        )


# ── ratified: a search need not propose anything ────────────────────────────


def test_a_search_without_a_new_search_proposal_is_valid() -> None:
    """M7 interprets the message; `NewSearchProposal` carries only the durable
    fuzzy wording, so having none is the ordinary case, not a defect."""
    decision = CustomerAgentDecision(action=AgentAction.SEARCH)

    assert decision.new_search is None


def test_a_new_search_proposal_on_a_non_search_action_is_rejected() -> None:
    """The converse still holds."""
    with pytest.raises(ValidationError):
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            new_search=NewSearchProposal(
                semantic_intent=SemanticIntentRefinement(
                    op=SemanticIntentOp.SET, value="cosy"
                )
            ),
        )


def test_an_interaction_still_cannot_carry_a_product_id() -> None:
    with pytest.raises(ValidationError):
        ProductInteractionIntent.model_validate(
            {"op": ProductInteractionOp.SELECT.value, "product_id": 165645}
        )
