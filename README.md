# ZORY AI Commerce Agent

Customer-facing commerce and interior-design agent service. Standalone FastAPI
application; the engineering contract is [`CLAUDE.md`](CLAUDE.md).

**Status: milestones M0–M2 complete.** The service boots, manages its
dependencies, and reads the catalog under enforced store scope. No agent,
orchestration, taxonomy, discovery or guardrail feature exists yet — those are
later, separately approved milestones.

## Boundaries

The Django platform (`Zory/backend`) owns `core_product`, `core_store` and
every migration that touches them. This service is **read-only** against that
schema and defines no migrations for it. Enforcement is not by convention:

* repositories issue only `SELECT` expressions built from SQLAlchemy Core;
* every connection opens with `default_transaction_read_only` (`ZORY_DB__READ_ONLY`),
  so PostgreSQL itself refuses a write.

### Store scoping is not authentication

`RetailerContext` is resolved by application code, is immutable, and is the
only way a repository learns which store it may read. This prevents accidental
and model-driven cross-store access. **V1 implements no authentication or
authorization, and store scoping is not a substitute for either** (CLAUDE.md 20.7).

## Running

```bash
docker compose up -d                      # PostgreSQL :55432, Redis :56379
cp .env.example .env                      # then edit
uv sync --all-extras
uvicorn app.main:create_app --factory
curl localhost:8000/health
```

`ZORY_DB__DSN` must point at a database that already carries the Django catalog
schema, including `commerce_category`, `commerce_subcategory` and
`seating_capacity`. Startup verifies those columns and **fails loudly** if they
are absent, rather than degrading into queries that silently return nothing.
The bare `docker compose` PostgreSQL has no catalog in it, so point local runs
at a development database (the integration test suite builds its own fixture
schema and does not need one).

### Taxonomy audit

Check live catalog values against the approved global taxonomy. Read-only; it
reports discrepancies and never repairs them:

```bash
ZORY_DB__DSN=postgresql+asyncpg://... python -m app.taxonomy.audit
```

Host ports are deliberately non-default so the containers cannot collide with a
PostgreSQL or Redis already running locally.

There is no module-level `app`: building one would read settings at import
time, so merely importing `app.main` would demand a configured environment.
Hence `--factory`.

## Furniture Finder

Find catalog products like something in a photo. Two endpoints:

* `POST /v1/furniture-finder/photos` (multipart: `session_id`, `store_id`, `image`)
  runs the main platform's object detector (Modal) and returns the objects this
  store sells something in, with outlines to draw over the photo.
* `POST /v1/furniture-finder/picks` (`session_id`, `store_id`, `image_id`,
  `object_id`) describes the picked object with a vision model, embeds the
  description with the model the product index was built with, searches the
  index in the object's category for this store, and resolves the neighbours
  through the store-scoped repository. It answers as a chat turn
  (`ChatResponse`) and commits the products as the list on screen, so "compare
  the first two" in the next `/v1/chat` message refers to them.

The product index (`nora-products-v2`) is **text**: one embedded document per
product (`text-embedding-3-large`, 1024 dims), with `store_id`, `category` and
`product_url` metadata. Matches join to the catalog by `product_url`. Configure
with `ZORY_FURNITURE_FINDER__*` (see `.env.example`); requires the `semantic`
extra.

## Room visualisation

`POST /v1/visualizations` (`session_id`, `store_id`, `view`: `corner` |
`eye_level` | `isometric` | `top_down`) renders the session's current room package - its
pieces, the room's size and style, all read by the application, never sent by
the client. Product photos are fetched (https, public hosts only, bounded) and
sent as references with a versioned prompt (`app/prompts/visualization/v1.py`)
that asks for exactly those pieces and nothing else. The primary image model
renders (`gpt-image-2.5-sunburst`), falling back once to the other
(`gemini-3-pro-image`). Nothing is stored: the image comes back inside the
reply as a JPEG data URL, as a chat turn (`ChatResponse.presentation.render`)
that is recorded in the history but changes no state. It lasts as long as the
customer's browser session. Configure with `ZORY_VISUALIZATION__*`.

## Browse Catalogue

Pick products straight from the store's catalogue, say how many of each,
describe the room, and see it rendered. Three endpoints, all scoped to the
store in the request:

* `GET /v1/catalog/products?store_id=…` pages through the store's active
  products: `q` (words in the English or Arabic name), `category`,
  `subcategory`, `color`, `style`, `min_price`/`max_price` with `currency`,
  `sort` (`featured` | `price_asc` | `price_desc`), `page`, `page_size`.
  Vocabulary values are checked against the taxonomy and attribute registries
  before any SQL runs. "Featured" lists floor furniture first. Each item carries
  its footprint for the fit check, only for floor pieces with a normalised size.
* `GET /v1/catalog/facets?store_id=…` returns the approved categories,
  colours, styles and price range this store actually holds, with counts, and
  the room-setup options and limits (`studio`).
