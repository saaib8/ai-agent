"""The golden set is expectations of model behaviour, checked for coherence.

pytest never calls a provider. What it can verify deterministically is that
every expectation is itself valid and self-consistent: a case expecting an
unapproved taxonomy value, or an amount with no currency, would silently become
an un-passable test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from app.schemas.discovery import DimensionConstraintKind, ProductSort
from app.schemas.query import (
    ClarificationReason,
    ConstraintStrength,
    RequirementFamily,
)
from app.taxonomy.attributes import AttributeFamily, load_catalog_attributes
from app.taxonomy.dimensions import DimensionRole, UnsupportedDimensionReason
from app.taxonomy.registry import load_taxonomy

GOLDEN_PATH = Path(__file__).parents[2] / "evals/query_understanding/golden_v2.yaml"
TAXONOMY = load_taxonomy()
ATTRIBUTES = load_catalog_attributes()
DOCUMENT: dict[str, Any] = yaml.safe_load(GOLDEN_PATH.read_text())
CASES: list[dict[str, Any]] = DOCUMENT["cases"]
IDS = [case["message"][:45] for case in CASES]

STRENGTH_KEYS = (
    "commerce_subcategory_strength",
    "price_min_strength",
    "price_max_strength",
    "seating_capacity_min_strength",
    "seating_capacity_max_strength",
)


def test_the_golden_set_is_versioned_and_broad_enough() -> None:
    assert DOCUMENT["version"] == 2
    assert len(CASES) >= 35


def test_every_message_is_unique() -> None:
    messages = [case["message"] for case in CASES]
    assert len(messages) == len(set(messages))


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_every_expectation_uses_approved_taxonomy(case: dict[str, Any]) -> None:
    if case["outcome"] in {"clarification", "any"} or case.get("skip_category"):
        return
    category = case["commerce_category"]
    assert TAXONOMY.is_category(category), category
    subcategory = case.get("commerce_subcategory")
    if subcategory is not None:
        assert TAXONOMY.is_pair(category, subcategory), (category, subcategory)


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_every_expectation_is_well_formed(case: dict[str, Any]) -> None:
    assert case["outcome"] in {
        "resolved", "clarification", "unsupported", "unsupported_dimension",
        "unresolved", "any",
    }

    if case["outcome"] == "any":
        assert case["forbids"], case["message"]
        return
    if case["outcome"] == "clarification":
        ClarificationReason(case["reason"])
        return
    if case["outcome"] == "unsupported":
        families = case["unsupported"]
        assert families, case["message"]
        for family in families:
            RequirementFamily(family)
    if case["outcome"] == "unresolved":
        assert case["unresolved"], case["message"]
        for family in case["unresolved"]:
            AttributeFamily(family)
    if case["outcome"] == "unsupported_dimension":
        assert case["unsupported_dimensions"], case["message"]
        for entry in case["unsupported_dimensions"]:
            role, reason, strength = entry.split(":")
            DimensionRole(role)
            UnsupportedDimensionReason(reason)
            ConstraintStrength(strength)

    # Every stated dimension expectation must be well formed and consistent
    # with the kind it claims.
    for entry in case.get("dimensions") or []:
        role, kind, low, high, target = entry.split(":")
        DimensionRole(role)
        DimensionConstraintKind(kind)
        present = {k for k, v in (("min", low), ("max", high), ("target", target)) if v != "-"}
        expected = {
            "min": {"min"}, "max": {"max"},
            "range": {"min", "max"}, "target": {"target"},
        }[kind]
        assert present == expected, case["message"]

    # A strength is recorded per measurement, for measurements that exist.
    strengths = case.get("dimension_strengths") or []
    for entry in strengths:
        role, strength = entry.split(":")
        DimensionRole(role)
        ConstraintStrength(strength)
    if strengths:
        assert {e.split(":")[0] for e in strengths} == {
            e.split(":")[0] for e in (case.get("dimensions") or [])
        }, case["message"]
    if (planar_strength := case.get("planar_strength")) is not None:
        ConstraintStrength(planar_strength)
        assert case.get("planar"), case["message"]

    # Exact attribute filters must name approved values, in the right family.
    for value in case.get("colors_any_of") or []:
        assert ATTRIBUTES.is_color(value), value
    for value in case.get("styles_all_of") or []:
        assert ATTRIBUTES.is_style(value), value
    for preference in case.get("preferences") or []:
        family, canonical, strength = preference.split(":")
        AttributeFamily(family)
        ConstraintStrength(strength)
        if canonical != "-":
            assert ATTRIBUTES.is_value(AttributeFamily(family), canonical), canonical

    if (sort := case.get("sort")) is not None:
        ProductSort(sort)
    for key in STRENGTH_KEYS:
        if (strength := case.get(key)) is not None:
            ConstraintStrength(strength)
    if case.get("price_max") is not None or case.get("price_min") is not None:
        # An amount without a currency would expect clarification instead.
        assert case.get("price_currency"), case["message"]


def test_every_evaluation_group_is_represented() -> None:
    outcomes = {case["outcome"] for case in CASES}
    assert outcomes == {
        "resolved", "clarification", "unsupported", "unsupported_dimension",
        "unresolved", "any",
    }

    # Every reason the service can return has a live case, so a new one cannot
    # ship without evidence that the model actually produces it.
    reasons = {c.get("reason") for c in CASES if c["outcome"] == "clarification"}
    assert reasons == {reason.value for reason in ClarificationReason}

    # Colour and style became structured filters or preferences in M8A, and
    # dimensions became a deterministic filter in M8B. Material alone remains
    # under the generic unsupported path.
    families = {f for c in CASES for f in c.get("unsupported", [])}
    assert families == {RequirementFamily.MATERIAL.value}
    assert RequirementFamily.COLOR.value not in families
    assert RequirementFamily.STYLE.value not in families
    assert RequirementFamily.DIMENSIONS.value not in families

    strengths = {
        c[key] for c in CASES for key in STRENGTH_KEYS if c.get(key) is not None
    }
    assert strengths == {strength.value for strength in ConstraintStrength}

    sorts = {c.get("sort") for c in CASES if c.get("sort")}
    assert sorts == {"price_asc", "price_desc", "default"}


def test_the_single_seater_case_expects_no_capacity_constraint() -> None:
    """Reviewed single-seater rows store NULL capacity (CLAUDE.md 31)."""
    case = next(c for c in CASES if "single-seater" in c["message"])

    assert case["commerce_subcategory"] == "single-seater-sofa"
    assert case["seating_capacity_min"] is None
    assert case["seating_capacity_max"] is None


def test_a_prohibition_case_names_a_superseded_value() -> None:
    prohibitions = [c for c in CASES if c["outcome"] == "any"]
    assert prohibitions
    assert any("l-shape-sofa" in c["forbids"] for c in prohibitions)


def test_superseded_values_are_never_expected() -> None:
    for case in CASES:
        assert case.get("commerce_subcategory") not in {
            "l-shape-sofa",
            "side-table",
            "lampshade",
            "floor-stand",
        }


def test_chandelier_and_pendant_lighting_are_distinct_expectations() -> None:
    subcategories = {c.get("commerce_subcategory") for c in CASES}
    assert {"chandelier", "pendant-lighting"} <= subcategories


def test_an_unqualified_bound_expects_the_locked_default() -> None:
    case = next(c for c in CASES if c["message"] == "sofas under 5000 SAR")
    assert case["price_max_strength"] == "locked"


def test_injection_cases_never_expect_an_unapproved_value() -> None:
    injections = [c for c in CASES if "nore" in c["message"] or "not allowed" in c["message"]]
    assert len(injections) >= 4
    for case in injections:
        if case["outcome"] not in {"clarification", "any"}:
            assert TAXONOMY.is_pair(
                case["commerce_category"], case["commerce_subcategory"]
            )


def test_colour_and_style_coverage_is_present() -> None:
    """Both roles, both families, and the collision pair."""
    exact_colors = {v for c in CASES for v in (c.get("colors_any_of") or [])}
    exact_styles = {v for c in CASES for v in (c.get("styles_all_of") or [])}
    preferences = [p for c in CASES for p in (c.get("preferences") or [])]

    assert exact_colors  # strict canonical colour
    assert exact_styles  # strict canonical style
    assert {"Modern_Classic", "Rustic_Modern"} <= exact_styles
    assert any(p.startswith("color:") for p in preferences)
    assert any(p.startswith("style:") for p in preferences)
    # Non-canonical values kept semantic rather than mapped to a near match.
    assert any(p.split(":")[1] == "-" for p in preferences)


def test_strict_non_canonical_cases_expect_the_unresolved_outcome() -> None:
    unresolved = [c for c in CASES if c["outcome"] == "unresolved"]

    assert len(unresolved) >= 2
    assert all(c["unresolved"] == ["color"] for c in unresolved)
    # They must not also carry an exact filter: the value is not in the vocabulary.
    assert all(not c.get("colors_any_of") for c in unresolved)


def test_ordinary_colour_wording_never_expects_an_exact_filter() -> None:
    """The M8A rule: wanting a colour is a leaning, not an exclusion."""
    for case in CASES:
        if any(p.startswith("color:") for p in (case.get("preferences") or [])):
            assert not case.get("colors_any_of"), case["message"]


# ── parent/child name collisions ────────────────────────────────────────────


def _name_collisions() -> list[str]:
    """Categories that contain a subcategory bearing the category's own name."""
    return sorted(c for c in TAXONOMY.categories if c in TAXONOMY.subcategories(c))


