"""Where finished renders are kept, and the address they are served from.

A render is written once, under an application-built key, and never changed.
The key holds no session id and no customer text: it is served publicly, so it
names a store and a date and is otherwise random.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from app.core.config import VisualizationSettings
from app.core.exceptions import ConfigurationError, RenderUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)


class RenderStore(Protocol):
    async def save(self, key: str, data: bytes, mime: str) -> str:
        """Store the image and return the public URL it is served from."""
        ...


class S3RenderStore:
    """Adapter over boto3. One client per process; boto3 is imported only here."""

    def __init__(self, settings: VisualizationSettings, *, client: Any = None) -> None:
        """`client` exists for tests; production builds one from the AWS chain."""
        if client is None:
            try:
                import boto3  # type: ignore[import-untyped]
            except ImportError as exc:
                raise ConfigurationError(
                    detail=(
                        "visualization is configured but the 'aws' extra (boto3) is not installed"
                    ),
                    public_message="Room visualisation is not installed.",
                ) from exc
            client = boto3.client("s3", region_name=settings.store_region)
        self._client = client
        self._bucket = settings.store_bucket
        self._prefix = settings.store_prefix.strip("/")
        self._base = settings.public_base_url.rstrip("/")

    async def save(self, key: str, data: bytes, mime: str) -> str:
        full_key = f"{self._prefix}/{key}"
        try:
            await asyncio.to_thread(
                self._client.put_object,
                Bucket=self._bucket,
                Key=full_key,
                Body=data,
                ContentType=mime,
                # Written once under a random key, so it can be cached forever.
                CacheControl="public, max-age=31536000, immutable",
            )
        except Exception as exc:
            logger.warning("render_store_failed", error_type=type(exc).__name__)
            raise RenderUnavailableError(reason="storage", error_type=type(exc).__name__) from exc
        return f"{self._base}/{full_key}"
