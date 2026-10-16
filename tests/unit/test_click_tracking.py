"""Zory's click id on the links a customer can follow."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from app.schemas.retailer import RetailerContext
from app.services.click_tracking import CLICK_ID_PARAM, ClickTrackingService, tracking_url
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import OperationalError

from tests.unit.click_ids import FakeClickIds

STORE = 50
CONTEXT = RetailerContext(store_id=STORE)
CLICK = "7f1c2a3e-0000-4000-8000-000000000001"


class Card(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    product_url: str


class Picture(BaseModel):
    """A shape with a link that is not a product page: never touched."""

    model_config = ConfigDict(frozen=True)

    image_url: str


class Screen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cards: tuple[Card, ...] = ()
    focus: Card | None = None
    by_slot: dict[str, Card] = {}
    picture: Picture | None = None
    note: str = ""


class TestTrackingUrl:
    def test_a_page_with_no_query_gains_the_click_id(self) -> None:
        assert tracking_url("https://shop.test/p/1", CLICK) == (
            f"https://shop.test/p/1?{CLICK_ID_PARAM}={CLICK}"
        )

    def test_the_page_own_parameters_are_kept(self) -> None:
        url = tracking_url("https://shop.test/p/1?ref=home&utm_source=x#reviews", CLICK)
        assert (
            url == f"https://shop.test/p/1?ref=home&utm_source=x&{CLICK_ID_PARAM}={CLICK}#reviews"
        )

    def test_an_existing_click_id_is_replaced_not_repeated(self) -> None:
        url = tracking_url(f"https://shop.test/p/1?{CLICK_ID_PARAM}=old&a=1", CLICK)
        assert url == f"https://shop.test/p/1?a=1&{CLICK_ID_PARAM}={CLICK}"


class TestAttach:
    async def test_every_product_url_with_a_known_page_carries_its_click_id(self) -> None:
        source = FakeClickIds({"https://shop.test/p/1": CLICK, "https://shop.test/p/2": "c2"})
        screen = Screen(
            cards=(
                Card(name="one", product_url="https://shop.test/p/1"),
                Card(name="gone", product_url="https://shop.test/p/9"),
            ),
            focus=Card(name="two", product_url="https://shop.test/p/2"),
            by_slot={"sofa": Card(name="one again", product_url="https://shop.test/p/1")},
        )

        filled = await ClickTrackingService(source).attach(screen, CONTEXT)

        assert filled.cards[0].product_url == tracking_url("https://shop.test/p/1", CLICK)
        assert filled.cards[1].product_url == "https://shop.test/p/9", "no id: left as it was"
        assert filled.focus is not None
        assert filled.focus.product_url == tracking_url("https://shop.test/p/2", "c2")
        assert filled.by_slot["sofa"].product_url == filled.cards[0].product_url
        assert screen.cards[0].product_url == "https://shop.test/p/1", "the original is untouched"

    async def test_one_lookup_per_response_scoped_to_the_store(self) -> None:
        source = FakeClickIds({"https://shop.test/p/1": CLICK})
        screen = Screen(
            cards=(
                Card(name="a", product_url="https://shop.test/p/1"),
                Card(name="b", product_url="https://shop.test/p/1"),
            ),
        )
        await ClickTrackingService(source).attach(screen, CONTEXT)
        assert source.lookups == [(("https://shop.test/p/1",), STORE)]

    async def test_a_response_without_product_pages_issues_no_lookup(self) -> None:
        source = FakeClickIds()
        screen = Screen(note="just words", picture=Picture(image_url="https://shop.test/1.jpg"))
        assert await ClickTrackingService(source).attach(screen, CONTEXT) is screen
        assert source.lookups == []

    async def test_nothing_known_returns_the_response_as_it_was(self) -> None:
        screen = Screen(cards=(Card(name="a", product_url="https://shop.test/p/1"),))
        assert await ClickTrackingService(FakeClickIds()).attach(screen, CONTEXT) is screen

    @pytest.mark.parametrize(
        "error",
        [OperationalError("SELECT", {}, Exception("down")), ConnectionRefusedError()],
    )
    async def test_a_failed_lookup_leaves_links_untracked_rather_than_failing(
        self, error: Exception
    ) -> None:
        class Down:
            async def click_ids_by_url(
                self, product_urls: Sequence[str], context: RetailerContext
            ) -> dict[str, str]:
                raise error

        screen = Screen(cards=(Card(name="a", product_url="https://shop.test/p/1"),))
        assert await ClickTrackingService(Down()).attach(screen, CONTEXT) is screen
