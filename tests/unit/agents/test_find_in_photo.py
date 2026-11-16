"""The finder as Sara's tool, on the photo sent in the chat
(docs/photos-and-cart-pictures-plan.md D5, D7)."""

from __future__ import annotations

import base64
from typing import Any, cast

from app.agents.payloads import SearchShown
from app.agents.tools.shopping import find_in_photo_tool
from app.agents.work import ToolStatus, TurnWork
from app.schemas.agent_state import AgentStateV1
from app.schemas.chat_photo import ChatPhoto, ChatPhotoState
from app.schemas.furniture_finder import DetectedObject, FinderPhoto, ImageBox

from tests.unit.agents.fakes import _product, agent_services, tool_context

PHOTO_ID = "a" * 32
IMAGE_ID = "b" * 32


def detected(object_id: int, label: str) -> DetectedObject:
    return DetectedObject(
        object_id=object_id,
        label=label,
        confidence=0.9,
        box=ImageBox(x1=10, y1=10, x2=90, y2=90),
        polygon=((10, 10), (89, 10), (89, 89), (10, 89)),
    )


class FakeFinder:
    def __init__(self, *labels: str) -> None:
        self.photo = FinderPhoto(
            image_id=IMAGE_ID,
            width=100,
            height=100,
            scan_id="1",
            stored_image="furniture-finder-stage/x.png",
            objects=tuple(detected(n, label) for n, label in enumerate(labels, start=1)),
        )
        self.detections = 0
        self.found: list[str] = []

    async def detect(self, data: bytes, **_: Any) -> tuple[FinderPhoto, int]:
        self.detections += 1
        return self.photo, 0

    async def load_photo(self, session_id: str, image_id: str, context: Any) -> FinderPhoto:
        return self.photo

    async def find(self, photo: Any, piece: DetectedObject, context: Any, shopper: Any) -> Any:
        self.found.append(piece.label)
        return (_product(201), _product(202))


class FakeChatPhotos:
    async def load(self, state: Any, *, session_id: str, context: Any) -> ChatPhoto:
        return ChatPhoto(
            photo_id=PHOTO_ID,
            width=100,
            height=100,
            jpeg_b64=base64.b64encode(b"jpeg").decode(),
        )


def with_photo(**record: Any) -> AgentStateV1:
    return AgentStateV1(chat_photo=ChatPhotoState(photo_id=PHOTO_ID, is_room=True, **record))


async def call(
    finder: FakeFinder, state: AgentStateV1, args: dict[str, Any], **context: Any
) -> Any:
    tool = find_in_photo_tool()
    services = agent_services(finder=cast(Any, finder), chat_photos=cast(Any, FakeChatPhotos()))
    return await tool.handler(
        tool.args.model_validate(args),
        tool_context(services, session_id="s1", **context),
        TurnWork.starting(state),
    )


async def test_a_named_piece_is_found_and_its_matches_are_the_cards() -> None:
    finder = FakeFinder("bed", "lamp")

    outcome = await call(finder, with_photo(), {"piece": "bed"})

    assert outcome.status is ToolStatus.OK
    assert finder.found == ["bed"]
    assert isinstance(outcome.payload, SearchShown)
    assert outcome.state.product_interaction.presented_product_ids == (201, 202)
    assert outcome.state.chat_photo.pieces == ("bed", "lamp")


async def test_the_photo_is_detected_once_and_reused() -> None:
    finder = FakeFinder("bed", "lamp")
    first = await call(finder, with_photo(), {"piece": "lamp"})

    await call(finder, first.state, {"piece": "bed"})

    assert finder.detections == 1


async def test_several_pieces_and_none_named_asks_which() -> None:
    finder = FakeFinder("sofa", "lamp", "lamp")

    outcome = await call(finder, with_photo(), {})

    assert outcome.code == "which_piece"
    assert [p["piece"] for p in outcome.facts["pieces_found"]] == ["sofa", "lamp", "lamp"]
    assert finder.found == []


async def test_a_word_matching_two_pieces_asks_which_and_a_number_picks_one() -> None:
    finder = FakeFinder("sofa", "lamp", "lamp")

    asked = await call(finder, with_photo(), {"piece": "lamps"})
    assert asked.code == "which_piece"

    picked = await call(finder, asked.state, {"number": 3})
    assert picked.status is ToolStatus.OK and finder.found == ["lamp"]


async def test_the_only_piece_is_found_without_a_question() -> None:
    finder = FakeFinder("armchair")

    outcome = await call(finder, with_photo(), {})

    assert finder.found == ["armchair"] and outcome.status is ToolStatus.OK


async def test_no_photo_in_the_chat_is_refused() -> None:
    outcome = await call(FakeFinder("bed"), AgentStateV1(), {"piece": "bed"})

    assert outcome.code == "no_photo_sent"


async def test_the_gateways_refusal_stops_the_finder_before_it_runs() -> None:
    finder = FakeFinder("bed")

    outcome = await call(finder, with_photo(), {"piece": "bed"}, finder_access="limit_reached")

    assert outcome.code == "finder_not_allowed"
    assert outcome.facts == {"reason": "limit_reached"}
    assert finder.detections == 0


async def test_another_word_for_a_piece_is_left_to_the_agent_with_what_was_found() -> None:
    finder = FakeFinder("l-shape-sofa", "carpet", "lampshade")

    outcome = await call(finder, with_photo(), {"piece": "rug"})

    # Code matches only the piece's own word; "a rug is a carpet" is Sara's to say.
    assert outcome.code == "which_piece" and finder.found == []
    assert outcome.facts["asked_for"] == "rug" and outcome.facts["same_word"] == []
    assert [p["piece"] for p in outcome.facts["pieces_found"]] == [
        "l shape sofa",
        "carpet",
        "lampshade",
    ]

    picked = await call(finder, outcome.state, {"number": 2})
    assert finder.found == ["carpet"] and picked.status is ToolStatus.OK
