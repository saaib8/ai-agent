"""Reviewed implied seat counts - loading and validation.

The registry is reviewed domain data, so the tests that matter are about what it
refuses: a type the taxonomy does not approve, a capacity that is not a positive
integer, a malformed file. A wrong implied count would put a guess into a
combination's seat total, which is exactly what recording it as data avoids.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.core.exceptions import TaxonomyConfigurationError
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.seating import load_seating_semantics

TAXONOMY = load_taxonomy()


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "seating.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_default_registry_carries_the_reviewed_defaults() -> None:
    seating = load_seating_semantics(taxonomy=TAXONOMY)

    assert seating.version == "v1"
    assert seating.implied_capacity("chair") == 1
    assert seating.implied_capacity("single-seater-sofa") == 1
    assert seating.implied_capacity("stool") == 1


def test_a_type_the_catalog_records_is_not_given_a_default() -> None:
    """A sofa's seat count comes from the catalog; the default must not shadow
    it. And a type the review has not settled (recliner) stays unknown."""
    seating = load_seating_semantics(taxonomy=TAXONOMY)

    assert seating.implied_capacity("sofa") is None
    assert seating.implied_capacity("recliner") is None
    assert seating.implied_capacity(None) is None


def test_it_rejects_a_type_the_taxonomy_does_not_approve(tmp_path: Path) -> None:
    path = _write(tmp_path, "version: v1\nimplied_capacity:\n  luxury-couch: 1\n")

    with pytest.raises(TaxonomyConfigurationError, match="not an approved seating"):
        load_seating_semantics(path=path, taxonomy=TAXONOMY)


def test_it_rejects_a_non_seating_type(tmp_path: Path) -> None:
    """A table type can never acquire an implied seat count."""
    path = _write(tmp_path, "version: v1\nimplied_capacity:\n  console: 1\n")

    with pytest.raises(TaxonomyConfigurationError, match="not an approved seating"):
        load_seating_semantics(path=path, taxonomy=TAXONOMY)


@pytest.mark.parametrize("value", ["0", "-1", "true", "1.5", "one"])
def test_it_rejects_a_capacity_that_is_not_a_positive_integer(tmp_path: Path, value: str) -> None:
    path = _write(tmp_path, f"version: v1\nimplied_capacity:\n  chair: {value}\n")

    with pytest.raises(TaxonomyConfigurationError, match="positive integer"):
        load_seating_semantics(path=path, taxonomy=TAXONOMY)


def test_it_rejects_a_missing_version(tmp_path: Path) -> None:
    path = _write(tmp_path, "implied_capacity:\n  chair: 1\n")

    with pytest.raises(TaxonomyConfigurationError, match="version"):
        load_seating_semantics(path=path, taxonomy=TAXONOMY)


def test_it_rejects_an_empty_mapping(tmp_path: Path) -> None:
    path = _write(tmp_path, "version: v1\nimplied_capacity: {}\n")

    with pytest.raises(TaxonomyConfigurationError, match="implied_capacity"):
        load_seating_semantics(path=path, taxonomy=TAXONOMY)
