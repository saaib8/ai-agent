"""Where a guest's chat moves under the account they signed in to.

Every Redis command for the move is here, and the move is one transaction: the
chat, its room photo and its finder photos are written under the new name and
removed from the old one together, or not at all. Each key comes from the store
that owns it (`session_key`, `room_photo_key`, `photo_key`), so no address is
spelled a second time. The finder's own local sign-in (`finder-visitor`) does
not move: it is a credential for one local run, never the shopper's.

Nothing is decoded on the way. The chat moves exactly as it was stored, with
its revision, so the screen the shopper is looking at stays current; each key
keeps the time it had left.
"""

from __future__ import annotations

import asyncio

from redis.asyncio import Redis
from redis.exceptions import RedisError, WatchError

from app.core.config import SessionSettings
from app.core.exceptions import SessionConflictError, SessionStoreUnavailableError
from app.core.logging import get_logger
from app.repositories.finder_photos import photo_key
from app.repositories.room_photos import room_photo_key
from app.repositories.sessions import SHOPPER_CART_KEY_TEMPLATE, session_key, shopper_of
from app.schemas.session import ShopperCart
from app.schemas.session_adoption import AdoptionOutcome

logger = get_logger(__name__)

ATTEMPTS = 2
"""A write to either chat between the read and the move aborts it; it is tried
once more before the caller is told the chat changed underneath it."""


class ConversationMoves:
    def __init__(self, client: Redis, settings: SessionSettings) -> None:
        self._client = client
        self._settings = settings

    async def move(
        self, store_id: int, from_session_id: str, to_session_id: str
    ) -> AdoptionOutcome:
        """Move the chat from one name to the other, never over one that exists."""
        try:
            async with asyncio.timeout(self._settings.operation_timeout_s):
                for _ in range(ATTEMPTS):
                    try:
                        return await self._move_once(store_id, from_session_id, to_session_id)
                    except WatchError:
                        logger.info("conversation_move_raced", store_id=store_id)
        except TimeoutError as exc:
            raise SessionStoreUnavailableError(dependency="redis") from exc
        except RedisError as exc:
            logger.warning("conversation_move_failed", error_type=type(exc).__name__)
            raise SessionStoreUnavailableError(dependency="redis") from exc
        raise SessionConflictError(store_id=store_id)

    async def keep_cart(self, store_id: int, session_id: str, cart: ShopperCart) -> None:
        """Keep `cart` as the cart of the shopper `session_id` names."""
        shopper = shopper_of(session_id)
        ttl = self._settings.shopper_cart_ttl_s
        if shopper is None or ttl == 0:
            return
        key = SHOPPER_CART_KEY_TEMPLATE.format(store_id=store_id, shopper=shopper)
        try:
            async with asyncio.timeout(self._settings.operation_timeout_s):
                await self._client.set(key, cart.model_dump_json(), ex=ttl)
        except TimeoutError as exc:
            raise SessionStoreUnavailableError(dependency="redis") from exc
        except RedisError as exc:
            logger.warning("shopper_cart_not_saved", error_type=type(exc).__name__)
            raise SessionStoreUnavailableError(dependency="redis") from exc

    async def _move_once(
        self, store_id: int, from_session_id: str, to_session_id: str
    ) -> AdoptionOutcome:
        source = session_key(store_id, from_session_id)
        target = session_key(store_id, to_session_id)
        beside = await self._beside(store_id, from_session_id)
        async with self._client.pipeline() as pipe:
            await pipe.watch(source, target, *beside)
            chat = await pipe.get(source)
            taken = bool(await pipe.exists(target))
            if chat is None or taken:
                # redis-py leaves `reset` and `multi` untyped, as in the session store.
                await pipe.reset()  # type: ignore[no-untyped-call]
                if chat is not None:
                    return AdoptionOutcome.TARGET_IN_USE
                return AdoptionOutcome.ALREADY_MOVED if taken else AdoptionOutcome.NOTHING_TO_MOVE

            moves = [(source, target, chat, await pipe.pttl(source))]
            for key in beside:
                value = await pipe.get(key)
                if value is not None:
                    moves.append((key, target + key[len(source) :], value, await pipe.pttl(key)))

            pipe.multi()  # type: ignore[no-untyped-call]
            whole_ms = self._settings.ttl_s * 1000
            for _, new, value, left_ms in moves:
                await pipe.set(new, value, px=left_ms if left_ms > 0 else whole_ms)
            await pipe.delete(*(old for old, *_ in moves))
            await pipe.execute()
        logger.info("conversation_moved", store_id=store_id, keys=len(moves))
        return AdoptionOutcome.MOVED

    async def _beside(self, store_id: int, session_id: str) -> list[str]:
        """The chat's room photo and finder photos: every key that extends its own."""
        keys = [room_photo_key(store_id, session_id)]
        # The session id admits no glob character, so this matches its photos only.
        async for key in self._client.scan_iter(match=photo_key(store_id, session_id, "*")):
            keys.append(key if isinstance(key, str) else key.decode())
        return keys
