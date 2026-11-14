# Vocabulary refresh plan

Status: built (2026-11-14). The check compares a full read rather than counting
rows (section 9); D6 needed no separate fix - the values products carry are now
read with the tables, under the same handling. Follows the switch of the vocabulary (product
families, product types, colours, styles, materials) from YAML files to the
database tables the backend keeps (`commerce_category`, `commerce_subcategory`,
`product_attribute_value`).

## 1. The problems, in plain words

1. **The service does not start when the database is down at startup.**
   Before the switch it started anyway and showed "postgres down" on `/health`
   until the database came back. Now it waits two minutes and gives up. (Caused
   by the switch.)
2. **A second startup step has the same gap.** Adding the colours products carry
   but the tables do not have yet does not catch our own "catalog unavailable"
   error, so a database outage can stop startup there too.
3. **New product types wait for a restart.** We read the tables once at startup.
   The backend's import adds a type ("bar-stool") the moment it sees one, but
   chat cannot find those products until our next deploy.
4. **Types still waiting for their Arabic name cannot be found.** The backend
   keeps them in `vocabulary_pending`; the products already carry them, but the
   type is not in the table, so chat cannot search for it.

And one thing that is not a problem but can cause one: **we re-check rows the
backend has already checked**, and refuse to start when one fails. The backend
already guarantees every rule we check (below), so our check can only ever stop
the service, never protect anything.

## 2. What the backend guarantees (read from `backend/core`)

- A type's name is always `lowercase-hyphen`, and it is exactly the value stored
  on products (`clean_value`, `vocab_models.py`).
- Spelling variants are one row: a new value is matched by a key that ignores
  case, spaces, `-` and `_` before it is inserted.
- Every row has an Arabic name: not empty (database check), Arabic letters only,
  at most four words (`arabic_problem`). A value without one waits in
  `vocabulary_pending` and gets its row when the 10-minute job translates it.
- Rows are only added, never renamed or deleted. A family cannot be deleted
  while it has types (`PROTECT`).
- A new type joins only a family that already exists.

## 3. The decisions

### D1 - Trust the tables

The tables are the backend's, checked by the backend, the way a store's prices
are the store's. We stop refusing them:

- Rows are taken as they are. Our extra checks on them (name format, Arabic
  present, spelling duplicates, styles without spaces) are removed from the
  database path.
- Nothing in the three tables can stop the service. In the impossible case of a
  row we cannot use at all, it is left out and logged by name
  (`vocabulary_row_skipped`) - never a refusal to start.
- A value with no Arabic name is shown in English (already true everywhere: cards,
  chips, the room picture reply).
- Two spellings of one value (impossible from the backend) keep the first; the
  attributes refuse twins in their constructor, so the database path merges
  them the way `CatalogAttributes.with_stored` already does.

Our own reviewed files (seating, rooms, pairings, compare groups, dimension
semantics) keep their strict checks: people edit them by hand, and tests catch
a mistake before it is deployed.

### D2 - One vocabulary in memory, swapped whole

Everything built from the vocabulary is kept together in one bundle:

- the taxonomy and the attributes (with the values products carry and the
  waiting values, D4);
- the reviewed files checked against them (dimensions, seating, rooms,
  pairings, compare groups);
- the agent team, whose tools list the allowed values.

The bundle already exists: it is `AppResources`, the app's resources. Every
request reaches the vocabulary through one dependency (`resources` in
`app/api/dependencies.py`), which FastAPI reads once per request, so a turn
always uses one version from start to end. A change builds a new
`AppResources` (`dataclasses.replace` of the vocabulary fields and the agent
team) and puts it in `app.state.resources` in one step; the next request takes
it. No other file reads the vocabulary from anywhere else, so nothing in
`dependencies.py` changes.

The agent team keeps its model clients; only its tools (whose arguments list
the allowed values) and its graph are built again.

### D3 - Check every few minutes, rebuild only on a change

A background task, every `vocabulary.refresh_every_s` (default 300):

1. Read the tables, the waiting list and the values products carry, in the
   background (about 2 seconds against the live database; 79 types and 200
   colours, styles and materials today), and compare their fingerprint with
   the one in use. A full read is cheap enough that no separate "did anything
   change?" query is needed, and it also catches an Arabic name corrected by
   hand, which a row count would miss.
2. Same fingerprint (almost always): nothing else happens.
3. A change: read the tables, build a new bundle (D2), and switch to it. Logged
   (`vocabulary_refreshed`, the old and new version, what was added).
4. The database cannot be read: keep the current bundle, log it, try at the next
   check. Customers notice nothing.
5. The new tables break one of our reviewed files (only possible by a hand edit
   that removes a type): keep the current bundle, log an error naming the file
   and the type, try again at the next check.

Each process runs its own check; for a few minutes after a change two processes
may hold different versions. That is harmless because a change only adds values.

