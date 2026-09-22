"""M11B-2 added contracts and nothing that acts on them.

A phase gate is only meaningful if it is checkable. These assert the absence
of the next phases' work, so "contracts only" is a property of the repository
rather than a claim in a report.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).parents[2] / "app"

# Names that would mean a later phase had begun.
#
# Symbols leave this list as the subphase that owns them builds them:
# `SearchRefinementComposer` in M11B-3A, then the reference resolver,
# comparison service and agent settings in M11B-3B. What remains is what
# no phase has built yet.
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

M11_CONTRACT_MODULES = (
    "schemas/conversation.py",
    "schemas/agent_view.py",
    "schemas/agent_decision.py",
    "schemas/refinement.py",
    "schemas/grounding.py",
    "schemas/comparison.py",
    "schemas/agent_turn.py",
)


def _defined_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    }


@pytest.mark.parametrize("symbol", NOT_YET_REACHABLE)
def test_bundle_refinement_does_not_exist_yet(symbol: str) -> None:
    for module in APP.rglob("*.py"):
        assert symbol not in module.read_text(), f"{module.name} defines {symbol}"


@pytest.mark.parametrize("name", M11_CONTRACT_MODULES)
def test_the_contract_modules_define_no_behaviour(name: str) -> None:
    """Schemas, enums and validators - nothing that performs I/O.

    Parsed rather than grepped: a docstring that mentions the session layer is
    not a session, and a guard that cannot tell the difference is a guard
    somebody will weaken.
    """
    tree = ast.parse((APP / name).read_text())

    for node in ast.walk(tree):
        assert not isinstance(node, ast.AsyncFunctionDef), f"{name}: async def"
        assert not isinstance(node, ast.Await), f"{name}: await"
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots = {(node.module or "").split(".")[0]}
        else:
            continue
        assert not roots & {
            "openai",
            "sqlalchemy",
            "httpx",
            "requests",
            "redis",
            "boto3",
            "pinecone",
        }, f"{name}: {roots}"


def test_both_customer_agent_prompts_are_versioned() -> None:
    """M11B-4 added the decision prompt and M11B-6 the response prompt. Each
    is a versioned asset rather than a string in business logic."""
    prompts = {p.name for p in (APP / "prompts/customer_commerce").glob("*.py")}

    assert {"v1.py", "response_v1.py"} <= prompts


def test_no_agent_module_exists_yet() -> None:
    assert not (APP / "agents").exists()


def test_no_new_openai_call_site_was_added() -> None:
    """Query understanding remains the only place a provider is consulted."""
    callers = [
        module.relative_to(APP).as_posix()
        for module in APP.rglob("*.py")
        if "responses.parse" in module.read_text()
    ]

    assert callers == ["integrations/llm.py"]


def test_the_m11b_1_search_foundation_is_untouched() -> None:
    """This phase adds contracts; it does not revisit search correctness."""
    repository = (APP / "repositories/products.py").read_text()
    controlled = (APP / "services/controlled_search.py").read_text()

    assert "async def search_eligible_ids" in repository
    assert "async def search_eligible_pool" in repository
    assert "eligible_pool(request, context)" in controlled
    assert "_for_execution" not in controlled


@pytest.mark.parametrize("name", M11_CONTRACT_MODULES)
def test_contract_modules_import_no_service(name: str) -> None:
    tree = ast.parse((APP / name).read_text())

    for node in ast.walk(tree):
        module = node.module if isinstance(node, ast.ImportFrom) else None
        if module is None:
            continue
        assert "app.services" not in module, f"{name} imports {module}"
        assert "app.repositories" not in module, f"{name} imports {module}"
        assert "app.integrations" not in module, f"{name} imports {module}"


def test_every_contract_module_is_reachable() -> None:
    """A module nobody can import is not a contract."""
    import importlib

    for name in M11_CONTRACT_MODULES:
        dotted = "app." + name.removesuffix(".py").replace("/", ".")
        assert importlib.import_module(dotted)
        assert _defined_names(APP / name)
