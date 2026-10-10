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
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

ENV_PREFIX = "ZORY_"

AZURE_KEY_VARIABLE = "AZURE_OPENAI_API_KEY"
AZURE_ENDPOINT_VARIABLE = "AZURE_OPENAI_ENDPOINT"


def azure_v1_base(endpoint: str) -> str:
    """The v1 API root of an Azure OpenAI resource, from any URL on it.

    Azure hands out per-operation URLs (`.../openai/v1/responses`,
    `.../openai/v1/images/generations`) or the bare resource. Every client
    needs the one root they share; anything after `/openai/v1` is dropped.
    """
    parts = urlsplit(endpoint.strip())
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError(f"{AZURE_ENDPOINT_VARIABLE} must be an https URL")
    path = parts.path
    marker = path.find("/openai/v1")
    root = path[:marker] if marker >= 0 else path.rstrip("/")
    return f"https://{parts.netloc}{root}/openai/v1/"


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

    base_url: str | None = Field(default=None, min_length=1)
    """An OpenAI-compatible endpoint to send every model call to - Azure
    OpenAI's v1 API (`https://<resource>.services.ai.azure.com/openai/v1/`).
    Absent means OpenAI itself. Model names are then the endpoint's deployment
    names. Filled from `AZURE_OPENAI_ENDPOINT` by `Settings`."""

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
    pair_tolerance_share: Decimal = Field(default=Decimal("0.05"), ge=0, lt=Decimal("0.5"))
    """How close each side of a piece must be to a pair they give - "a bed
    160 x 200", "a desk 120 x 60" - read by side. Rugs, whose sizes are
    standard, are matched exactly."""
    size_by_side: bool = True
    """A size they ask for is read as the piece's longer or shorter floor side
    - the larger or smaller of its two floor measurements, whatever column a
    merchant put each in - so any store's column habits give the same answer.
    Off, each measurement reads the column reviewed for store 50, as before."""

    @model_validator(mode="after")
    def _default_within_maximum(self) -> DiscoverySettings:
        if self.default_candidate_limit > self.max_candidate_limit:
            raise ValueError("discovery default_candidate_limit cannot exceed max_candidate_limit")
        return self


