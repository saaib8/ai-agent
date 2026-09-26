"""Browse Catalogue: filtering the store's catalog, facets, the fit facts, and
rendering a room from picked products.

Faked is only what leaves the process: PostgreSQL (behind the repository's
contract), the image model, S3, the photo host and Redis. Vocabulary checks,
the fit facts, the prompt, the selection render and the session commit are
the real code.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from app.api.dependencies import (
    catalog_service,
    catalog_visualization_runtime,
    retailer_context_provider,
)
from app.api.errors import register_exception_handlers
from app.api.routes.catalog import router as catalog_router
from app.core.config import CatalogSettings, SessionSettings
from app.core.exceptions import (
    CatalogFilterRejectedError,
    SelectionRejectedError,
    SelectionUnavailableError,
    SessionConflictError,
)
from app.prompts.visualization.v1 import ROOM_TYPES, build_prompt
from app.repositories.products import FacetCounts
from app.schemas.catalog import (
    CatalogFilter,
    CatalogQuery,
    CatalogSelectionItem,
    CatalogSort,
    CatalogVisualizeRequest,
)
from app.schemas.dimensions import RawDimensions
from app.schemas.discovery import ProductSort
from app.schemas.product import CommerceClassification, ProductRow
from app.schemas.retailer import RetailerContext
from app.schemas.visualization import RenderRoomSpec, RenderSource, RenderView, RoomType
from app.services.catalog import FLOOR_CATEGORIES, CatalogService, catalog_item
from app.services.room_visualization import CatalogVisualizationRuntime, RoomVisualizer
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.registry import load_taxonomy
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from tests.conftest import build_settings
from tests.unit.test_chat_api import FakeRetailers, FakeSessionStore
from tests.unit.test_room_visualization import (
    FakeCatalog,
    FakePhotos,
    RecordingGenerator,
    viz_settings,
)

STORE = 50
SESSION = "sess-1"
CONTEXT = RetailerContext(store_id=STORE)
TAXONOMY = load_taxonomy()
ATTRIBUTES = load_catalog_attributes()


def a_row(
    product_id: int,
    *,
    name: str | None = None,
    category: str = "seating",
    subcategory: str = "sofa",
    length: str | None = "220",
    width: str | None = "95",
    unit: str | None = "cm",
    price: str = "2500",
) -> ProductRow:
    return ProductRow(
        id=product_id,
        uuid=uuid4(),
        store_id=STORE,
        name_english=name or f"Piece {product_id}",
        name_arabic="قطعة",
        price_amount=Decimal(price),
        price_unit="SAR",
        image_url=f"https://shop.test/{product_id}.jpg",
        product_url=f"https://shop.test/p/{product_id}",
        visual_category="sofa",
        commerce=CommerceClassification(category=category, subcategory=subcategory),
        dimensions=RawDimensions(
            length=Decimal(length) if length else None,
            width=Decimal(width) if width else None,
            height=Decimal("80"),
            unit=unit,
        ),
        main_color="Beige",
        styles=("Modern",),
        is_active=True,
    )


class FakeBrowseRepository:
    """The repository's browse contract, recording what it was asked."""

    def __init__(self, rows: Sequence[ProductRow] = (), count: int | None = None) -> None:
        self.rows = list(rows)
        self.count = len(self.rows) if count is None else count
        self.filters: list[CatalogFilter] = []
        self.pages: list[tuple[int, int]] = []

    async def count_browse(self, filters: CatalogFilter, context: RetailerContext) -> int:
        self.filters.append(filters)
        return self.count

    async def browse(
        self, filters: CatalogFilter, context: RetailerContext, *, offset: int, limit: int
    ) -> list[ProductRow]:
        self.pages.append((offset, limit))
        return self.rows[:limit]

    async def supported_commerce_types(
        self, context: RetailerContext
    ) -> tuple[tuple[str, str | None, int], ...]:
        return (
            ("decor", "vase", 3),
            ("seating", None, 2),
            ("seating", "chair", 4),
            ("seating", "sofa", 10),
            ("seating", "made-up-thing", 1),
            ("not-a-category", "sofa", 7),
        )

    async def facet_counts(self, context: RetailerContext) -> FacetCounts:
        return FacetCounts(
            colors=(("Beige", 5), ("beige", 2), ("Moonbeam", 9), ("Grey", 6)),
            styles=(("Modern", 8), ("Modern_Classic", 3), ("Futurist", 4)),
            prices=(
                ("SAR", Decimal("80"), Decimal("11950"), 20),
                ("USD", Decimal("5"), Decimal("9"), 1),
            ),
        )


