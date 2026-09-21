"""Settings validation: the one place runtime configuration is defined."""

from __future__ import annotations

import json
from typing import Any

import pytest
from app.core.config import (
    AwsSecretsManagerSource,
    Environment,
    PineconeSettings,
    Settings,
    get_settings,
)
from pydantic import SecretStr, ValidationError

from tests.conftest import UNIT_TEST_DSN, UNIT_TEST_REDIS_URL, build_settings


def test_nested_values_come_from_prefixed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZORY_ENVIRONMENT", "test")
    monkeypatch.setenv("ZORY_DB__DSN", UNIT_TEST_DSN)
    monkeypatch.setenv("ZORY_DB__POOL_SIZE", "27")
    monkeypatch.setenv("ZORY_REDIS__URL", UNIT_TEST_REDIS_URL)
    monkeypatch.setenv("ZORY_LLM__API_KEY", "test-key-not-real")
    monkeypatch.setenv("ZORY_LLM__MODEL", "test-model")
    monkeypatch.setenv("ZORY_OBSERVABILITY__LOG_LEVEL", "WARNING")

    settings = Settings(_env_file=None)

    assert settings.environment is Environment.TEST
    assert settings.db.pool_size == 27
    assert settings.observability.log_level == "WARNING"


def test_a_blocking_database_driver_is_rejected() -> None:
    """A sync driver on the async request path would stall the event loop."""
    with pytest.raises(ValidationError, match="asyncpg"):
        build_settings(db={"dsn": "postgresql://zory:zory@localhost:5432/zory"})


def test_wildcard_cors_origin_is_rejected() -> None:
    with pytest.raises(ValidationError, match="allowlist"):
        build_settings(api={"cors_origins": ["*"]})


def test_redis_url_scheme_is_validated() -> None:
    with pytest.raises(ValidationError, match="redis"):
        build_settings(redis={"url": "http://localhost:6379"})


def test_api_prefix_must_be_a_path() -> None:
    with pytest.raises(ValidationError, match="must start with"):
        build_settings(api={"prefix": "v1"})


def test_api_prefix_loses_its_trailing_slash() -> None:
    assert build_settings(api={"prefix": "/v1/"}).api.prefix == "/v1"


def test_unknown_settings_are_rejected_rather_than_ignored() -> None:
    with pytest.raises(ValidationError):
        build_settings(totally_unknown_field="x")


def test_missing_required_settings_fail_fast() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="test")


def test_redacted_summary_leaks_no_secret() -> None:
    settings = build_settings()
    rendered = json.dumps(settings.redacted())

    assert "test-key-not-real" not in rendered
    assert "zory:zory" not in rendered
    assert UNIT_TEST_DSN not in rendered
    assert UNIT_TEST_REDIS_URL not in rendered
    assert settings.redacted()["db_read_only"] is True


def test_repr_does_not_render_secrets() -> None:
    settings = build_settings()
    assert "zory:zory" not in repr(settings)
    assert "zory:zory" not in str(settings.db.dsn)


@pytest.mark.parametrize("environment", ["local", "test"])
def test_secrets_manager_source_is_inert_outside_stage_and_prod(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    """Local and test runs must not reach for AWS, or import boto3."""
    monkeypatch.setenv("ZORY_ENVIRONMENT", environment)
    monkeypatch.setenv("ZORY_AWS__SECRET_NAME", "zory/agent")

    assert AwsSecretsManagerSource(Settings)() == {}


def test_secrets_manager_source_is_inert_without_a_secret_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZORY_ENVIRONMENT", "prod")
    monkeypatch.delenv("ZORY_AWS__SECRET_NAME", raising=False)

    assert AwsSecretsManagerSource(Settings)() == {}


def test_secrets_manager_source_returns_the_secret_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZORY_ENVIRONMENT", "prod")
    monkeypatch.setenv("ZORY_AWS__SECRET_NAME", "zory/agent")
    payload = {"db": {"dsn": UNIT_TEST_DSN}}

    class _FakeClient:
        def get_secret_value(self, SecretId: str) -> dict[str, str]:  # noqa: N803
            assert SecretId == "zory/agent"
            return {"SecretString": json.dumps(payload)}

    class _FakeBoto3:
        @staticmethod
        def client(service: str, region_name: str) -> _FakeClient:
            assert service == "secretsmanager"
            return _FakeClient()

    monkeypatch.setitem(__import__("sys").modules, "boto3", _FakeBoto3)

    assert AwsSecretsManagerSource(Settings)() == payload


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZORY_ENVIRONMENT", "test")
    monkeypatch.setenv("ZORY_DB__DSN", UNIT_TEST_DSN)
    monkeypatch.setenv("ZORY_REDIS__URL", UNIT_TEST_REDIS_URL)
    monkeypatch.setenv("ZORY_LLM__API_KEY", "test-key-not-real")
    monkeypatch.setenv("ZORY_LLM__MODEL", "test-model")
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()


def test_environment_knows_where_secrets_live() -> None:
    assert Environment.PROD.uses_secrets_manager
    assert Environment.STAGE.uses_secrets_manager
    assert not Environment.LOCAL.uses_secrets_manager
    assert not Environment.TEST.uses_secrets_manager


def test_read_only_is_the_default() -> None:
    """Django owns every write to the catalog."""
    settings: Any = build_settings()
    assert settings.db.read_only is True


# ── semantic ranking settings (M9) ──────────────────────────────────────────


def test_pinecone_is_absent_by_default() -> None:
    """Semantic ranking is an enhancement: no Pinecone is a valid running state."""
    settings = build_settings()

    assert settings.pinecone is None
    assert settings.redacted()["semantic_ranking_configured"] is False


def test_pinecone_settings_load_from_the_nested_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZORY_DB__DSN", UNIT_TEST_DSN)
    monkeypatch.setenv("ZORY_REDIS__URL", UNIT_TEST_REDIS_URL)
    monkeypatch.setenv("ZORY_LLM__API_KEY", "test-key-not-real")
    monkeypatch.setenv("ZORY_LLM__MODEL", "test-model")
    monkeypatch.setenv("ZORY_LLM__EMBEDDING_MODEL", "test-embedding-model")
    monkeypatch.setenv("ZORY_PINECONE__API_KEY", "pc-test-key")
    monkeypatch.setenv("ZORY_PINECONE__INDEX_NAME", "ai-agent")

    settings = Settings(_env_file=None)

    assert settings.pinecone is not None
    assert settings.pinecone.index_name == "ai-agent"
    assert settings.llm.embedding_model == "test-embedding-model"
    assert settings.redacted()["semantic_ranking_configured"] is True


def test_the_pinecone_key_never_renders() -> None:
    settings = build_settings(
        pinecone={"api_key": "pc-super-secret", "index_name": "ai-agent"}
    )

    assert "pc-super-secret" not in repr(settings)
    assert "pc-super-secret" not in str(settings.redacted())


def test_a_namespace_is_derived_from_the_store_id() -> None:
    pinecone = PineconeSettings(api_key=SecretStr("k"), index_name="ai-agent")

    assert pinecone.namespace_for(50) == "store-50"


def test_a_namespace_template_without_the_store_id_is_rejected() -> None:
    """A namespace that ignores the retailer would let one store see another."""
    with pytest.raises(ValidationError):
        PineconeSettings(
            api_key=SecretStr("k"), index_name="ai-agent", namespace_template="products"
        )


def test_the_embedding_model_is_never_defaulted() -> None:
    """No model identifier belongs in the codebase (CLAUDE.md 31)."""
    assert build_settings().llm.embedding_model is None
