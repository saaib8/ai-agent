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
(`gemini-3-pro-image`). The image is stored in S3 and returned as a public URL,
as a chat turn (`ChatResponse.presentation.render`) that is recorded in the
history but changes no state. Configure with `ZORY_VISUALIZATION__*`.

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
