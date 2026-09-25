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


class SessionSettings(BaseModel):
    """Short-term runtime session lifetime and size.

    Redis expiry here is a **technical** session lifetime and nothing else. It
    is not customer memory, not a billing period and not a commercial
    engagement window; conflating them would let an infrastructure timeout
    redefine a product concept (CLAUDE.md 19).
    """

    ttl_s: int = Field(default=3600, ge=60, le=86_400)
    """How long an idle session survives. Refreshed on every successful save,
    so an active conversation does not expire mid-way through."""

    max_history_messages: int = Field(default=20, ge=2, le=200)
    """How many prior messages travel into the next turn.

    Counted in messages rather than turns, and even by default so a bound can
    always be met by whole user/assistant pairs. No summarisation in V1: a
    model asked to compress history is a third reasoning step nobody approved,
    and it would put invented wording into the context of every later turn.
    """

    operation_timeout_s: float = Field(default=2.0, gt=0, le=30)
    """Ceiling on one session read or write. Distinct from the connection
    timeouts on `RedisSettings`: those bound reaching Redis at all, this bounds
    a single session operation once connected."""


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
            raise ValueError("discovery default_candidate_limit cannot exceed max_candidate_limit")
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


class FurnitureFinderSettings(BaseModel):
    """Photo-based product finding. Absent when the finder is not configured.

    Three steps, each served by something this service does not own: the
    object detector deployed on Modal outlines what is in a photo; a vision
    model describes the picked object in words; and the description is
    searched in a product index built from **text** documents about each
    product. The index is text, so the query must be embedded by the model
    and at the width the index was built with - a vector from anything else
    is in a different space, and its nearest neighbours would be noise that
    still looks like an answer.

    The vision and embedding models use the language-model provider's key
    (`llm.api_key`): same provider, different jobs.
    """

    detector_url: str = Field(min_length=1)
    """The synchronous Modal detection endpoint."""

    detector_key: SecretStr
    detector_secret: SecretStr
    detector_timeout_s: float = Field(default=150.0, gt=0)
    """Modal caps a synchronous request at about 150 s; a cold start alone can
    take 20."""

    detector_confidence: float = Field(default=0.25, gt=0, lt=1)

    vision_model: str = Field(min_length=1)
    """Describes the picked object. No default: no model identifier belongs in
    the codebase (CLAUDE.md 31)."""

    vision_reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None
    """Set for a reasoning model, unset for one that rejects it. Kept apart
    from `llm.reasoning_effort`: describing a crop is a short job, and the
    customer is waiting on it."""

    vision_timeout_s: float = Field(default=30.0, gt=0)

    embedding_model: str = Field(min_length=1)
    """The model the product index was built with."""

    embedding_dimensions: int = Field(gt=0)
    """The width the product index was built at, requested from the model."""

    index_api_key: SecretStr
    index_name: str = Field(min_length=1)
    index_namespace: str = Field(min_length=1)
    """The product index and its namespace. Vectors carry `store_id`,
    `category` and `product_url` metadata; the category is the catalog's
    visual category, the same vocabulary the detector labels with."""

    result_limit: int = Field(default=10, gt=0, le=50)
    """How many products one pick presents."""

    candidate_limit: int = Field(default=40, gt=0, le=200)
    """How many neighbours are asked of the index before PostgreSQL decides
    which of them are still sellable here. Larger than `result_limit` so that
    stale or inactive vectors do not leave the customer with a short list."""

    max_upload_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    min_image_side: int = Field(default=300, gt=0)
    detection_max_side: int = Field(default=1600, gt=0)
    """Uploads are downscaled to this before detection, and every coordinate
    the detector returns is in that downscaled image's pixel space - which is
    the image kept for cropping, so the two can never disagree."""

    medium_crop_padding: float = Field(default=0.15, ge=0, le=1)
    """Context around the object in the second view the vision model sees, as
    a share of its longer side."""

    @model_validator(mode="after")
    def _candidates_cover_results(self) -> FurnitureFinderSettings:
        if self.candidate_limit < self.result_limit:
            raise ValueError("furniture_finder candidate_limit must be at least result_limit")
        return self