### D4 - Waiting values are live, in English

During each read we also read `vocabulary_pending`:

- a waiting **type** joins its family (the waiting list names it) and is
  searchable, shown in English until its Arabic name arrives;
- a waiting colour, style or material is added the same way (today we already
  add those from the values products carry; that stays for values that were
  never queued, such as "Other").

A waiting type whose family does not exist is left out (the backend drops those
too). The waiting list never holds placeholders such as "null": the backend
rejects them before they are queued.

### D5 - Last good copy, so the service always starts

Every successful read saves the tables, as read, to Redis
(`zory:vocabulary:last_good`, no expiry). At startup:

- database readable: use it (and save the copy);
- database not readable: start straight away from the saved copy, log it; the
  check (D3) picks up the database as soon as it is back;
- neither (the very first start, with the database down): wait and retry as
  today (`vocabulary.startup_wait_s`) - there is nothing to build the agents'
  word lists from.

Empty tables count as not readable.

### D6 - Fix the second startup gap

`_with_stored_values` catches our own unavailable error too (one line), so a
database outage there never stops startup.

### D7 - Caches after a change

- **Catalog overview** (what each store stocks): keyed by the vocabulary's
  version, so it is rebuilt on first use after a change - about a second per
  store, measured. It already expires every 5 minutes on its own
  (`catalog.overview_ttl_s`), so this adds nothing new.
- **The agents' tool lists** change, so the AI provider reads them fresh once per
  agent - the same as after any quiet spell today.
- **Saved cross-sell plans** are labelled by Nora's instructions version only
  (`AGENT_PROMPT_VERSION`), no longer by the vocabulary's version
  (`app/api/dependencies.py`). With the check every few minutes, the old label
  would throw every saved plan away on each import that adds a value, and leave
  a dead copy of each plan in Redis until it expires. Safe because values are
  only added: a plan made before a new colour existed is still right; it just
  does not use it.

## 4. What customers notice

Nothing, except that a new product type can be found within a few minutes of
the backend adding it, and a type waiting for Arabic can be found at once (in
English until its Arabic name arrives).

## 5. Settings

| Setting | Default | Meaning |
|---|---|---|
| `vocabulary.refresh_every_s` | 300 | How often the tables are checked for a change |
| `vocabulary.startup_wait_s` | 120 | Wait for the database at startup when there is no saved copy |
| `vocabulary.retry_every_s` | 5 | Time between those startup tries |

## 6. Tests

Unit:

- a table row the old checks refused (bad name, missing Arabic, spelling twin)
  no longer stops startup;
- database down at startup with a saved copy: starts from the copy;
- database down with no copy: waits, then fails as today;
- a check with no change does not rebuild;
- a check with a new type swaps the bundle; a request that started before keeps
  the old one, the next request sees the new type;
- database down during a check: the current bundle stays;
- tables that break a reviewed file: the current bundle stays, error logged;
- a waiting type is searchable and shown in English; once it has a row, in
  Arabic;
- `_with_stored_values` survives our unavailable error.

Live (store 151 and store 50):

- start with the database stopped and a saved copy: the service starts, `/health`
  shows postgres down, chat works once it is back;
- add a type and a colour by hand in a test database: found in chat within one
  check, no restart;
- the add-path regression set and the query-understanding eval, unchanged.

## 7. Docs to update

- CLAUDE.md 14.1 (read at startup and every few minutes, last good copy, trusted
  rows) and 12.4 (waiting values live in English).
- `docs/known-issues.md`: nothing new; the "null" product types stay with the
  backend team.

## 8. Out of scope

- The backend telling us the moment something changes. The check every few
  minutes is enough for now; it can be added later if minutes are too slow.
- Products whose type is the text "null" (a backend data matter, reported).

## 9. Checked against the code (2026-11-14)

- Every request reaches the vocabulary through the `resources` dependency; the
  only other readers (`comparisons.py`, `labels.py`, the health routes) go
  through it too, and no module keeps a copy of its own. Swapping
  `app.state.resources` is therefore enough.
- The service runs as one process (`uvicorn` with no `--workers`), so there is
  one copy and one background check.
- Tests reach resources by overriding `resources`; two build `AppResources`
  directly (health routes) and are unaffected.
- The catalog overview and cross-sell plan caches are keyed by the version
  (`catalog_capability.py`, `dependencies.py`), so they follow a change with no
  extra code.
- Our database user can read `vocabulary_pending` (empty today).
- The backend never edits a row in code, but a hand edit is possible, which is
  why the check compares content rather than counting rows.

## 10. Order of work

1. D6 (one line) and D1 (trust the tables).
2. D2 (the bundle and the per-request read).
3. D5 (last good copy).
4. D3 (the check every few minutes).
5. D4 (waiting values).
6. Tests, the full gate, live checks, CLAUDE.md.
