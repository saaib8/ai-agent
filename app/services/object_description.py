"""The vision-model boundary a service reads images through.

Furniture Finder no longer describes a picked object in words - the main
platform's finder compares the object's own pixels - so what remains here is
the interface room photos check a customer's picture through.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypeVar

from pydantic import BaseModel

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