class VisualizationSettings(BaseModel):
    """Rendering a room package as an image. Absent when not configured.

    Two image models, each named here and nowhere in code (CLAUDE.md 31): the
    primary renders, and the other - when configured - is tried once if the
    primary fails. OpenAI uses the language-model provider's key
    (`llm.api_key`); Gemini has its own.

    Renders are stored in a bucket and served from `public_base_url`, which is
    a public address by deliberate choice: the link is what the customer
    downloads and shares.
    """

    primary: Literal["openai", "gemini"] = "openai"

    openai_model: str | None = Field(default=None, min_length=1)
    openai_quality: Literal["low", "medium", "high", "xhigh", "max", "auto"] = "medium"
    openai_size: str = Field(default="1536x1024", pattern=r"^\d{3,4}x\d{3,4}$")

    gemini_model: str | None = Field(default=None, min_length=1)
    gemini_api_key: SecretStr | None = None
    gemini_image_size: Literal["1K", "2K", "4K"] = "2K"
    gemini_aspect_ratio: str = Field(default="3:2", pattern=r"^\d{1,2}:\d{1,2}$")

    timeout_s: float = Field(default=180.0, gt=0)
    """One render. The slowest observed was ~50 s; this bounds a stuck call."""

    max_references: int = Field(default=14, ge=1, le=14)
    """Product photos sent with one render. 14 is Gemini's ceiling, and the
    same cap keeps the two providers' renders comparable."""

    reference_timeout_s: float = Field(default=15.0, gt=0)
    reference_max_bytes: int = Field(default=8 * 1024 * 1024, gt=0)

    store_bucket: str = Field(min_length=1)
    store_region: str = Field(min_length=1)
    store_prefix: str = Field(min_length=1)
    """Keeps stage and prod renders apart inside one bucket."""

    public_base_url: str = Field(min_length=1, pattern=r"^https://")
    """Where a stored render is served from, e.g. the bucket's CloudFront."""

    @model_validator(mode="after")
    def _providers_are_usable(self) -> VisualizationSettings:
        if self.gemini_model is not None and self.gemini_api_key is None:
            raise ValueError("visualization gemini_model requires gemini_api_key")
        configured = {
            "openai": self.openai_model is not None,
            "gemini": self.gemini_model is not None,
        }
        if not configured[self.primary]:
            raise ValueError(f"visualization primary provider '{self.primary}' is not configured")
        return self


class CatalogSettings(BaseModel):
    """Browsing the store's catalog and rendering a room from what was picked.

    Browsing reads only PostgreSQL, so it is always available. Rendering a
    selection also needs `visualization`, and refuses without it.
    """

    page_size: int = Field(default=24, ge=1)
    max_page_size: int = Field(default=60, ge=1)
    max_query_chars: int = Field(default=80, ge=1)
    """A name search is a few words; anything longer is not a search."""

    max_products: int = Field(default=14, ge=1)
    """Distinct products in one selection. Each needs a reference photo, so
    the effective cap is never above `visualization.max_references` (see
    `Settings.effective_catalog`)."""

    max_quantity: int = Field(default=10, ge=1)
    """Units of one product: eight dining chairs is a room, forty is a hall."""

    min_room_side_m: float = Field(default=1.5, gt=0)
    max_room_side_m: float = Field(default=20.0, gt=0)

    crowded_floor_ratio: float = Field(default=0.6, gt=0, le=1)
    """Share of the floor furniture may cover before the room reads as
    crowded. Advisory only: the customer is warned, never stopped."""

    @model_validator(mode="after")
    def _bounds_are_ordered(self) -> CatalogSettings:
        if self.page_size > self.max_page_size:
            raise ValueError("catalog page_size cannot exceed max_page_size")
        if self.min_room_side_m >= self.max_room_side_m:
            raise ValueError("catalog min_room_side_m must be below max_room_side_m")
        return self


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


class InteriorDesignSettings(BaseModel):
    """The interior-design specialist's own model.

    Its own settings block rather than another field on `CustomerAgentSettings`,
    because it is a different agent. Absent means the specialist is not
    configured, and nothing falls back to the query-understanding, decision or
    response model: four jobs, four prompts, four identifiers.
    """

    model: str | None = Field(default=None, min_length=1)


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
    session: SessionSettings = SessionSettings()
    llm: LLMSettings
    customer_agent: CustomerAgentSettings = CustomerAgentSettings()
    interior_design: InteriorDesignSettings = InteriorDesignSettings()
    discovery: DiscoverySettings = DiscoverySettings()
    relaxation: RelaxationSettings = RelaxationSettings()
    # Optional on purpose: semantic ranking is an enhancement, so the service
    # starts and serves deterministic results with no Pinecone configured at
    # all. Absence is the "semantic ranking off" state, not a startup failure.
    pinecone: PineconeSettings | None = None
    # Optional for the same reason: without it the finder routes refuse and
    # everything else is unaffected.
    furniture_finder: FurnitureFinderSettings | None = None
    # And again: without it the Visualize route refuses and nothing else changes.
    visualization: VisualizationSettings | None = None
    catalog: CatalogSettings = CatalogSettings()
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
                "relaxation target_candidates cannot exceed discovery max_candidate_limit"
            )
        return self

    def effective_catalog(self) -> CatalogSettings:
        """Catalog settings with the selection capped at what one render can
        take a photo of. A piece beyond it would be drawn from words alone,
        and look it - so the cap follows `visualization.max_references`
        rather than failing startup when only one of the two is lowered."""
        if self.visualization is None:
            return self.catalog
        cap = min(self.catalog.max_products, self.visualization.max_references)
        return self.catalog.model_copy(update={"max_products": cap})

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
            "presentation_configured": self.customer_agent.presentation_limit is not None,
            # Booleans, never the identifiers: an operator needs to know
            # whether a capability is configured, not which model serves it.
            "decision_configured": self.customer_agent.decision_model is not None,
            "response_configured": self.customer_agent.response_model is not None,
            "interior_design_configured": self.interior_design.model is not None,
            "furniture_finder_configured": self.furniture_finder is not None,
            "furniture_finder_index": (
                self.furniture_finder.index_name if self.furniture_finder else None
            ),
            "visualization_configured": self.visualization is not None,
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
