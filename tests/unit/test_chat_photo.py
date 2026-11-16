"""A photo sent with a chat message (docs/photos-and-cart-pictures-plan.md D2)."""

from __future__ import annotations

import base64
from io import BytesIO
from typing import Any, cast

import pytest
from app.core.exceptions import ImageRejectedError
from app.schemas.chat import ChatRequest
from app.schemas.chat_photo import ChatPhoto
from app.schemas.retailer import RetailerContext
from app.services.chat_photo import ChatPhotoService
from PIL import Image
from pydantic import ValidationError

CONTEXT = RetailerContext(store_id=50)


def a_photo() -> str:
    buffer = BytesIO()
    Image.new("RGB", (640, 480), (200, 180, 160)).save(buffer, "JPEG")
    return base64.b64encode(buffer.getvalue()).decode()


class Store:
    def __init__(self) -> None:
        self.saved: list[ChatPhoto] = []

    async def save(self, store_id: int, session_id: str, photo: ChatPhoto) -> None:
        self.saved.append(photo)


class RoomCheck:
    def __init__(self, answer: bool | None) -> None:
        self.answer = answer
        self.checked = 0

    async def is_room(self, jpeg: bytes, context: Any) -> bool | None:
        self.checked += 1
        return self.answer


def service(store: Store, check: RoomCheck | None) -> ChatPhotoService:
    return ChatPhotoService(cast(Any, store), cast(Any, check), max_bytes=10_000_000, max_side=2048)


@pytest.mark.parametrize("answer", [True, False, None])
async def test_a_photo_is_room_checked_once_and_kept(answer: bool | None) -> None:
    store, check = Store(), RoomCheck(answer)

    record = await service(store, check).receive(a_photo(), session_id="s1", context=CONTEXT)

    assert check.checked == 1
    assert record.is_room is answer
    assert [p.photo_id for p in store.saved] == [record.photo_id]


async def test_without_room_photos_it_is_kept_and_its_room_not_known() -> None:
    store = Store()

    record = await service(store, None).receive(a_photo(), session_id="s1", context=CONTEXT)

    assert record.is_room is None and len(store.saved) == 1


async def test_a_photo_that_cannot_be_read_is_refused_and_not_kept() -> None:
    store = Store()

    with pytest.raises(ImageRejectedError):
        await service(store, RoomCheck(True)).receive(
            "not-base64!", session_id="s1", context=CONTEXT
        )

    assert store.saved == []


def test_a_photo_may_come_without_words_but_a_message_needs_one_or_the_other() -> None:
    ChatRequest(session_id="s1", store_id=50, photo=a_photo())
    with pytest.raises(ValidationError):
        ChatRequest(session_id="s1", store_id=50, message="  ")
