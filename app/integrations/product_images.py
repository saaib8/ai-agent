"""Fetching a catalog product's photo to show an image model.

The URL comes from the catalog, which the retailer writes - so it is data from
outside this service, and this module is where it becomes a request. Three
rules make that safe:

* **https only, public hosts only.** Every address the name resolves to must
  be public; loopback, private, link-local and reserved ranges are refused, so
  a crafted image URL cannot make this service call its own network.
* **Redirects are followed by hand**, a small number of times, and each hop is
  checked the same way - a public URL that redirects inward is refused too.
* **Bounded.** A size cap while reading, a timeout, and the bytes must decode
  as an image before anything else sees them.

A photo that cannot be fetched is not an error: the piece is rendered from its
description instead, which the prompt says.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from io import BytesIO
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

from app.core.logging import get_logger

logger = get_logger(__name__)

_MAX_REDIRECTS = 2
_MAX_SIDE = 1024
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class ProductImageFetcher:
    def __init__(
        self,
        *,
        timeout_s: float,
        max_bytes: int,
        transport: httpx.AsyncBaseTransport | None = None,
        resolve_public: bool = True,
    ) -> None:
        """`transport` and `resolve_public` exist for tests."""
        self._max_bytes = max_bytes
        self._resolve_public = resolve_public
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout_s,
            follow_redirects=False,
            headers={"User-Agent": _USER_AGENT, "Accept": "image/*"},
        )

    async def fetch(self, url: str) -> bytes | None:
        """The photo as a JPEG no larger than 1024 px, or None."""
        try:
            raw = await self._download(url)
        except (httpx.HTTPError, _RefusedError) as exc:
            logger.warning("product_image_unavailable", reason=type(exc).__name__)
            return None
        if raw is None:
            return None
        try:
            image = Image.open(BytesIO(raw)).convert("RGB")
            image.thumbnail((_MAX_SIDE, _MAX_SIDE))
            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=88)
        except (UnidentifiedImageError, OSError, ValueError):
            logger.warning("product_image_undecodable")
            return None
        return buffer.getvalue()

    async def _download(self, url: str) -> bytes | None:
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            await self._check(current)
            async with self._client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return None
                    current = urljoin(current, location)
                    continue
                if response.status_code != httpx.codes.OK:
                    logger.warning("product_image_status", status=response.status_code)
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_bytes:
                        raise _RefusedError("too_large")
                return bytes(body)
        raise _RefusedError("too_many_redirects")

    async def _check(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise _RefusedError("not_https")
        if self._resolve_public and not await _is_public(parts.hostname):
            raise _RefusedError("not_public")

    async def close(self) -> None:
        await self._client.aclose()


class _RefusedError(Exception):
    """A URL this module will not request."""


async def _is_public(host: str) -> bool:
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(ipaddress.ip_address(a).is_global for a in addresses)
