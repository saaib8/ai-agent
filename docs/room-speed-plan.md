# Room design speed plan

## Context
A whole-room turn takes about 75–125 s, close to the 140 s limit. A senior review ("Why it takes so long") found the causes; each was checked in the code:

- **Room searches run one after another.** `DesignDiscoveryService.discover()` awaits each piece in a loop (`app/services/design_discovery.py:118`). This is the 16–44 s `design_room` step.
- **Each piece makes its own network calls.** One embedding call per piece (`app/services/semantic_ranking.py:308`), and the Pinecone chunks run one after another (`app/integrations/pinecone.py`).
- **Fixes sent together still run one at a time.** `AgentLoop` runs tool calls in order (`app/agents/loop.py:199`). Each fix re-reads every pinned piece, one database call per piece: `RoomEngine._pinned_outcome` → `_forced_entry` → `execute_forced_pool` (`app/agents/operations/rooms.py`, `app/services/search_pipeline.py:298`).
- **Reasoning is set per agent, not per task.** Nora's planning, check and final wording all run at medium (`app/integrations/chat_models.py:33`).
- **No timing below tool level.** `agent_turn_completed` logs model and tool times, but not the database, embedding or Pinecone time per piece, nor the reasoning level actually used.

Goal: about 20–40 s off a room turn without weaker designs. Keep the agent structure and medium reasoning for planning; remove the sequential work.

Each phase ships and is tested on its own. Nothing is committed. Each phase runs the gate and a live check; a QA review follows each phase.

## Phase 1: timing logs first (measure before changing)
- **Per-piece timings.** Each piece search logs the SQL pool, relaxation rounds, embedding and Pinecone time in ms, plus candidate count and piece role. One structured event per piece (`room_piece_searched`) from `DesignDiscoveryService._for_need`, with timings collected in `search_pipeline.execute_candidate_pool` (time spent in each stage).
- **Room summary.** `design_discovery_completed` gains a total, the slowest piece, and the sum against the wall-clock time (shows how much is sequential).
- **Model calls.** `TraceEvent` (`app/agents/trace.py`) gains `model` and `reasoning` (the setting actually used), so slow provider calls are told apart from our own work.
- **Rules:** no product names, customer words or ids beyond counts (CLAUDE.md 22).
- **Baseline:** run the 11-room comparison once and keep the numbers (median and slowest per step).
- **Tests:** the events carry the fields; nothing sensitive is logged.

## Phase 2: parallel room searches (the biggest win)
- **Problem:** the search pipeline is built around the request's single `AsyncSession` (`app/api/dependencies.py:121`, `product_search_pipeline(session, …)`). A SQLAlchemy session must not be used by two tasks at once, so `gather()` over the current pipeline is unsafe.
- **Design:**
  - a `PipelineFactory`, an async context manager that opens its own session from `Database.session()` (`app/integrations/postgres.py:65`) and builds a `ProductSearchPipeline` on it, reusing the shared Redis, Pinecone and embedder clients built at startup;
  - `DesignDiscoveryService` takes the factory, and `discover()` runs the pieces with `asyncio.TaskGroup` and an `asyncio.Semaphore(agents.room_search_concurrency)`;
  - results are kept in piece order, so the outcome is identical to today;
  - each task's session closes in `finally`; a failing piece cancels the others, and the error surfaces exactly as today (catalog unavailable).
- **Setting:** `agents.room_search_concurrency`, default 3, min 1. 1 means sequential, which is the rollback switch. One search runs on the request's own session (whose connection it already holds), the rest on fresh ones, so a room holds `concurrency` connections. The documented limit: concurrency × rooms built at once + every other open request ≤ the database pool (`db.pool_size` 10 + `max_overflow` 5). A pool timeout is answered as "catalog unavailable", never a 500.
- **Deadline:** cancelling the turn (`app/agents/deadline.py`) cancels the TaskGroup, so no task outlives the turn and the existing `catalog_reset` still applies.
- **Tests:**
  - the same plan gives the same pieces, in order, for concurrency 1 and 3 (fake pipeline);
  - concurrency never exceeds the setting (a counting fake);
  - a failing piece cancels the rest and every session is closed;
  - a cancelled turn leaves no running task.