* `POST /v1/catalog/visualizations` (`items`: product ids and quantities,
  `room`: type, approved style and side lengths, `view`) reads the pieces
  back through the store-scoped repository and renders them with the same
  pipeline and prompt as the package render. It answers as a chat turn and
  records it in the history, and it does not change the room package.

The fit check (the share of the floor covered, pieces too big for the room) is
computed in the browser from those footprints. It is advisory only. Configure
limits with `ZORY_CATALOG__*`.

## Product discovery: the question card, picks and comparison

Every new search for a kind of product - "I need a sofa", "find me a sofa",
"I'd like to see some sofas", "show me sofas", or moving on from sofas to
dining tables - is answered with a card of short questions before anything is
searched: the kind (2-seater, 3-seater, L-shape, set...), the budget, colours,
the feel (bouclé, marble, with storage) and the style. The questions per
product family and their kinds and feel words are reviewed data,
`app/taxonomy/briefs_v1.yaml`. Budget bands (price quartiles), colours and
styles are counted from the store's live catalog when the card is built, and
a kind the store doesn't stock isn't offered. Anything they already said is
skipped. A search naming only a category ("I need a table") gets that
category's card with the kind asked first. Refinements ("only beige"), "show
me more" and a typed answer to the card on screen never bring a card. The
decision model only flags a decline (`skip_questions`): "Just show me sofas"
shows results at once, with the card folded beside them as "Narrow down"
(once per family per session).

* `POST /v1/chat` with `search_action: {kind: "brief", card, piece, budget,
  colours, styles, feel}` answers the card with the keys it offered. The kind
  and the budget filter; colours, styles and the feel only rank. The catalog
  has no material field, so the feel is never filtered on or claimed. Results
  show `ZORY_CUSTOMER_AGENT__PRESENTATION_LIMIT` products (5), and the first
  is flagged `best_match` when their own words ordered the list.
* `POST /v1/picks` (`session_id`, `store_id`, `action`: `{kind: "select",
  ordinal, list_revision}` | `{kind: "deselect", pick}`,
  `expected_session_revision`) ticks a card or removes a pick. It is silent.
  `list_revision` names the result list the card is on
  (`presentation.list_revision`), and the latest 7 lists stay tickable.
  `goes_with` is set when the tick picked the first product of its kind that
  has companions the store sells; the client then sends
  `product_action: {kind: "goes_with", pick}`. A pick typed in chat ("I like
  the third one") gets the same offer.
* `POST /v1/chat` with `product_action`:
  * `{kind: "goes_with", pick}` shows the pick and offers the kinds that go
    well with it (a bed → nightstands, wardrobes, rugs) as chips, with "No
    thanks" - nothing is searched until they tap one. Only stocked kinds, most
    stocked first, never a kind already picked.
  * `{kind: "compare", picks: [a, b, ...]}` compares two or more picks (kept
    for typed "compare the ones I picked"; the console compares through the
    pop-up).
  * `{kind: "companion", category, subcategory}` is one of those chips,
    checked against the pairings for the product in focus: a normal page of
    that kind, leaning towards the pick's style.

* `POST /v1/comparisons` (`session_id`, `store_id`, `cards`: two or more
  `{list_revision, ordinal}`) compares the cards checked on the results, for
  the pop-up the console opens from its Compare button once two are checked:
  the table, one column per product, and a short take. Read only - nothing is
  added to the chat or the session. Only similar products compare
  (`compare_groups_v1.yaml`: every type with itself, sofas with
  L-shapes/sets/sofa beds, armchairs with accent/lounge chairs); a set with
  any dissimilar product is refused with `comparison_refused`, and so is one
  over the limit. `GET /v1/compare-groups` serves those families and the
  limit (`max_products`) so the console can grey out cards that cannot be
  compared. Configure the limit with `ZORY_CUSTOMER_AGENT__COMPARISON_MAX_PRODUCTS`
  (default 10, at most 20).

The pairings are `app/taxonomy/complements_v1.yaml`. Companion searches lean
towards the pick's style and carry no budget. Configure the tray's size with
`ZORY_CUSTOMER_AGENT__MAX_PICKS`.

## Configuration

Everything runtime-dependent is declared in `app/core/config.py` and nowhere
else — no module elsewhere reads the environment. Variables are `ZORY_`-prefixed
and nest with `__`, e.g. `ZORY_DB__POOL_SIZE`. See `.env.example`.

In `stage`/`prod`, secret material is additionally loaded from AWS Secrets
Manager as JSON shaped like the settings tree, matching how the Django platform
stores credentials. `boto3` is never imported in `local`/`test`.

## Checks

```bash
uv run ruff check .
uv run mypy
uv run pytest                 # everything
uv run pytest -m "not integration"   # no PostgreSQL/Redis needed
```

Integration tests build the catalog fixture from the same read-only projection
the queries use (`app/db/tables.py`), so the fixture cannot drift from the code
under test. They skip themselves when PostgreSQL is not running.