def test_the_registry_is_scanned_for_name_collisions() -> None:
    """Documents which collisions exist, so a new one cannot appear unnoticed."""
    assert _name_collisions() == ["lighting"]


@pytest.mark.parametrize("category", _name_collisions())
def test_every_collision_has_a_broad_request_case(category: str) -> None:
    """A broad ask for such a category must expect the category alone.

    Selecting the same-named child would narrow the customer to the leftovers
    and hide the specific types under it.
    """
    broad = [
        c
        for c in CASES
        if c["outcome"] == "resolved"
        and c.get("commerce_category") == category
        and c.get("commerce_subcategory") is None
    ]

    assert broad, f"no broad-request case for the colliding category {category!r}"
    assert all(c["commerce_subcategory"] is None for c in broad)


@pytest.mark.parametrize("category", _name_collisions())
def test_no_case_ever_expects_the_same_named_child(category: str) -> None:
    expectations = [c.get("commerce_subcategory") for c in CASES]
    assert category not in expectations


def test_the_collision_rule_is_stated_generally_in_the_prompt() -> None:
    """Prompt-level, taxonomy-derived: no category is named in the instruction."""
    source = (
        Path(__file__).parents[2] / "app/prompts/query_understanding/v1.py"
    ).read_text()

    assert "same-named child" in source
    for category in TAXONOMY.categories:
        assert f'"{category}"' not in source, category


