"""What no prompt may contain, for every prompt there is and will be.

Each prompt module has its own tests for what it says. This one is about what
none of them may say, and it discovers the prompts rather than listing them, so
a prompt added later is guarded the day it appears rather than the day someone
remembers to add it here.

Two rules, and they are the same rule twice. A prompt carries no secret, and a
prompt carries no identity - no product id, no store id, no index name. The
first is obvious. The second is the authority argument the whole agent rests
on: a model that has seen an id can emit one, and a plausible-looking id is
exactly the hallucination a membership check waves through (CLAUDE.md 20.3).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROMPTS = Path(__file__).parents[2] / "app/prompts"
PROMPT_MODULES = sorted(
    module for module in PROMPTS.rglob("*.py") if module.name != "__init__.py"
)


def _prompt_text(module: Path) -> str:
    """Every string literal the module defines, joined.

    Read from the source rather than by importing, so a prompt that is built
    from parts is still covered and no module has to expose a particular name.
    """
    tree = ast.parse(module.read_text())
    return "\n".join(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def test_there_are_prompts_to_check() -> None:
    """Guards the guard: a glob that matches nothing passes everything."""
    assert len(PROMPT_MODULES) >= 2
    assert any("query_understanding" in m.as_posix() for m in PROMPT_MODULES)
    assert any("customer_commerce" in m.as_posix() for m in PROMPT_MODULES)


IDENTIFIERS = (
    "product_id",
    "store_id",
    "retailer_id",
    "pinecone_id",
    "salla_product_id",
    "uuid",
    "presented_search_revision",
    "schema_version",
)


@pytest.mark.parametrize("module", PROMPT_MODULES, ids=lambda m: m.stem)
@pytest.mark.parametrize("forbidden", IDENTIFIERS)
def test_no_prompt_names_an_internal_identifier(module: Path, forbidden: str) -> None:
    assert forbidden not in _prompt_text(module), f"{module.name}: {forbidden}"


SECRETS = (
    "api_key",
    "apikey",
    "sk-",
    "secret",
    "password",
    "token",
    "postgresql://",
    "postgres://",
    "redis://",
    "dsn",
    "bearer ",
    "authorization",
)


@pytest.mark.parametrize("module", PROMPT_MODULES, ids=lambda m: m.stem)
@pytest.mark.parametrize("forbidden", SECRETS)
def test_no_prompt_carries_a_credential(module: Path, forbidden: str) -> None:
    assert forbidden not in _prompt_text(module).lower(), (
        f"{module.name}: {forbidden}"
    )


@pytest.mark.parametrize("module", PROMPT_MODULES, ids=lambda m: m.stem)
def test_no_prompt_hardcodes_a_model_or_index_name(module: Path) -> None:
    """Configuration, not prose (CLAUDE.md 31)."""
    text = _prompt_text(module).lower()

    for forbidden in ("gpt-", "text-embedding", "claude-", "prod-index", "-index"):
        assert forbidden not in text, f"{module.name}: {forbidden}"


@pytest.mark.parametrize("module", PROMPT_MODULES, ids=lambda m: m.stem)
def test_no_prompt_embeds_a_catalog_row(module: Path) -> None:
    """Prompts carry vocabulary at most, never inventory (CLAUDE.md 28).

    A price, a URL or a product name in a prompt is a catalog fact the model
    could restate as current when it is not.
    """
    text = _prompt_text(module)

    for forbidden in ("http://", "https://", "SAR ", "core_product"):
        assert forbidden not in text, f"{module.name}: {forbidden}"


@pytest.mark.parametrize("module", PROMPT_MODULES, ids=lambda m: m.stem)
def test_no_prompt_asks_for_hidden_reasoning(module: Path) -> None:
    """Bounded structured output, never an exposed chain of thought
    (CLAUDE.md 18)."""
    text = _prompt_text(module).lower()

    for forbidden in ("chain of thought", "step by step", "think out loud"):
        assert forbidden not in text, f"{module.name}: {forbidden}"


@pytest.mark.parametrize("module", PROMPT_MODULES, ids=lambda m: m.stem)
def test_every_prompt_is_versioned(module: Path) -> None:
    """A prompt is a versioned asset, so a trace can name which one ran."""
    tree = ast.parse(module.read_text())
    assigned = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    assert "VERSION" in assigned, module.name
