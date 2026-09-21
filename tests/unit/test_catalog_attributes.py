"""The catalog-attribute registry, and the routing of colour/style intent."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from app.core.exceptions import LLMResponseInvalidError, TaxonomyConfigurationError
from app.integrations.llm import StructuredLLMClient
from app.schemas.query import (
    AttributeInterpretation,
    CommerceInterpretation,
    ConstraintStrength,
    ResolvedSearch,
    UnresolvedStrictRequirement,
    UnsupportedRequirement,
    build_constrained_interpretation,
)
from app.services.query_understanding import QueryUnderstandingService
from app.taxonomy.attributes import (
    DEFAULT_ATTRIBUTES_PATH,
    AttributeFamily,
    load_catalog_attributes,
)
from app.taxonomy.dimensions import load_dimension_semantics
from app.taxonomy.registry import load_taxonomy

ATTRIBUTES = load_catalog_attributes()
TAXONOMY = load_taxonomy()
DIMENSIONS = load_dimension_semantics(taxonomy=TAXONOMY)
COLOR = AttributeFamily.COLOR
STYLE = AttributeFamily.STYLE
LOCKED = ConstraintStrength.LOCKED
PREFERRED = ConstraintStrength.PREFERRED
APPROXIMATE = ConstraintStrength.APPROXIMATE

APPROVED_COLORS = {
    "Ivory", "Beige", "Taupe", "Sand", "Greige", "Light Grey", "Grey", "White",
    "Charcoal", "Black", "Oak", "Walnut", "Espresso", "Clay", "Terracotta",
    "Olive", "Mustard", "Burnt Orange", "Gold", "Brass", "Bronze", "Sage",
    "Forest Green", "Powder Blue", "Denim Blue", "Navy", "Teal", "Emerald",
    "Sapphire", "Ruby", "Plum", "Blush", "Lavender",
}
APPROVED_STYLES = {
    "Modern", "Contemporary", "Minimalist", "Boho", "Industrial", "Classy",
    "Modern_Classic", "Rustic_Modern", "Eclectic", "Zen", "Shabby_Chic",
    "Islamic", "Tropical", "Scandinavian", "Mid_Century", "Japandi", "Coastal",
    "Traditional", "Moroccan",
}


# ── registry ────────────────────────────────────────────────────────────────


def test_the_approved_vocabularies_load() -> None:
    assert ATTRIBUTES.colors == frozenset(APPROVED_COLORS)
    assert ATTRIBUTES.styles == frozenset(APPROVED_STYLES)
    assert ATTRIBUTES.version == "v1"


def test_families_do_not_leak_into_one_another() -> None:
    assert ATTRIBUTES.is_color("Beige")
    assert not ATTRIBUTES.is_style("Beige")
    assert ATTRIBUTES.is_style("Modern")
    assert not ATTRIBUTES.is_color("Modern")
    assert not ATTRIBUTES.is_value(COLOR, "Modern")
    assert not ATTRIBUTES.is_value(STYLE, "Beige")


def test_no_approved_style_contains_a_space() -> None:
    """Stored styles are split on commas after spaces are stripped."""
    assert all(" " not in style for style in ATTRIBUTES.styles)


def test_colliding_styles_are_distinct_values() -> None:
    for style in ("Modern", "Modern_Classic", "Rustic_Modern"):
        assert ATTRIBUTES.is_style(style)


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        ("version: v1\nstyles: [Modern]\n", "colors"),
        ("version: v1\ncolors: [Beige]\n", "styles"),
        ("version: v1\ncolors: []\nstyles: [Modern]\n", "colors"),
        ("version: v1\ncolors: [Beige]\nstyles: [Modern, Modern]\n", "more than once"),
        ("version: v1\ncolors: [Beige]\nstyles: ['Mid Century']\n", "must not contain spaces"),
        ("version: ''\ncolors: [Beige]\nstyles: [Modern]\n", "version"),
        ("[]\n", "mapping"),
        ("::: not yaml :::\n", "YAML"),
    ],
)
def test_a_malformed_registry_is_rejected(
    tmp_path: Path, document: str, reason: str
) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text(document)

    with pytest.raises(TaxonomyConfigurationError, match=reason):
        load_catalog_attributes(path)


def test_only_the_registry_enumerates_the_vocabulary() -> None:
    """No prompt, service, schema or repository restates a colour or style."""
    app = Path(__file__).parents[2] / "app"
    samples = ("Beige", "Terracotta", "Modern_Classic", "Scandinavian", "Japandi")
    for module in app.rglob("*.py"):
        source = module.read_text()
        for value in samples:
            assert f'"{value}"' not in source, f"{module.name} hardcodes {value}"
    assert "Beige" in DEFAULT_ATTRIBUTES_PATH.read_text()


# ── the model-facing schema ─────────────────────────────────────────────────


def test_canonical_values_are_constrained_to_the_registry() -> None:
    model = build_constrained_interpretation(TAXONOMY, ATTRIBUTES)
    schema = model.model_json_schema()
    attribute = schema["$defs"]["ConstrainedAttributeInterpretation"]
    allowed = set(attribute["properties"]["canonical_value"]["anyOf"][0]["enum"])

    assert allowed == APPROVED_COLORS | APPROVED_STYLES
    assert "red" not in allowed
    assert "warm neutral" not in allowed


# ── routing ─────────────────────────────────────────────────────────────────


class FakeLLMClient:
    def __init__(self, interpretation: CommerceInterpretation) -> None:
        self._interpretation = interpretation

    @property
    def model(self) -> str:
        return "fake-model"

    async def parse(self, **_: Any) -> Any:
        return self._interpretation


async def _interpret(*attributes: AttributeInterpretation, **extra: Any) -> Any:
    interpretation = CommerceInterpretation(
        commerce_category="seating",
        commerce_subcategory="sofa",
        attributes=list(attributes),
        **extra,
    )
    service = QueryUnderstandingService(
        cast(StructuredLLMClient, FakeLLMClient(interpretation)), TAXONOMY, ATTRIBUTES, DIMENSIONS
    )
    return await service.interpret("a message")


def _attr(
    family: AttributeFamily,
    raw: str,
    canonical: str | None,
    strength: ConstraintStrength,
) -> AttributeInterpretation:
    return AttributeInterpretation(
        family=family, raw_value=raw, canonical_value=canonical, strength=strength
    )


async def test_ordinary_canonical_wording_becomes_a_preference_not_a_filter() -> None:
    """"I want a Beige sofa" must not exclude every sofa that is not Beige."""
    outcome = await _interpret(_attr(COLOR, "Beige", "Beige", PREFERRED))

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.request.colors_any_of == ()
    assert len(outcome.semantic_preferences) == 1
    preference = outcome.semantic_preferences[0]
    assert (preference.family, preference.canonical_value, preference.strength) == (
        COLOR,
        "Beige",
        PREFERRED,
    )


async def test_strict_canonical_wording_becomes_an_exact_filter() -> None:
    outcome = await _interpret(_attr(COLOR, "Beige", "Beige", LOCKED))

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.request.colors_any_of == ("Beige",)
    assert outcome.semantic_preferences == ()


async def test_strict_canonical_style_becomes_an_exact_filter() -> None:
    outcome = await _interpret(_attr(STYLE, "Japandi", "Japandi", LOCKED))

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.request.styles_all_of == ("Japandi",)


async def test_several_strict_colours_are_alternatives() -> None:
    outcome = await _interpret(
        _attr(COLOR, "Beige", "Beige", LOCKED), _attr(COLOR, "Taupe", "Taupe", LOCKED)
    )

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.request.colors_any_of == ("Beige", "Taupe")


async def test_several_strict_styles_are_all_required() -> None:
    outcome = await _interpret(
        _attr(STYLE, "Modern", "Modern", LOCKED),
        _attr(STYLE, "Contemporary", "Contemporary", LOCKED),
    )

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.request.styles_all_of == ("Modern", "Contemporary")


async def test_a_non_canonical_preference_stays_semantic() -> None:
    """"I want a red sofa": red is preserved, never mapped onto Ruby."""
    outcome = await _interpret(_attr(COLOR, "red", None, PREFERRED))

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.request.colors_any_of == ()
    preference = outcome.semantic_preferences[0]
    assert preference.raw_value == "red"
    assert preference.canonical_value is None


async def test_broad_language_is_preserved_rather_than_forced() -> None:
    outcome = await _interpret(_attr(COLOR, "warm neutral", None, APPROXIMATE))

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.semantic_preferences[0].raw_value == "warm neutral"
    assert outcome.semantic_preferences[0].canonical_value is None
    assert outcome.request.colors_any_of == ()


async def test_a_strict_non_canonical_value_is_surfaced_not_downgraded() -> None:
    """"It must be red" is neither a filter we can run nor a mere leaning."""
    outcome = await _interpret(_attr(COLOR, "red", None, LOCKED))

    assert isinstance(outcome, UnresolvedStrictRequirement)
    assert outcome.unresolved[0].family is COLOR
    assert outcome.unresolved[0].raw_value == "red"
    assert outcome.request.colors_any_of == ()
    # The two outcomes are disjoint types, so a caller handling only the
    # resolved case cannot receive this one. mypy enforces that statically.


async def test_colour_and_style_together_stay_separate() -> None:
    outcome = await _interpret(
        _attr(COLOR, "Beige", "Beige", PREFERRED),
        _attr(STYLE, "Modern", "Modern", PREFERRED),
    )

    assert isinstance(outcome, ResolvedSearch)
    families = {p.family for p in outcome.semantic_preferences}
    assert families == {COLOR, STYLE}
    assert outcome.request.colors_any_of == ()
    assert outcome.request.styles_all_of == ()


async def test_a_cross_family_value_is_rejected() -> None:
    """A style offered as a colour is untrusted output, not a near miss."""
    with pytest.raises(LLMResponseInvalidError):
        await _interpret(_attr(COLOR, "Modern", "Modern", LOCKED))


async def test_colour_is_no_longer_an_unsupported_requirement() -> None:
    outcome = await _interpret(_attr(COLOR, "Beige", "Beige", PREFERRED))

    assert not isinstance(outcome, UnsupportedRequirement)


async def test_material_and_dimensions_remain_unsupported() -> None:
    from app.schemas.query import RequirementFamily

    for family in (RequirementFamily.MATERIAL, RequirementFamily.DIMENSIONS):
        outcome = await _interpret(unsupported_requirements=[family])
        assert isinstance(outcome, UnsupportedRequirement)
        assert outcome.unsupported == (family,)


async def test_a_duplicate_strict_colour_is_recorded_once() -> None:
    outcome = await _interpret(
        _attr(COLOR, "Beige", "Beige", LOCKED), _attr(COLOR, "beige", "Beige", LOCKED)
    )

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.request.colors_any_of == ("Beige",)


# ── vocabulary integrity ────────────────────────────────────────────────────


def test_japandi_is_the_canonical_token_and_ndi_is_not() -> None:
    assert ATTRIBUTES.is_style("Japandi")
    assert not ATTRIBUTES.is_style("ndi")
    assert "ndi" not in ATTRIBUTES.styles


def test_no_source_treats_ndi_as_a_style_token() -> None:
    """The truncation must appear nowhere as a value - comments aside."""
    import re

    root = Path(__file__).parents[2]
    token = re.compile(r"(?<![A-Za-z])ndi(?![A-Za-z])")
    for module in (*root.glob("app/**/*.py"), *root.glob("evals/**/*.yaml")):
        for line in module.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # a comment explaining the correction is not a value
            assert not token.search(stripped), f"{module.name}: {line}"