- **Measure:** `design_room` before and after, from the phase 1 logs. Expected: 16–44 s → about 6–15 s.

## Phase 3: fewer network trips per room
- **One read for pinned pieces.** `_pinned_outcome` collects every pinned product id and hydrates them in **one** `hydrate_ids` call, then builds each piece's one-product pool from that result. New `execute_forced_pools(ids)` beside `execute_forced_pool` (`app/services/search_pipeline.py`). The same store scope is applied, and a missing product is still an empty pool.
- **Batch the embeddings.** `QueryEmbedder.embed_queries(texts)` (`app/integrations/embeddings.py`) sends one API call with many inputs, checks every vector's dimension, and keeps the same errors as `embed_query`. Room discovery embeds all piece texts first, then ranks each piece with its vector. Ranking for a single search is unchanged.
- **Pinecone chunks** of one pool are scored concurrently, with the same query vector and no candidate dropped (CLAUDE.md 16.1). This only matters for large pools, so do it only if the phase 1 logs show chunking happens.
- **Tests:**
  - forced pools equal the old one-by-one result, and a missing product gives an empty pool;
  - `embed_queries` keeps order, checks dimensions, and maps errors the same way;
  - room results are unchanged with batching.

## Phase 4: corrections as one operation
- **New engine operation `revise_pieces(revisions)`** (`app/agents/operations/rooms.py`):
  - takes the room snapshot once (every unchanged piece pinned);
  - searches all revised pieces together (the phase 2 concurrency);
  - costs the whole room once and commits once;
  - is never half-applied: either all the revisions land or none do.
- **`revise_room_piece` (`app/agents/tools/design.py`)** takes a list of up to 2 revisions. Nora sends both in one call; the single form keeps working. `remove_room_piece` in the same round stays a separate, cheap call.
- **Prompt:** one call with every revision. The two-revision limit is kept.
- **Tests:** two revisions give the same pieces as two calls in sequence would; both are refused when one fails; locks are kept; one commit.
- **Measure:** about 3–5 s saved on turns with fixes.

## Phase 5: cheaper final wording
- **Problem:** Nora's settings can't tell planning from wording.
- **Design:**
  - the loop picks the reasoning per model call;
  - a call made **after** this turn's room was built and its fixes applied (only the reply is left) uses `agents.nora.wording_reasoning_effort` (default unset, the design's own effort: low leaked the work behind the room);
  - every other Nora call stays at her effort (medium);
  - `build_chat_model` builds the second model variant at startup, so nothing is created per call;
  - the supervisor is already low.
- **The flag is code-decided, not a guess:** `TurnTrace` records that `design_room` ran and that a review round (`revise_room_piece` or `remove_room_piece`) completed. The next Nora call is the wording call. When no fix is needed, Nora reviews and replies in the same call, and that call stays medium.
- **Tests:** model choice per step (review round done → low; otherwise medium).
- **Evals:** the 11-room run, replies compared for quality (layout described, colours, compromises, budget left) and time.

## Phase 6 (only if logs show it): factual checks before Nora's review
Most are already in the review facts: colour, `size_in_range`, planned and used share, `colours_in_room`, seats, `budget_left`, why a piece is missing. Add what the logs and replies show is still missing, for example:
- an unknown size flagged;
- a one-line summary of what's off (sizes out of range, colours asked but absent).

Nora reads it first and judges only taste and layout.

## Out of scope
- Lowering reasoning for planning, or globally: the review advised against it.
- Changing the agent structure.

## Verification
- **Gate:** `.venv/bin/ruff check app tests && .venv/bin/mypy app && .venv/bin/pytest tests/unit -q`.
- **Live:**
  - restart the backend preview (port 8001);
  - run the 11-room comparison (`rooms_v2.py`) and the step-by-step chat;
  - compare against the phase 1 baseline: per-step medians and slowest, total turn time, timeouts, budget used, pieces, and reply quality.
- **Load check:** two rooms built at once with concurrency 3 stay within the database pool (no pool timeouts in the logs).