# ── dimension coverage ──────────────────────────────────────────────────────


def test_dimension_kinds_and_roles_are_all_exercised() -> None:
    entries = [e for c in CASES for e in (c.get("dimensions") or [])]
    kinds = {e.split(":")[1] for e in entries}
    roles = {e.split(":")[0] for e in entries}

    assert kinds == {k.value for k in DimensionConstraintKind}
    assert {"overall_width", "depth", "height", "length"} <= roles


def test_a_target_expectation_is_never_written_as_a_maximum() -> None:
    for case in CASES:
        for entry in case.get("dimensions") or []:
            _, kind, _, high, target = entry.split(":")
            if kind == "target":
                assert target != "-" and high == "-", case["message"]


def test_vague_size_words_expect_no_measurement() -> None:
    for word in ("compact", "small", "low-profile"):
        cases = [c for c in CASES if word in c["message"]]
        assert cases, word
        for case in cases:
            assert not case.get("dimensions"), case["message"]


def test_unsafe_families_expect_the_unsupported_dimension_outcome() -> None:
    unsupported = [c for c in CASES if c["outcome"] == "unsupported_dimension"]
    subcategories = {c["commerce_subcategory"] for c in unsupported}

    assert {"bed", "sectional-sofa", "chair"} <= subcategories
    assert all(not c.get("dimensions") for c in unsupported)


def test_a_planar_case_exists_in_both_orientations() -> None:
    planar = [c for c in CASES if c.get("planar")]
    assert len(planar) >= 2
    assert all(c["commerce_subcategory"] == "carpet" for c in planar)


def test_every_dimension_strength_is_exercised() -> None:
    stated = {
        entry.split(":")[1]
        for case in CASES
        for entry in (case.get("dimension_strengths") or [])
    }

    assert stated == {s.value for s in ConstraintStrength}


def test_a_mixed_strength_case_exists() -> None:
    """One message, two measurements, two different strengths.

    This is the case a single global dimension strength could not represent.
    """
    mixed = [
        case
        for case in CASES
        if len({e.split(":")[1] for e in (case.get("dimension_strengths") or [])}) > 1
    ]

    assert mixed, "no case distinguishes two measurements by strength"
    for case in mixed:
        assert len(case["dimensions"]) > 1, case["message"]


def test_planar_strength_is_exercised_both_ways() -> None:
    stated = {c["planar_strength"] for c in CASES if c.get("planar_strength")}

    assert {"locked", "approximate"} <= stated


def test_both_dimension_clarification_reasons_have_a_live_case() -> None:
    """Deterministic tests cover these; only a live case shows the model agrees."""
    reasons = {c.get("reason") for c in CASES if c["outcome"] == "clarification"}

    assert ClarificationReason.MISSING_DIMENSION_ROLE.value in reasons
    assert ClarificationReason.MISSING_DIMENSION_UNIT.value in reasons


def test_a_refused_dimension_keeps_the_strength_it_was_stated_with() -> None:
    refused = [c for c in CASES if c["outcome"] == "unsupported_dimension"]

    assert refused
    for case in refused:
        for entry in case["unsupported_dimensions"]:
            assert ConstraintStrength(entry.split(":")[2])