class SizeSettings(BaseModel):
    """How product sizes are read when no convention is known (the shape rule).

    A merchant may store a sofa's width in `length` or in `width`, so sizes are
    read convention-free: the longer floor side and the shorter. For a kind the
    store's own data shows to be long and shallow - one floor side at least
    `elongation_ratio` times the other in `long_and_shallow_share` of the
    pieces measured, over at least `min_measured` of them - the longer side is
    the width and the shorter the depth, in any store
    (docs/designer-led-shopping-plan.md, 4.2a).
    """

    elongation_ratio: Decimal = Field(default=Decimal("1.3"), gt=1)
    long_and_shallow_share: Decimal = Field(default=Decimal("0.95"), gt=0, le=1)
    min_measured: int = Field(default=8, ge=1)
    min_side_cm: Decimal = Field(default=Decimal("5"), gt=0)
    max_side_cm: Decimal = Field(default=Decimal("1200"), gt=0)
    """A floor side outside this range is a data slip - a value in metres
    recorded as centimetres, a height in a floor column - and is not read."""
    near_share: Decimal = Field(default=Decimal("0.25"), gt=0)
    close_share: Decimal = Field(default=Decimal("0.5"), gt=0)
    """A size within `near_share` of the one asked for ranks first, within
    `close_share` next - bands, so similarity still orders inside each."""
    similar_low: Decimal = Field(default=Decimal("0.85"), gt=0)
    similar_high: Decimal = Field(default=Decimal("1.15"), gt=0)
    """A size proportion between these is "similar" to the pick in words;
    below is "smaller", above "larger"."""
    fit_share: Decimal = Field(default=Decimal("0.03"), gt=0, lt=1)
    """A piece that goes in the pick - a mattress in a bed - is in the pick's
    size when its shorter floor side is within this share of the size the
    designer read off the pick: 180 matches 183, never 160."""


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

    Renders are not stored anywhere. The picture travels in the reply as a
    data URL and lives only on the customer's screen, like the rest of the
    conversation they are looking at.
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

    jpeg_quality: int = Field(default=88, ge=50, le=95)
    """Used only when a model answers in another format: the picture travels
    inside the reply, so a multi-megabyte PNG is re-encoded rather than sent."""

    # ── the customer's own room photo ───────────────────────────────────────

    room_check_model: str | None = Field(default=None, min_length=1)
    """A vision model that says whether an upload is a photo of a room, before
    anything is emptied or paid for. None turns room photos off: the uploads
    are refused as not configured."""
    room_check_reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None
    """Set for a reasoning model, unset for one that rejects it."""
    room_check_timeout_s: float = Field(default=30.0, gt=0)

    empty_room_primary: Literal["openai", "gemini"] = "gemini"
    """Which image model empties a room photo; the other, when configured, is
    tried once if it fails. Gemini by default: it keeps the customer's walls,
    floor and windows closest to the photo."""
    photo_edit_temperature: float | None = Field(default=0.15, ge=0, le=2)
    """Gemini's temperature when it edits the customer's photo. Low, because
    the room must come back as itself, not a reinterpretation of it."""

    room_photo_max_bytes: int = Field(default=15 * 1024 * 1024, gt=0)
    room_photo_min_side: int = Field(default=480, gt=0)
    room_photo_max_side: int = Field(default=2048, gt=0)
    """Longer sides are scaled down before anything is sent: the image models
    work at about 2K, so more pixels would only cost upload time."""

    @property
    def room_photos_enabled(self) -> bool:
        return self.room_check_model is not None

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

    comparison_max_products: int = Field(default=10, ge=2, le=20)
    """How many products one comparison may cover.

    The schema's own ceiling is twenty (`MAX_COMPARED_PRODUCTS`); this may
    choose less. Customers compare as many as they like by product decision;
    the default matches the picks tray, so "compare my picks" always fits.
    Beyond it the customer is asked to narrow the set rather than having the
    extras silently dropped, and typed and tapped comparisons share the limit.
    """

    arabic_replies: bool = False
    """Answer a customer who writes Arabic in Arabic - the writer's reply, the
    decision's own question and the fixed sentences - while everything the
    application reasons with stays English (docs/arabic-replies-plan.md).
    Off means English replies, exactly as before. Defaulted on in local and
    stage by `Settings`; an explicit value always wins."""

    cross_sell_shows_products: bool = True
    """After a pick, show products that go with it at once - one kind, chosen
    by the design specialist from what the store stocks and ranked by the
    colours and styles the customer has expressed. Off, a pick is offered the
    kinds that go with it as chips and nothing is searched until they tap
    one, exactly as before (CLAUDE.md 10.4)."""

    designer_led_opening: bool = True
    """Open a new product search with two soft questions the reply writer
    chooses from those still open, instead of the card of every question
    (docs/designer-led-shopping-plan.md, phase 1). Off brings the card back,
    exactly as it was - a way back, not a second experience."""

    designer_led_brief: bool = True
    """Beside every list of results, what the search is using as removable
    chips, and Narrow down opened pre-filled with every option
    (docs/designer-led-shopping-plan.md, phase 2). Off brings back the folded
    card offered once per family, exactly as it was."""

    designer_direction: bool = True
    """After a pick, the designer names one direction for the suggested kind -
    colours and styles the store stocks for it, what to avoid, a size
    proportion - in the room they named; the cards are ordered by it and the
    reply explains its best one or two (docs/designer-led-shopping-plan.md,
    phase 4). Off, a suggestion leans only on the customer's taste and the
    pick's style, exactly as before."""

    designer_taste: bool = True
    """Taste after products: what they liked, picked or asked more like of
    leans later searches quietly, and a soft taste question - which of two
    feels more like them, which of the store's styles, anything to avoid - is
    asked only about what nothing has told us yet, in place of the old
    colour-or-style follow-up (docs/designer-led-shopping-plan.md, phase 5).
    Off is the behaviour before it, exactly."""

    designer_room_handoff: bool = True
    """A room starts from what shopping learned: the head count given for
    seating, the colours and styles they said, the wall they gave - so only
    what is new, such as the budget, is asked (docs/designer-led-shopping-
    plan.md, phase 6). Off, a room asks everything as before."""
    designer_fit: bool = True
    """Will it fit: the pieces in view are checked in code against every wall
    and doorway the customer gave - a wall against the piece's longer side, a
    doorway against the smaller of its depth and height - and the reply answers
    "will it fit?" from that, never from a guess (docs/designer-led-shopping-
    plan.md, phase 7). Off, no check is made."""
    designer_space_fit: bool = True
    """When they say how wide the wall or spot is, the designer decides what
    width suits it - a sofa at about two-thirds of its wall - and the cards are
    ordered by closeness to that width; everything that fits stays in view.
    Off, the space only limits the width, as before."""

    designer_space_question: bool = False
    """Whether customers are asked how wide the space is: "How wide is the spot
    where it will go?" after products (CLAUDE.md 10.9), and "How wide a space?"
    on the cards - the opening and Narrow down (10.5, 10.6). Off by default:
    neither is asked, and the next taste question is asked instead. A space
    they give in words ("my wall is 300 cm") is still used exactly as before."""

    designer_led_buttons: bool = True
    """Three buttons on every card: Select as before, ♡ Like - a silent liked
    list, kept apart from the picks - and More like this, a similarity search
    from any card (docs/designer-led-shopping-plan.md, phase 3). Off, a like
    or More like this is refused and no liked list is reported."""

    fit_after_pick: bool = True
    """A pick checked against the room (reviewed: a bed) asks the room's size
    once and has the designer judge the space around it; a piece that goes in
    the pick - a mattress for a bed - is shown in the size the designer reads
    off the pick, first; and a fit between two pieces never asks for the room.
    Off, a pick asks nothing and "will it fit?" asks the room as before."""

    mixed_types: bool = True
    """A customer's search for a type also shows the types reviewed to stand
    beside it (`seating_v1.yaml`, `shown_with`) - sofa sets and sectional
    sofas mixed into a page of sofas - and the reply says plainly when the
    type they asked for has fewer matches than the page; "just sofas" keeps
    to the one type, and so does a size, which those types' listings cannot
    answer. Off, a sofa search shows sofas only, exactly as before."""

    max_picks: int = Field(default=10, ge=2, le=50)
    """How many products the picks tray keeps. A shortlist, not a second
    catalogue: past this a tick asks them to remove one first."""

    max_likes: int = Field(default=20, ge=2, le=50)
    """How many products the liked list keeps. Past this the oldest like is
    let go: a like is a taste signal, not a commitment to keep."""

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
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None
    """The specialist's own effort, for a model that accepts one. Unset, it
    spends what `llm.reasoning_effort` does. Kept apart because a design
    direction is a short structured answer, and every customer waits on it
    after a selection."""


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
        # A validation error never echoes its input: the input holds every
        # credential, and a startup error is printed and logged (CLAUDE.md 20.5).
        hide_input_in_errors=True,
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
    size: SizeSettings = SizeSettings()
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

    azure_openai_api_key: SecretStr | None = Field(
        default=None, validation_alias=AZURE_KEY_VARIABLE
    )
    azure_openai_endpoint: str | None = Field(
        default=None, validation_alias=AZURE_ENDPOINT_VARIABLE
    )
    """Azure OpenAI, under the names Azure's own tooling uses. Both set: every
    model call goes to Azure (`_azure_openai`). Neither: OpenAI, as before."""

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

    @field_validator("azure_openai_api_key", "azure_openai_endpoint", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: Any) -> Any:
        """`AZURE_OPENAI_ENDPOINT=` as `.env.example` ships it means "not set",
        not "set to nothing"."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="before")
    @classmethod
    def _azure_openai(cls, data: Any) -> Any:
        """Azure's key and endpoint become the LLM's, when both are given.

        Done before validation so `llm.api_key` need not also be set. An
        explicit `llm.base_url` wins: it is the more specific instruction.
        Raises nothing here - an error raised before validation would echo
        the raw input, secrets included; `_azure_openai_complete` reports a
        half-configured pair afterwards.
        """
        if not isinstance(data, dict):
            return data
        values = {str(k).lower(): v for k, v in data.items()}
        key = values.get(AZURE_KEY_VARIABLE.lower()) or values.get("azure_openai_api_key")
        endpoint = values.get(AZURE_ENDPOINT_VARIABLE.lower()) or values.get(
            "azure_openai_endpoint"
        )
        if not key or not endpoint:
            return data
        llm = data.get("llm")
        llm = dict(llm) if isinstance(llm, dict) else {}
        if llm.get("base_url"):
            return data
        try:
            base = azure_v1_base(str(endpoint))
        except ValueError:
            return data  # reported by `_azure_openai_complete`, without the value
        llm["api_key"] = key
        llm["base_url"] = base
        return {**data, "llm": llm}

    @model_validator(mode="after")
    def _azure_openai_complete(self) -> Settings:
        """Half an Azure configuration is a mistake, not a choice: fail at
        startup by variable name, never by value."""
        given = (self.azure_openai_api_key is not None, self.azure_openai_endpoint is not None)
        if any(given) and not all(given):
            raise ValueError(
                f"set both {AZURE_KEY_VARIABLE} and {AZURE_ENDPOINT_VARIABLE}, or neither"
            )
        if self.azure_openai_endpoint is not None:
            azure_v1_base(self.azure_openai_endpoint)
        return self

    @model_validator(mode="after")
    def _target_fits_discovery_limit(self) -> Settings:
        """A target discovery could never return would relax forever."""
        if self.relaxation.target_candidates > self.discovery.max_candidate_limit:
            raise ValueError(
                "relaxation target_candidates cannot exceed discovery max_candidate_limit"
            )
        return self

    @model_validator(mode="after")
    def _arabic_replies_by_environment(self) -> Settings:
        """Arabic replies are on by default in local and stage, off in test and
        prod until reviewed there; an explicit setting always wins."""
        if (
            self.environment in (Environment.LOCAL, Environment.STAGE)
            and "arabic_replies" not in self.customer_agent.model_fields_set
        ):
            self.customer_agent = self.customer_agent.model_copy(update={"arabic_replies": True})
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
            # The host only: which provider every model call goes to.
            "llm_endpoint": urlsplit(self.llm.base_url).netloc if self.llm.base_url else "openai",
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