def a_service(
    repository: FakeBrowseRepository | None = None,
    *,
    render_available: bool = True,
    **settings: Any,
) -> tuple[CatalogService, FakeBrowseRepository]:
    repo = repository or FakeBrowseRepository([a_row(1), a_row(2)])
    service = CatalogService(
        repo,  # type: ignore[arg-type]
        TAXONOMY,
        ATTRIBUTES,
        CatalogSettings(**settings),
        render_available=render_available,
    )
    return service, repo


def a_query(**values: Any) -> CatalogQuery:
    return CatalogQuery(store_id=STORE, **values)


# ── settings and vocabularies ═══════════════════════════════════════════════


class TestSettings:
    def test_a_selection_is_capped_at_what_one_render_can_take(self) -> None:
        settings = build_settings(
            visualization=viz_settings(max_references=5).model_dump(mode="json")
            | {"gemini_api_key": "k"},
            catalog={"max_products": 9},
        )
        assert settings.effective_catalog().max_products == 5
        assert build_settings(catalog={"max_products": 9}).effective_catalog().max_products == 9

    def test_bounds_must_be_ordered(self) -> None:
        with pytest.raises(ValidationError):
            CatalogSettings(page_size=80, max_page_size=60)
        with pytest.raises(ValidationError):
            CatalogSettings(min_room_side_m=5, max_room_side_m=5)


def test_every_floor_category_is_approved() -> None:
    assert TAXONOMY.categories >= FLOOR_CATEGORIES


def test_every_room_type_is_worded_for_the_prompt_and_the_picker() -> None:
    assert set(ROOM_TYPES) == set(RoomType)


# ── browsing ════════════════════════════════════════════════════════════════


