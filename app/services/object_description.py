"""Turning a picked object into words the product index can search.

The product index holds text: one embedded document per product, built from
its name, category, style and colour. A photo has no vector in that space, so
the picked object is described first - by a vision model looking at the same
two crops the customer's pick produced - and the description is what gets
embedded.

The model is an interpreter of pixels, nothing more. Its output is a query:
it is never shown as a product attribute, never becomes a filter, and cannot
change the category the detector assigned (CLAUDE.md 3.3).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypeVar

from pydantic import BaseModel

from app.core.exceptions import (
    LLMRequestError,
    LLMResponseInvalidError,
    LLMUnavailableError,
    VisualSearchUnavailableError,
)
from app.core.logging import get_logger
from app.prompts.furniture_finder.v1 import INSTRUCTIONS, VERSION, user_message
from app.schemas.furniture_finder import ObjectDescription

logger = get_logger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class VisionClient(Protocol):
    async def parse_images(
        self,
        *,
        instructions: str,
        user_input: str,
        images: Sequence[bytes],
        schema: type[SchemaT],
    ) -> SchemaT: ...


class ObjectDescriber:
    def __init__(self, client: VisionClient) -> None:
        self._client = client

    async def describe(self, *, category_words: str, views: Sequence[bytes]) -> ObjectDescription:
        """What the object looks like, or a controlled failure.

        Every provider outcome that is not a description - unreachable,
        refused, or answered in a shape that does not validate - is the same
        thing to the customer: the item could not be searched right now.
        """
        try:
            description = await self._client.parse_images(
                instructions=INSTRUCTIONS,
                user_input=user_message(category_words),
                images=views,
                schema=ObjectDescription,
            )
        except (LLMUnavailableError, LLMRequestError, LLMResponseInvalidError) as exc:
            logger.warning(
                "finder_description_failed",
                prompt_version=VERSION,
                error_type=type(exc).__name__,
            )
            raise VisualSearchUnavailableError(reason=type(exc).__name__) from exc
        logger.info(
            "finder_object_described",
            prompt_version=VERSION,
            style_count=len(description.styles),
            material_count=len(description.materials),
        )
        return description
