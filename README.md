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
