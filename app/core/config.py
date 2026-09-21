"""Centralised, validated runtime configuration.

Every environment-dependent value in the service is declared here and nowhere
else. No module calls ``os.getenv`` outside this file, so there is exactly one
place to audit for defaults, secrets and tuning knobs (CLAUDE.md 3.1).

Settings are grouped into nested models and populated from ``ZORY_``-prefixed
environment variables using ``__`` as the nesting delimiter, e.g.::

    ZORY_DB__DSN=postgresql+asyncpg://user:pass@host/db
    ZORY_DB__POOL_SIZE=10

In ``stage``/``prod`` an additional source hydrates secret material from AWS
Secrets Manager, matching how the existing Django platform stores credentials.
Local and test runs never import boto3.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

ENV_PREFIX = "ZORY_"


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGE = "stage"
    PROD = "prod"

    @property
    def uses_secrets_manager(self) -> bool:
        return self in (Environment.STAGE, Environment.PROD)


class DatabaseSettings(BaseModel):
    """PostgreSQL connection and pool tuning.

    The DSN is required to name an async driver: a blocking driver on the async
    request path would stall the event loop under load (CLAUDE.md 24).
    """

    dsn: SecretStr
    pool_size: int = Field(default=10, ge=1, le=100)
    max_overflow: int = Field(default=5, ge=0, le=100)
    pool_timeout_s: float = Field(default=10.0, gt=0)
    pool_recycle_s: int = Field(default=1800, gt=0)
    pool_pre_ping: bool = True
    connect_timeout_s: int = Field(default=10, gt=0)
    command_timeout_s: float = Field(default=15.0, gt=0)
    echo_sql: bool = False
    # Opens every connection with ``default_transaction_read_only``. Django owns
    # the catalog schema and all writes to it, so the database itself refuses a
    # write from this service rather than relying on review (CLAUDE.md 4).
    read_only: bool = True

    @field_validator("dsn")
    @classmethod
    def _require_async_driver(cls, value: SecretStr) -> SecretStr:
        dsn = value.get_secret_value()
        if not dsn.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "database DSN must use the asyncpg driver "
                "(expected a 'postgresql+asyncpg://' prefix)"
            )
        return value


class RedisSettings(BaseModel):
    """Redis connection settings.

    Key prefixes and cache TTLs are added by the milestones that introduce
    session state and the capability cache; nothing reads them yet.
    """

    url: SecretStr
    connect_timeout_s: float = Field(default=2.0, gt=0)
    socket_timeout_s: float = Field(default=2.0, gt=0)
    max_connections: int = Field(default=20, ge=1)

    @field_validator("url")
    @classmethod
    def _require_redis_scheme(cls, value: SecretStr) -> SecretStr:
        url = value.get_secret_value()
        if not url.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError("redis URL must start with redis://, rediss:// or unix://")
        return value


class ApiSettings(BaseModel):
    prefix: str = "/v1"
    cors_origins: tuple[str, ...] = ()
    enable_docs: bool = False

    @field_validator("prefix")
    @classmethod
    def _leading_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("api prefix must start with '/'")
        return value.rstrip("/")

    @field_validator("cors_origins")
    @classmethod
    def _no_wildcard(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if "*" in value:
            raise ValueError("CORS origins must be an explicit allowlist, not '*'")
        return value


class LLMSettings(BaseModel):
    """Language-model provider configuration.

    The model name is required rather than defaulted, so no model identifier is
    ever baked into the codebase (CLAUDE.md 31).
    """

    api_key: SecretStr
    model: str = Field(min_length=1)
    timeout_s: float = Field(default=20.0, gt=0)
    max_retries: int = Field(default=2, ge=0)
    # Both of the following are sent only when set. Which one a model accepts
    # depends on the model: reasoning models reject `temperature` outright,
    # and non-reasoning models have no reasoning effort to spend. Leaving the
    # inapplicable one unset is how a model is configured, not a code branch.
    #
    # Structured extraction wants the same answer every time, not variety, so
    # set this to 0 on a model that supports it.
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None

    # Same provider and same key as the chat model, but a different model and a
    # different job. Left unset until semantic ranking is configured; a service
    # that needs it and finds it absent must say so rather than default to one,
    # because no model identifier belongs in the codebase (CLAUDE.md 31).
    embedding_model: str | None = Field(default=None, min_length=1)


class DiscoverySettings(BaseModel):
    """Bounds on structured product retrieval.

    The candidate pool feeds later ranking; it is never a bulk export. Both
    bounds are configuration rather than constants scattered through the code.
    """

    default_candidate_limit: int = Field(default=50, ge=1)
    max_candidate_limit: int = Field(default=200, ge=1)

    @model_validator(mode="after")
    def _default_within_maximum(self) -> DiscoverySettings:
        if self.default_candidate_limit > self.max_candidate_limit:
            raise ValueError(
                "discovery default_candidate_limit cannot exceed max_candidate_limit"
            )
        return self


class PineconeSettings(BaseModel):
    """The text semantic index. Absent when semantic ranking is not configured.

    Deliberately small: only what M9 needs to reach one index and scope a query
    to one retailer. The index's own dimension and metric are read from the
    live index rather than restated here, so there is no second copy of a
    number that could disagree with the index itself.
    """

    api_key: SecretStr
    index_name: str = Field(min_length=1)

    namespace_template: str = "store-{store_id}"
    """Namespace scope, derived by application code from RetailerContext.

    A template rather than a literal so the convention is configuration. It is
    never built from a customer message, a model's output or a query parameter
    (CLAUDE.md 20.2).
    """

    @field_validator("namespace_template")
    @classmethod
    def _requires_store_id(cls, value: str) -> str:
        if "{store_id}" not in value:
            raise ValueError("pinecone namespace_template must contain '{store_id}'")
        return value

    def namespace_for(self, store_id: int) -> str:
        return self.namespace_template.format(store_id=store_id)


class RelaxationSettings(BaseModel):
    """Policy for broadening a search that returned too little.

    Product policy, not code: every bound below is configuration, and no
    service carries a literal copy of it.
    """

    target_candidates: int = Field(default=5, ge=1)
    """Unique candidates that count as enough. At or above this, stop."""

    # Fractions of the ORIGINAL bound, never compounded. Capped at 20%: beyond
    # that the result stops resembling what the customer asked for.
    price_steps: tuple[Decimal, ...] = (Decimal("0.10"), Decimal("0.20"))

    seating_delta: int = Field(default=1, ge=1, le=1)
    """Seats of flexibility. V1 allows exactly one."""

    # Fractions of the ORIGINAL measurement, never compounded. Capped at 10%,
    # which is where the store-50 evidence stopped showing smooth growth; which
    # measurements may use these at all is a separate allowlist
    # (app/services/dimension_policy.py).
    dimension_steps: tuple[Decimal, ...] = (Decimal("0.05"), Decimal("0.10"))

    @model_validator(mode="after")
    def _check_price_steps(self) -> RelaxationSettings:
        if not self.price_steps:
            raise ValueError("relaxation price_steps must not be empty")
        if any(step <= 0 for step in self.price_steps):
            raise ValueError("relaxation price_steps must be positive")
        if list(self.price_steps) != sorted(set(self.price_steps)):
            raise ValueError("relaxation price_steps must be strictly increasing")
        if max(self.price_steps) > Decimal("0.20"):
            raise ValueError("relaxation price_steps may not exceed 0.20")
        return self

    @model_validator(mode="after")
    def _check_dimension_steps(self) -> RelaxationSettings:
        if not self.dimension_steps:
            raise ValueError("relaxation dimension_steps must not be empty")
        if any(step <= 0 for step in self.dimension_steps):
            raise ValueError("relaxation dimension_steps must be positive")
        if list(self.dimension_steps) != sorted(set(self.dimension_steps)):
            raise ValueError("relaxation dimension_steps must be strictly increasing")
        if max(self.dimension_steps) > Decimal("0.10"):
            raise ValueError("relaxation dimension_steps may not exceed 0.10")
        return self


class CustomerAgentSettings(BaseModel):
    """Runtime behaviour of the customer-facing commerce capabilities.

    Deliberately small for now: a field arrives when the capability that reads
    it does. The presentation limit and the decision/response model
    configuration belong to the phases that introduce their consumers.
    """

    comparison_max_products: int = Field(default=3, ge=2, le=4)
    """How many products one comparison may cover.

    The schema's own ceiling is four; this may choose a smaller one. Beyond it
    a comparison stops being a comparison, and the customer is asked to narrow
    it rather than having the extras silently dropped.
    """

    decision_model: str | None = Field(default=None, min_length=1)
    """The model that decides what a customer turn should do.

    Absent means the Customer Agent's decision step is not configured, the
    same way an absent `embedding_model` means semantic ranking is not.

    Deliberately **not** defaulted to `llm.model`. Interpreting one message
    into a search and deciding a whole conversational turn are different jobs
    with different prompts, and silently sharing one identifier would make
    changing either of them change both.
    """

    response_model: str | None = Field(default=None, min_length=1)
    """The model that words the customer's reply.

    Absent means the response capability is not configured. Deliberately not
    defaulted to `decision_model` or `llm.model`: deciding a turn, interpreting
    a message and writing a reply are three jobs with three prompts, and
    sharing one identifier would make changing any of them change the others.
    """

    presentation_limit: int | None = Field(default=None, ge=1)
    """How many products one turn shows, applied only after ranking.

    Absent means the search pipeline is not configured - the same way an
    absent `embedding_model` means semantic ranking is not. It is **not** a
    product default: no number has been approved, and one invented here would
    quietly decide how many options a customer gets to see.

    Deliberately unrelated to `discovery.max_candidate_limit`. The pool this
    selects from is unbounded, so that ceiling bounds nothing here.
    """


class ObservabilitySettings(BaseModel):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    trace_header: str = "X-Request-ID"


class AwsSettings(BaseModel):
    """Where stage/prod secret material is read from."""

    region: str = "me-south-1"
    secret_name: str = ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="forbid",
    )

    environment: Environment = Environment.LOCAL
    service_name: str = "zory-agent"

    db: DatabaseSettings
    redis: RedisSettings
    api: ApiSettings = ApiSettings()
    llm: LLMSettings
    customer_agent: CustomerAgentSettings = CustomerAgentSettings()
    discovery: DiscoverySettings = DiscoverySettings()
    relaxation: RelaxationSettings = RelaxationSettings()
    # Optional on purpose: semantic ranking is an enhancement, so the service
    # starts and serves deterministic results with no Pinecone configured at
    # all. Absence is the "semantic ranking off" state, not a startup failure.
    pinecone: PineconeSettings | None = None
    observability: ObservabilitySettings = ObservabilitySettings()
    aws: AwsSettings = AwsSettings()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Environment variables outrank Secrets Manager so an operator can
        # break-glass override a single value without editing the secret.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            AwsSecretsManagerSource(settings_cls),
            file_secret_settings,
        )

    @model_validator(mode="after")
    def _target_fits_discovery_limit(self) -> Settings:
        """A target discovery could never return would relax forever."""
        if self.relaxation.target_candidates > self.discovery.max_candidate_limit:
            raise ValueError(
                "relaxation target_candidates cannot exceed "
                "discovery max_candidate_limit"
            )
        return self

    def redacted(self) -> dict[str, Any]:
        """A summary safe to emit at startup: no secret ever renders (22)."""
        return {
            "environment": str(self.environment),
            "semantic_ranking_configured": self.pinecone is not None,
            "pinecone_index": self.pinecone.index_name if self.pinecone else None,
            "embedding_model": self.llm.embedding_model,
            "service_name": self.service_name,
            "api_prefix": self.api.prefix,
            "docs_enabled": self.api.enable_docs,
            "log_level": self.observability.log_level,
            "log_format": self.observability.log_format,
            "db_pool_size": self.db.pool_size,
            "db_max_overflow": self.db.max_overflow,
            "db_read_only": self.db.read_only,
            "discovery_default_limit": self.discovery.default_candidate_limit,
            "discovery_max_limit": self.discovery.max_candidate_limit,
            "llm_model": self.llm.model,
            "relaxation_target_candidates": self.relaxation.target_candidates,
            "comparison_max_products": self.customer_agent.comparison_max_products,
            "presentation_configured": self.customer_agent.presentation_limit
            is not None,
            # Booleans, never the identifiers: an operator needs to know
            # whether a capability is configured, not which model serves it.
            "decision_configured": self.customer_agent.decision_model is not None,
            "response_configured": self.customer_agent.response_model is not None,
        }


class AwsSecretsManagerSource(PydanticBaseSettingsSource):
    """Hydrate secret material from AWS Secrets Manager in stage/prod.

    The secret holds JSON shaped like the settings tree, e.g.
    ``{"db": {"dsn": "..."}, "redis": {"url": "..."}}``. Returns an empty
    mapping (and imports nothing) in every other environment, so local and test
    runs have no AWS dependency at all.
    """

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        # Unused: this source supplies the whole mapping via __call__.
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        raw_env = os.environ.get(f"{ENV_PREFIX}ENVIRONMENT", Environment.LOCAL.value)
        try:
            environment = Environment(raw_env.strip().lower())
        except ValueError:
            return {}
        if not environment.uses_secrets_manager:
            return {}

        secret_name = os.environ.get(f"{ENV_PREFIX}AWS__SECRET_NAME", "")
        if not secret_name:
            return {}
        region = os.environ.get(f"{ENV_PREFIX}AWS__REGION", "me-south-1")

        import boto3  # type: ignore[import-untyped]  # lazy: never loaded outside stage/prod

        client = boto3.client("secretsmanager", region_name=region)
        payload = client.get_secret_value(SecretId=secret_name)["SecretString"]
        parsed: dict[str, Any] = json.loads(payload)
        return parsed


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings singleton."""
    return Settings()
