"""Where a conversation lives between requests.

Every Redis command for sessions is here. Graph and runtime code asks for a
session and offers a new one; it never builds a key, never serialises, and
never reasons about transactions (M13 27).

Three decisions carry the weight.

**The key is the only place scope lives.** `store_id` and `session_id` are in
the key and nowhere in the value, so the same session id under two retailers is
two unrelated records with no field that could disagree about which is which.

**Writes are compare-and-set, never read-then-write.** Two requests can load
revision 4 concurrently; only one may write revision 5. A plain `SET` would let
the slower one overwrite the faster one's committed turn, losing a state
transition the customer was already told about.

**Absent and unreadable are different answers.** A missing key is a new
conversation. A key holding something this version cannot validate is a
refusal - resetting it would discard a room the customer built and then
reinterpret "the second one" against an empty session.
"""

from __future__ import annotations

import asyncio
import re

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError, WatchError

from app.core.config import SessionSettings
from app.core.exceptions import (
    InvalidRequestError,
    SessionStateInvalidError,
    SessionStoreUnavailableError,
)
from app.core.logging import get_logger
from app.schemas.session import SessionEnvelope

logger = get_logger(__name__)

KEY_TEMPLATE = "zory:store:{store_id}:session:{session_id}"

MAX_SESSION_ID_CHARS = 128
_SESSION_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
"""What a session id may contain.

Restricted rather than escaped. `:` is the key separator, so a permissive id
could address a key in another retailer's namespace - and an id containing a
glob character would match patterns nobody intended. Refusing the input is one
rule; escaping correctly everywhere a key is built is several, and only one of
them has to be missed.
"""


def session_key(store_id: int, session_id: str) -> str:
    """The one place a session key is constructed.

    Validates on every call rather than trusting the transport to have done it.
    The API layer checks the same rule, which is duplication on purpose: this is
    the function that turns a string into a Redis address.
    """
    if not _SESSION_ID.fullmatch(session_id):
        raise InvalidRequestError(
            public_message="That session identifier is not valid.",
            session_id_length=len(session_id),
        )
    return KEY_TEMPLATE.format(store_id=store_id, session_id=session_id)


class SessionStore:
    """Load a conversation, or replace it only if nobody else did first."""

    def __init__(self, client: Redis, settings: SessionSettings) -> None:
        self._client = client
        self._settings = settings

    async def load(self, store_id: int, session_id: str) -> SessionEnvelope | None:
        """The stored conversation, or None when there has never been one.

        `None` means the key is genuinely absent - a new conversation, or one
        whose TTL elapsed. Anything present but unreadable raises instead, so a
        caller cannot treat corruption as a fresh start.
        """
        key = session_key(store_id, session_id)
        raw = await self._read(key)
        if raw is None:
            return None
        return self._decode(raw, store_id=store_id)

    async def save_if_revision(
        self,
        store_id: int,
        session_id: str,
        *,
        expected_revision: int,
        envelope: SessionEnvelope,
    ) -> bool:
        """Write the next snapshot, but only over the revision we started from.

        Returns False when someone else committed first - never an exception,
        because losing the race is an ordinary outcome that the caller answers
        with a conflict rather than an error.

        `WATCH` covers the absent case too, which is what protects two
        simultaneous first requests: both see no key, both try to write
        revision 1, and the transaction of whichever runs second aborts because
        the key it was watching came into existence (M13 19).
        """
        key = session_key(store_id, session_id)
        payload = envelope.model_dump_json()
        ttl = self._settings.ttl_s

        try:
            async with asyncio.timeout(self._settings.operation_timeout_s):
                async with self._client.pipeline() as pipe:
                    await pipe.watch(key)
                    current = await pipe.get(key)
                    if not self._revision_matches(current, expected_revision, store_id):
                        # redis-py leaves `reset` and `multi` untyped; the
                        # transaction semantics they provide are the point of
                        # this method, so they are used and annotated here.
                        await pipe.reset()  # type: ignore[no-untyped-call]
                        return False

                    pipe.multi()  # type: ignore[no-untyped-call]
                    await pipe.set(key, payload, ex=ttl)
                    await pipe.execute()
        except WatchError:
            # The key changed between the check and the write. Whoever changed
            # it committed a turn we have not seen.
            logger.info(
                "session_save_conflict",
                store_id=store_id,
                expected_revision=expected_revision,
            )
            return False
        except TimeoutError as exc:
            raise SessionStoreUnavailableError(dependency="redis") from exc
        except RedisError as exc:
            logger.warning("session_save_failed", error_type=type(exc).__name__)
            raise SessionStoreUnavailableError(dependency="redis") from exc

        logger.info(
            "session_saved",
            store_id=store_id,
            session_revision=envelope.session_revision,
            ttl_s=ttl,
        )
        return True

    # ── the parts that touch Redis or JSON ──────────────────────────────────

    async def _read(self, key: str) -> str | None:
        try:
            async with asyncio.timeout(self._settings.operation_timeout_s):
                value = await self._client.get(key)
        except TimeoutError as exc:
            raise SessionStoreUnavailableError(dependency="redis") from exc
        except RedisError as exc:
            logger.warning("session_load_failed", error_type=type(exc).__name__)
            raise SessionStoreUnavailableError(dependency="redis") from exc
        if value is None:
            return None
        # The client decodes responses, but a bytes value would still validate
        # fine and then compare unequal everywhere else.
        return value if isinstance(value, str) else value.decode()

    @staticmethod
    def _decode(raw: str, *, store_id: int) -> SessionEnvelope:
        """Stored bytes as a real envelope, or a controlled refusal.

        Validated through the actual contracts, so an agent state written by an
        older schema fails here rather than being coerced into a shape that
        happens to parse (M13 7).
        """
        try:
            return SessionEnvelope.model_validate_json(raw)
        except ValidationError as exc:
            # The payload itself is never logged: it is the customer's room and
            # conversation, and this is a diagnostic.
            logger.warning(
                "session_state_unreadable",
                store_id=store_id,
                error_count=len(exc.errors()),
            )
            raise SessionStateInvalidError(store_id=store_id) from exc

    @staticmethod
    def _revision_matches(current: str | bytes | None, expected: int, store_id: int) -> bool:
        """Whether what is stored now is still the revision we loaded.

        An absent key matches expectation zero and nothing else: expecting
        revision 4 and finding no key means the session expired underneath this
        request, which is a conflict rather than a licence to create it.
        """
        if current is None:
            return expected == 0
        text = current if isinstance(current, str) else current.decode()
        try:
            stored = SessionEnvelope.model_validate_json(text)
        except ValidationError as exc:
            raise SessionStateInvalidError(store_id=store_id) from exc
        return stored.session_revision == expected
