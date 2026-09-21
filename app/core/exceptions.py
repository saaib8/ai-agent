"""Domain exception hierarchy and its safe public projection.

Two rules hold everywhere in the service (CLAUDE.md 20.5, 21):

* ``public_message`` is the ONLY text that may reach a caller. It is written by
  us, never derived from an exception's ``str()``, a driver message or a
  traceback.
* internal detail travels in ``context`` and is logged against the request's
  trace id, never serialised into a response.

This deliberately does not mirror the existing Django ``core/exception.py``
decorator, which returns ``str(e)`` to the client.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any


class ZoryError(Exception):
    """Base class for every error this service raises deliberately."""

    code: str = "internal_error"
    http_status: int = HTTPStatus.INTERNAL_SERVER_ERROR
    public_message: str = "Something went wrong. Please try again."

    def __init__(
        self,
        *,
        public_message: str | None = None,
        **context: Any,
    ) -> None:
        if public_message is not None:
            self.public_message = public_message
        self.context: dict[str, Any] = context
        # The exception's own str() is for logs only.
        super().__init__(self.public_message)


# ── Request / context errors ────────────────────────────────────────────────


class InvalidRequestError(ZoryError):
    code = "invalid_request"
    http_status = HTTPStatus.UNPROCESSABLE_ENTITY
    public_message = "The request could not be processed as written."


class StoreContextMissingError(ZoryError):
    code = "store_context_missing"
    http_status = HTTPStatus.BAD_REQUEST
    public_message = "A store must be identified before this request can be handled."


class StoreNotFoundError(ZoryError):
    code = "store_not_found"
    http_status = HTTPStatus.NOT_FOUND
    public_message = "That store is not available."


class ProductNotFoundError(ZoryError):
    code = "product_not_found"
    http_status = HTTPStatus.NOT_FOUND
    public_message = "That product is not available."


# ── Integration errors ──────────────────────────────────────────────────────


class IntegrationUnavailableError(ZoryError):
    """A dependency this service does not own is unreachable or failing."""

    code = "dependency_unavailable"
    http_status = HTTPStatus.SERVICE_UNAVAILABLE
    public_message = "The service is temporarily unavailable. Please try again shortly."


class CatalogUnavailableError(IntegrationUnavailableError):
    code = "catalog_unavailable"


class SessionStoreUnavailableError(IntegrationUnavailableError):
    code = "session_store_unavailable"


class EmbeddingUnavailableError(IntegrationUnavailableError):
    """The query could not be embedded. Ranking is skipped, discovery is not.

    Semantic ranking is an enhancement, so this never reaches a customer as a
    failure: the caller returns eligible products in deterministic order.
    """

    code = "embedding_unavailable"


class SemanticIndexUnavailableError(IntegrationUnavailableError):
    """The semantic index could not be reached or answered. Same treatment."""

    code = "semantic_index_unavailable"


class LLMUnavailableError(IntegrationUnavailableError):
    """The model provider was unreachable, timed out, or refused the call."""

    code = "llm_unavailable"


class LLMRequestError(ZoryError):
    """The provider refused our request as invalid.

    An unsupported parameter, a rejected schema, a bad credential: the provider
    is healthy and answering, and what it rejected is ours to fix. Retrying
    cannot help, so this is a server fault (500) and never reported as a
    temporary outage - a persistent misconfiguration must not masquerade as a
    transient one.
    """

    code = "llm_request_error"
    http_status = HTTPStatus.INTERNAL_SERVER_ERROR
    public_message = "We could not complete that request."


class LLMResponseInvalidError(ZoryError):
    """The provider answered, but not with output we can use.

    Model output is untrusted input. A response that does not satisfy the
    structured schema is a failure, never something to repair by guessing.
    """

    code = "llm_response_invalid"
    http_status = HTTPStatus.BAD_GATEWAY
    public_message = "We could not interpret that request. Please rephrase it."


# ── Taxonomy errors ─────────────────────────────────────────────────────────


class TaxonomyValidationError(ZoryError):
    """A commerce category/subcategory outside the approved taxonomy.

    Raised by deterministic validation, including on values an LLM proposed.
    An unapproved value must never reach SQL or be persisted (CLAUDE.md 14.3).
    """

    code = "taxonomy_validation_failed"
    http_status = HTTPStatus.UNPROCESSABLE_ENTITY
    public_message = "That product type is not one we carry."


class UnknownCommerceCategoryError(TaxonomyValidationError):
    code = "unknown_commerce_category"


class UnknownCommerceSubcategoryError(TaxonomyValidationError):
    code = "unknown_commerce_subcategory"


class UnsupportedDimensionRoleError(TaxonomyValidationError):
    """A dimension role the registry does not support for this product type.

    Raised if one reaches discovery. Query understanding should have surfaced it
    as an unsupported requirement long before, so this is defence in depth.
    """

    code = "unsupported_dimension_role"
    public_message = "We cannot search that measurement for this kind of product."


class UnknownCatalogAttributeError(TaxonomyValidationError):
    """A colour or style outside the approved controlled vocabulary."""

    code = "unknown_catalog_attribute"


class DimensionSemanticsMissingError(ZoryError):
    """A request dimension has no recorded strength.

    Unreachable through the query-understanding path: the outcome contracts
    validate that every request dimension has exactly one semantics entry. It
    exists so a caller asking for a strength that was never recorded fails
    loudly instead of receiving a silent default, because a missing strength
    read as consent is exactly how a locked bound gets widened.
    """

    code = "dimension_semantics_missing"


class RankingIntegrityError(ZoryError):
    """Ranking returned a different population from the one it was given.

    M9's contract is that it decides ORDER and never eligibility: the candidate
    list it receives is the list it returns, reordered. If that stops holding,
    the customer is being shown a set nothing established - a product ranking
    invented, or one the catalog qualified silently lost.

    An internal defect, not a customer-visible condition and not a degradation:
    every ranking path, including every fallback, is required to carry all
    candidates through. Raised rather than repaired, because a pipeline that
    quietly restored the missing products would hide the defect and answer with
    an order nothing produced.
    """

    code = "ranking_integrity_error"


# ── Startup / programming errors ────────────────────────────────────────────


class BundlePresentationError(ZoryError):
    """A committed room could not be rendered from the outcome that made it.

    An invariant violation rather than a shortfall: every committed line came
    from that same outcome, so a card with no verified facts behind it means
    the two describe different rooms. Surfaced rather than papered over,
    because rendering a room with a piece missing would be quietly wrong.
    """


class ConfigurationError(ZoryError):
    """Raised at startup when the service is misconfigured.

    These abort the process rather than answering a request, so ``str()``
    carries the diagnostic ``detail`` - an operator reading the crash needs to
    know which file or column was wrong. That leaks nothing: responses are
    always built from ``public_message``, never from ``str(exc)``.
    """

    code = "configuration_error"
    http_status = HTTPStatus.INTERNAL_SERVER_ERROR

    def __str__(self) -> str:
        detail = self.context.get("detail")
        return str(detail) if detail else self.public_message


class ResourceNotInitialisedError(ConfigurationError):
    """A lifespan-managed resource was used before startup completed."""

    code = "resource_not_initialised"


class TaxonomyConfigurationError(ConfigurationError):
    """The taxonomy file is missing or malformed. Fails startup."""

    code = "taxonomy_configuration_error"


class CatalogSchemaError(ConfigurationError):
    """The live catalog lacks a column this service requires. Fails startup."""

    code = "catalog_schema_error"
