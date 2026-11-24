"""The photo's outlines are drawn beside the reply whenever the finder looked in it.

The finder answers with cards, a question or nothing found; each time, the
photo the customer sent goes on screen with what was outlined in it, so they
can see what was found and tap another piece.
"""

from __future__ import annotations

from app.agents.payloads import PhotoPiecesShown, SearchShown
from app.agents.screen import presentation
from app.agents.work import ToolRecord, ToolStatus, TurnWork
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import AgentStateV1
from app.schemas.chat import ChatPresentation
from app.schemas.furniture_finder import FinderObjectView, ImageBox, PhotoPieces
from app.schemas.language import ReplyLanguage

from tests.unit.agents.test_find_in_photo import IMAGE_ID, FakeFinder, call, with_photo


def outlines_of(outcome: object) -> PhotoPieces:
    beside = getattr(outcome, "beside", None)
    assert isinstance(beside, PhotoPiecesShown)
    return beside.photo


async def test_with_its_matches_the_photo_is_outlined_and_the_piece_marked() -> None:
    outcome = await call(FakeFinder("bed", "lamp"), with_photo(), {"piece": "bed"})

    assert isinstance(outcome.payload, SearchShown), "the cards still own the screen"
    photo = outlines_of(outcome)
    assert (photo.image_id, photo.width, photo.height) == (IMAGE_ID, 100, 100)
    assert [o.label for o in photo.objects] == ["bed", "lamp"]
    assert photo.objects[0].polygon == ((10, 10), (89, 10), (89, 89), (10, 89))
    assert photo.looked_for == 1, "the bed is the piece its matches are like"


async def test_asking_which_piece_shows_every_piece_to_choose_from() -> None:
    outcome = await call(FakeFinder("sofa", "lamp", "lamp"), with_photo(), {})

    assert outcome.code == "which_piece"
    photo = outlines_of(outcome)
    assert [o.object_id for o in photo.objects] == [1, 2, 3]
    assert photo.looked_for is None


async def test_nothing_found_still_shows_the_photo() -> None:
    outcome = await call(FakeFinder(), with_photo(), {})

    assert outcome.code == "nothing_found_in_photo"
    assert outlines_of(outcome).objects == ()


async def test_no_photo_has_nothing_to_draw() -> None:
    outcome = await call(FakeFinder("bed"), AgentStateV1(), {"piece": "bed"})

    assert outcome.status is ToolStatus.REFUSED
    assert outcome.beside is None


def a_photo(**changes: object) -> PhotoPieces:
    piece = FinderObjectView(
        object_id=1,
        label="bed",
        display_label="bed",
        confidence=0.9,
        box=ImageBox(x1=10, y1=10, x2=90, y2=90),
        polygon=((10, 10), (89, 10), (89, 89)),
    )
    values: dict[str, object] = {
        "image_id": IMAGE_ID,
        "width": 100,
        "height": 100,
        "objects": (piece,),
    }
    values.update(changes)
    return PhotoPieces.model_validate(values)


def screen(*records: ToolRecord) -> ChatPresentation | None:
    work = TurnWork.starting(AgentStateV1())
    for record in records:
        work.record(record)
    return presentation(work, work.staged, (), ReplyLanguage.EN)


def a_record(*, status: ToolStatus = ToolStatus.OK, photo: PhotoPieces | None = None) -> ToolRecord:
    return ToolRecord(
        agent=AgentPersona.SARA,
        tool="find_in_photo",
        status=status,
        beside=PhotoPiecesShown(photo=photo) if photo is not None else None,
    )


def test_the_screen_draws_the_photo_beside_the_reply() -> None:
    shown = screen(a_record(photo=a_photo(looked_for=1)))

    assert shown is not None, "a photo alone is something to draw"
    assert shown.photo_pieces == a_photo(looked_for=1)


def test_the_latest_look_in_the_photo_is_the_one_drawn() -> None:
    shown = screen(a_record(photo=a_photo()), a_record(photo=a_photo(looked_for=1)))

    assert shown is not None and shown.photo_pieces is not None
    assert shown.photo_pieces.looked_for == 1


def test_a_refused_look_draws_nothing() -> None:
    assert screen(a_record(status=ToolStatus.REFUSED, photo=a_photo())) is None


def test_a_turn_that_did_not_look_has_no_photo() -> None:
    assert ChatPresentation().photo_pieces is None
    assert ChatPresentation().is_empty()