class TestBrowse:
    async def test_a_page_reports_its_place_in_the_whole(self) -> None:
        service, repo = a_service(FakeBrowseRepository([a_row(1), a_row(2)], count=50))
        page = await service.browse(a_query(page=2, page_size=20), CONTEXT)
        assert (page.page, page.page_size, page.total_pages, page.count) == (2, 20, 3, 50)
        assert repo.pages == [(20, 20)]
        assert [item.product_id for item in page.items] == [1, 2]

    async def test_the_page_size_is_capped_by_configuration(self) -> None:
        service, repo = a_service(max_page_size=30, page_size=10)
        await service.browse(a_query(page_size=500), CONTEXT)
        assert repo.pages == [(0, 30)]

    async def test_a_page_past_the_end_reads_no_rows(self) -> None:
        service, repo = a_service(FakeBrowseRepository([a_row(1)], count=1))
        page = await service.browse(a_query(page=9), CONTEXT)
        assert page.items == () and page.count == 1
        assert repo.pages == []

    async def test_vocabulary_is_normalised_to_approved_spelling(self) -> None:
        service, repo = a_service()
        await service.browse(
            a_query(
                category=" Seating ",
                subcategory="SOFA",
                color="light grey",
                style="modern classic",
                sort=CatalogSort.PRICE_ASC,
            ),
            CONTEXT,
        )
        (filters,) = repo.filters
        assert (filters.category, filters.subcategory) == ("seating", "sofa")
        assert (filters.color, filters.style) == ("Light Grey", "Modern_Classic")
        assert filters.sort is ProductSort.PRICE_ASC

    async def test_featured_leads_with_floor_furniture(self) -> None:
        service, repo = a_service()
        await service.browse(a_query(), CONTEXT)
        assert set(repo.filters[0].lead_categories) == FLOOR_CATEGORIES

    async def test_name_words_are_split_deduplicated_and_bounded(self) -> None:
        service, repo = a_service()
        await service.browse(a_query(q="  grey   sofa grey a b c d e f g "), CONTEXT)
        assert repo.filters[0].name_words == ("grey", "sofa", "a", "b", "c", "d", "e", "f")

    @pytest.mark.parametrize(
        ("values", "message"),
        [
            ({"category": "chairs"}, "That category isn't in this catalogue."),
            ({"category": "seating", "subcategory": "bed"}, "That type isn't in this category."),
            ({"color": "Moonbeam"}, "That color isn't one this catalogue uses."),
            ({"style": "Futurist"}, "That style isn't one this catalogue uses."),
            ({"q": "x" * 81}, "That search is too long. Try a few words from the name."),
        ],
    )
    async def test_an_unapproved_filter_never_reaches_the_repository(
        self, values: dict[str, Any], message: str
    ) -> None:
        service, repo = a_service()
        with pytest.raises(CatalogFilterRejectedError) as raised:
            await service.browse(a_query(**values), CONTEXT)
        assert raised.value.public_message == message
        assert repo.filters == []

    @pytest.mark.parametrize(
        "values",
        [
            {"min_price": "100"},
            {"min_price": "500", "max_price": "100", "currency": "SAR"},
            {"subcategory": "sofa"},
        ],
    )
    def test_a_malformed_query_is_refused_at_the_boundary(self, values: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            a_query(**values)


# ── facets ══════════════════════════════════════════════════════════════════


class TestFacets:
    async def test_only_approved_vocabulary_is_offered(self) -> None:
        service, _ = a_service()
        facets = await service.facets(CONTEXT)

        assert [(c.value, c.count) for c in facets.categories] == [("decor", 3), ("seating", 17)]
        seating = facets.categories[1]
        assert [(s.value, s.count) for s in seating.subcategories] == [("chair", 4), ("sofa", 10)]
        assert [(c.value, c.count) for c in facets.colors] == [("Beige", 7), ("Grey", 6)]
        assert [(s.value, s.count) for s in facets.styles] == [
            ("Modern", 8),
            ("Modern_Classic", 3),
        ]

    async def test_the_price_range_is_the_main_currency_s(self) -> None:
        service, _ = a_service()
        price = (await service.facets(CONTEXT)).price
        assert price is not None
        assert (price.currency, price.min_amount, price.max_amount) == (
            "SAR",
            Decimal("80"),
            Decimal("11950"),
        )

    async def test_the_studio_carries_every_limit_and_vocabulary(self) -> None:
        service, _ = a_service(render_available=False, max_products=9, max_quantity=4)
        studio = (await service.facets(CONTEXT)).studio
        assert [r.value for r in studio.room_types] == list(RoomType)
        assert set(studio.styles) == ATTRIBUTES.styles
        assert (studio.max_products, studio.max_quantity) == (9, 4)
        assert studio.render_available is False


# ── the fit facts ═══════════════════════════════════════════════════════════


class TestFitFacts:
    def test_a_floor_piece_of_known_size_has_a_footprint(self) -> None:
        item = catalog_item(a_row(1, length="220", width="95"))
        assert item.stands_on_floor is True
        assert (item.footprint_cm2, item.longest_side_cm) == (20900.0, 220.0)

    def test_the_footprint_does_not_care_which_way_round_the_sides_were_stored(self) -> None:
        assert catalog_item(a_row(1, length="95", width="220")).footprint_cm2 == 20900.0

    def test_metres_are_converted_like_everywhere_else(self) -> None:
        item = catalog_item(a_row(1, length="2.2", width="0.95", unit="m"))
        assert item.footprint_cm2 == 20900.0

    @pytest.mark.parametrize(
        ("row", "floor"),
        [
            (a_row(1, category="decor", subcategory="carpet"), False),
            (a_row(1, category="lighting", subcategory="floor-lamp"), False),
            (a_row(1, unit="furlongs"), True),
            (a_row(1, width=None), True),
        ],
        ids=["rug", "lamp", "unknown-unit", "missing-side"],
    )
    def test_nothing_is_guessed(self, row: ProductRow, floor: bool) -> None:
        item = catalog_item(row)
        assert item.stands_on_floor is floor
        assert item.footprint_cm2 is None and item.longest_side_cm is None


# ── rendering a selection ═══════════════════════════════════════════════════


SPEC = RenderRoomSpec(room_type=RoomType.MAJLIS, style="Modern_Classic", length_m=4.5, width_m=6)


def a_visualizer(
    rows: Sequence[ProductRow] | None = None,
) -> tuple[RoomVisualizer, dict[str, Any]]:
    parts: dict[str, Any] = {
        "catalog": FakeCatalog(
            rows
            if rows is not None
            else [a_row(1, name="Boucle Sofa"), a_row(2, name="Accent Chair", subcategory="chair")]
        ),
        "photos": FakePhotos(),
        "generator": RecordingGenerator(),
    }
    visualizer = RoomVisualizer(
        parts["catalog"],
        parts["photos"],
        parts["generator"],
        viz_settings(max_references=14),
    )
    return visualizer, parts


def picks(*pairs: tuple[int, int]) -> tuple[CatalogSelectionItem, ...]:
    return tuple(CatalogSelectionItem(product_id=pid, quantity=qty) for pid, qty in pairs)


class TestSelectionRender:
    async def test_the_picked_pieces_are_rendered_in_the_room_set_up(self) -> None:
        visualizer, parts = a_visualizer()
        render = await visualizer.render_selection(
            picks((2, 2), (1, 1)), SPEC, RenderView.CORNER, CONTEXT
        )

        (prompt,) = parts["generator"].prompts
        assert "render of a modern classic majlis (a Gulf-style formal sitting room" in prompt
        assert "approximately 4.5 m by 6.0 m" in prompt
        assert "(3 pieces in total)" in prompt
        assert "- image 1: Accent Chair (chair, 220 x 95 x 80 cm) - 2 of them" in prompt
        assert "- image 2: Boucle Sofa (sofa, 220 x 95 x 80 cm)" in prompt
        assert render.source is RenderSource.CATALOG
        assert render.room == SPEC
        assert [(i.name_english, i.quantity) for i in render.items] == [
            ("Accent Chair", 2),
            ("Boucle Sofa", 1),
        ]

    async def test_pieces_the_store_no_longer_sells_are_left_out(self) -> None:
        visualizer, _ = a_visualizer()
        render = await visualizer.render_selection(
            picks((1, 1), (999, 3)), SPEC, RenderView.CORNER, CONTEXT
        )
        assert [i.name_english for i in render.items] == ["Boucle Sofa"]

    async def test_nothing_left_is_refused_before_anything_is_paid(self) -> None:
        visualizer, parts = a_visualizer(rows=[])
        with pytest.raises(SelectionUnavailableError):
            await visualizer.render_selection(picks((1, 1)), SPEC, RenderView.CORNER, CONTEXT)
        assert parts["generator"].prompts == []

    def test_the_package_prompt_is_unchanged_by_room_types(self) -> None:
        """Catalogue renders reuse the package prompt; its wording is not forked."""
        from app.prompts.visualization.v1 import RenderRoom

        prompt = build_prompt(
            RenderRoom(room_label="bedroom", style="modern", length_m=4.0, width_m=5.0),
            (),
            RenderView.CORNER,
        )
        assert prompt.startswith("Create a single photorealistic render of a modern bedroom")


def a_runtime(
    sessions: FakeSessionStore | None = None,
    rows: Sequence[ProductRow] | None = None,
    **catalog: Any,
) -> tuple[CatalogVisualizationRuntime, dict[str, Any], FakeSessionStore]:
    store = sessions or FakeSessionStore()
    visualizer, parts = a_visualizer(rows)
    runtime = CatalogVisualizationRuntime(
        visualizer,
        store,  # type: ignore[arg-type]
        SessionSettings(max_history_messages=6),
        CatalogSettings(**catalog),
        ATTRIBUTES,
    )
    return runtime, parts, store


def a_request(**values: Any) -> CatalogVisualizeRequest:
    body: dict[str, Any] = {
        "session_id": SESSION,
        "store_id": STORE,
        "items": [{"product_id": 1, "quantity": 1}, {"product_id": 2, "quantity": 2}],
        "room": {"room_type": "living_room", "style": "modern", "length_m": 4, "width_m": 5},
        "view": "isometric",
    }
    body.update(values)
    return CatalogVisualizeRequest.model_validate(body)


class TestSelectionTurn:
    async def test_a_render_is_a_committed_turn_that_changes_no_state(self) -> None:
        runtime, _, sessions = a_runtime()
        reply = await runtime.visualize(a_request(), CONTEXT)

        assert reply.session_revision == 1
        assert reply.response.message == "Here's your modern living room, from above."
        assert reply.presentation is not None and reply.presentation.render is not None
        assert reply.presentation.render.room is not None
        assert reply.presentation.render.room.style == "Modern", "stored in approved spelling"
        saved = sessions.saved[(STORE, SESSION)]
        assert saved.state.room_project is None, "a selection does not become the package"
        user, assistant = saved.conversation.messages
        assert user.content == (
            "[Visualised 3 pieces picked from the catalogue in a 4 x 5 m modern living room, "
            "isometric view]"
        )
        assert assistant.content == reply.response.message

    async def test_the_reply_says_what_was_left_out(self) -> None:
        runtime, _, _ = a_runtime(rows=[a_row(1)])
        reply = await runtime.visualize(a_request(), CONTEXT)
        assert reply.response.message == (
            "Here's your modern living room, from above. 1 piece you picked is no longer "
            "available, so I left it out."
        )

    @pytest.mark.parametrize(
        ("values", "settings", "message"),
        [
            (
                {"room": {"room_type": "bedroom", "style": "Bauhaus", "length_m": 4, "width_m": 5}},
                {},
                "Please choose a style from the list.",
            ),
            (
                {"room": {"room_type": "bedroom", "style": "Modern", "length_m": 1, "width_m": 5}},
                {},
                "Room sides must be between 1.5 and 20 m.",
            ),
            ({}, {"max_products": 1}, "Pick at most 1 products to visualise."),
            ({}, {"max_quantity": 1}, "At most 1 of any one product."),
        ],
    )
    async def test_an_out_of_bounds_selection_is_refused_before_anything_is_spent(
        self, values: dict[str, Any], settings: dict[str, Any], message: str
    ) -> None:
        runtime, parts, sessions = a_runtime(**settings)
        with pytest.raises(SelectionRejectedError) as raised:
            await runtime.visualize(a_request(**values), CONTEXT)
        assert raised.value.public_message == message
        assert parts["generator"].prompts == []
        assert sessions.loads == 0

    async def test_a_stale_screen_is_refused_before_an_image_is_paid_for(self) -> None:
        runtime, parts, _ = a_runtime()
        with pytest.raises(SessionConflictError):
            await runtime.visualize(a_request(expected_session_revision=4), CONTEXT)
        assert parts["generator"].prompts == []

    def test_a_product_is_picked_once_with_a_quantity(self) -> None:
        with pytest.raises(ValidationError):
            a_request(items=[{"product_id": 1}, {"product_id": 1}])


# ── the routes ══════════════════════════════════════════════════════════════


def an_app(
    service: CatalogService | None = None, runtime: CatalogVisualizationRuntime | None = None
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(catalog_router, prefix="/v1")
    if service is not None:
        app.dependency_overrides[catalog_service] = lambda: service
    if runtime is not None:
        app.dependency_overrides[catalog_visualization_runtime] = lambda: runtime
    app.dependency_overrides[retailer_context_provider] = lambda: FakeRetailers()
    return app


async def call(app: FastAPI, method: str, url: str, **kwargs: Any) -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.request(method, url, **kwargs)


class TestRoutes:
    async def test_a_page_comes_back_with_fit_facts(self) -> None:
        service, _ = a_service()
        reply = await call(an_app(service), "GET", f"/v1/catalog/products?store_id={STORE}&q=sofa")
        assert reply.status_code == 200
        item = reply.json()["items"][0]
        assert item["footprint_cm2"] == 20900.0 and item["stands_on_floor"] is True

    async def test_facets_come_back(self) -> None:
        service, _ = a_service()
        reply = await call(an_app(service), "GET", f"/v1/catalog/facets?store_id={STORE}")
        assert reply.status_code == 200
        assert reply.json()["studio"]["render_available"] is True

    @pytest.mark.parametrize(
        "query",
        ["min_price=5", "subcategory=sofa", "store_id=abc", "sort=newest", "unknown=1"],
    )
    async def test_a_malformed_browse_is_refused(self, query: str) -> None:
        service, repo = a_service()
        store = "" if "store_id" in query else f"store_id={STORE}&"
        reply = await call(an_app(service), "GET", f"/v1/catalog/products?{store}{query}")
        assert reply.status_code == 422
        assert repo.filters == []

    async def test_an_unknown_store_is_refused(self) -> None:
        service, repo = a_service()
        reply = await call(an_app(service), "GET", "/v1/catalog/products?store_id=9999")
        assert reply.status_code == 404
        assert repo.filters == []

    async def test_a_selection_render_answers_like_a_chat_turn_without_ids(self) -> None:
        runtime, _, _ = a_runtime()
        reply = await call(
            an_app(runtime=runtime),
            "POST",
            "/v1/catalog/visualizations",
            json=a_request().model_dump(mode="json"),
        )
        assert reply.status_code == 200
        render = reply.json()["presentation"]["render"]
        assert render["source"] == "catalog" and render["room"]["room_type"] == "living_room"
        for forbidden in ("provider", "model", "prompt", "product_id"):
            assert f'"{forbidden}' not in reply.text, forbidden

    async def test_a_selection_of_gone_products_answers_with_our_words(self) -> None:
        runtime, _, _ = a_runtime(rows=[])
        reply = await call(
            an_app(runtime=runtime),
            "POST",
            "/v1/catalog/visualizations",
            json=a_request().model_dump(mode="json"),
        )
        assert reply.status_code == 409
        assert reply.json()["error"]["code"] == "selection_unavailable"
