# ZORY AI Commerce Agent - V1 Engineering Instructions

This file is the implementation contract for Claude Code when working on the ZORY AI Commerce Agent V1.

The goal is not to produce the shortest implementation. Build the system as a senior backend/AI engineer would: explicit boundaries, typed contracts, deterministic business logic, safe tool access, configurable runtime behavior, strong tenant/store scoping, testability, observability, and clean failure modes.

Do not trade architecture quality for speed unless the user explicitly asks for a temporary prototype.

---

## 1. Product Context

ZORY is an AI commerce and interior-design agent embedded in furniture-retailer experiences.

V1 must support two primary journeys:

1. Single-product discovery
2. Whole-room furnishing/design using products from the active retailer's catalog

The customer sees one conversation, answered by a team of agents (section 17):
a supervisor that delegates each turn, and four tool-calling agents - Sara
(shopping), Nora (interior design), Salman (sales) and Faysal (store support) -
each its own model with its own tools, sharing one memory. Nora is the
interior designer: the design judgement - what goes, where it stands, what a
room holds - is hers, written as checked tool arguments (there is no separate
design model).

Facts, scope and money stay in deterministic services the agents reach only
through typed tools. Do not add an agent where a tool would do.

---

## 2. V1 Scope

Implement or prepare for the following V1 components:

- FastAPI application
- Customer / Commerce Agent
- Interior Design Agent
- LangGraph orchestration
- Redis short-term session state
- Retailer/store context
- Catalog capability service
- Commerce taxonomy registry and deterministic taxonomy validation
- Product discovery service
- PostgreSQL structured filtering
- Pinecone semantic retrieval/ranking when needed
- Constraint classification and controlled relaxation
- Ranking service
- Comparison service
- Whole-room furnishing planner
- Budget allocation / bundle optimization
- Guardrail and policy layer
- Structured logging, tracing, metrics-ready instrumentation
- Unit, integration, and agent evaluation tests

Explicitly out of scope for initial V1 unless requested later:

- Authentication and authorization
- Long-term user memory
- S3/raw conversation archive
- WhatsApp
- Voice
- Checkout/payment integration (the cart is in - section 36)
- Post-purchase support
- Visualization/image-generation pipeline
- Furniture Finder / image similarity UX expansion
- Full customer identity/profile system

Design boundaries so these can be added later without rewriting core services.

---

## 3. Non-Negotiable Engineering Principles

### 3.1 Do not hardcode runtime configuration

Infrastructure and runtime behavior must come from validated settings/environment configuration.

Use `pydantic-settings` for configuration loading and validation.

Examples that must be configurable:

- database URL and pool settings
- Redis URL and TTLs
- OpenAI API key
- model names
- model timeouts/retries
- Pinecone API key/index/namespace configuration
- candidate limits
- semantic top-k
- semantic-score thresholds
- relaxation thresholds
- relaxation budget percentages
- request/message limits
- feature flags
- logging level
- API prefix
- environment name

Do not scatter `os.getenv()` calls throughout the codebase. Centralize settings.

Large domain data such as taxonomy definitions, retailer catalogs, or per-retailer product availability does not belong in environment variables. Store it in the database or a versioned domain configuration source. Environment variables configure the application; they are not a substitute for domain data.

### 3.2 No business logic in FastAPI routes

Routes should only:

- validate transport-level input
- resolve dependencies/context
- call application/orchestration services
- map output to API responses

Do not put search, ranking, prompt, retailer, guardrail, or agent logic in route handlers.

### 3.3 LLMs reason; services establish facts

LLMs may decide what should happen next.

They must not be the source of truth for:

- product existence
- price
- dimensions
- store ownership
- category availability
- catalog facts
- product URLs/images
- exact compatibility rules that can be validated deterministically
- which commerce taxonomy values exist

Facts must come from repositories/services/tools.

The division of responsibility across the whole system is:

```text
THE LLM UNDERSTANDS THE CUSTOMER'S LANGUAGE.
THE TAXONOMY REGISTRY DEFINES WHAT VALUES ARE ALLOWED.
SERVICES / DATABASE DEFINE WHAT FACTS AND PRODUCTS ACTUALLY EXIST.
```

An LLM may interpret what a customer means. It may never decide what a valid
category is, and it may never decide what the catalog contains.

### 3.4 Agents never receive unrestricted database access

Never expose arbitrary SQL execution as an agent tool.

Agents call typed domain tools such as:

- search_products
- compare_products
- get_catalog_capabilities
- get_product
- design_room

Repositories own SQL.

### 3.5 No free-form agent-to-agent conversations

The agents reach the Interior Design Agent only through typed tools, never in
free text to one another, and hand turns to each other only with a typed reason
(17.1).

The Interior Design Agent returns structured outputs that the tools and domain
services consume.

### 3.6 Prefer deterministic services over agent reasoning

The following should be normal Python services unless there is a clear reason otherwise:

- taxonomy resolution
- product search
- filtering
- controlled constraint relaxation
- ranking
- comparison
- catalog capability lookup
- budget calculations
- bundle optimization
- retailer/store scoping
- output allowlisting
- guardrails

### 3.7 Avoid premature microservices

The V1 agent backend is a standalone FastAPI service, but its internal components should be modules/services inside one deployable application unless a component has a real independent scaling or lifecycle requirement.

---

## 4. Existing System Boundary

ZORY already has an existing Django/DRF backend and PostgreSQL product catalog.

The new AI Agent service is a separate FastAPI service.

The Django system remains the owner of the existing product/catalog schema and catalog writes unless explicitly changed later.

The FastAPI Agent service should treat the current product catalog as read-oriented application data.

Do not let the FastAPI service start owning migrations for Django-owned tables such as `core_product` without an explicit architectural decision.

If existing catalog columns must be added, confirm which repository owns the Django model/migration and make the change there. Do not silently create competing Alembic migrations against the same table.

---

## 5. Current Product Data

The currently visible `core_product` data includes:

- `id`
- `uuid`
- `name_arabic`
- `name_english`
- `price_amount`
- `price_unit`
- `image_url`
- `product_url`
- `is_active`
- `store_id`
- `category`
- `pinecone_id`
- `file_id`
- `time_created`
- `time_updated`
- `detection`
- `dimension_unit`
- `length`
- `width`
- `product_color`
- `two_d_icon`
- `height`
- `three_d_model`
- `main_color`
- `secondary_colors`
- `styles`
- `salla_product_id`
- `room_types`
- `style_tags`

Before implementing repositories/models, inspect the real database/model definitions and confirm exact SQL types, nullability, indexes, relationships, and semantics. Do not infer types from names or screenshots.

`material` is an approved authoritative product field and will be provided by Django on `core_product` (see section 6). Until that column ships, treat material as unavailable rather than deriving it from names, styles, or any other column. Do not invent a storage location and do not build a substitute.

---

## 6. New Commerce Classification Fields

The existing `core_product.category` is a visual classification: it represents what the product looks like in the image.

Do not rename, repurpose, or overwrite it.

Separate commerce classification fields are provided by Django on `core_product`:

- `commerce_category`
- `commerce_subcategory`
- `seating_capacity` nullable integer
- `material`

Purpose:

- visual `category` = what the item looks like
- `commerce_category` = broad commercial shopping family
- `commerce_subcategory` = what the merchant is actually selling
- `seating_capacity` = numeric capacity only when explicitly known
- `material` = the product's authoritative material

### 6.1 These fields are reviewed data, not a runtime derivation

`commerce_category`, `commerce_subcategory`, `seating_capacity` and `material`
are **authoritative reviewed commerce data**. They are produced by the external
product-data preparation process, which is outside the FastAPI agent runtime.

The agent service consumes the final reviewed values directly.

The agent service therefore does **not**:

- own or maintain a visual-category-to-commerce mapping
- derive commerce fields from the visual `category` at runtime
- fall back to the visual `category` when a commerce field is missing
- re-classify, correct, or enrich product rows

A product whose commerce fields are absent or unknown is a data-preparation
matter. The agent service reports what the catalog actually contains; it does
not fill the gap by guessing.

Preserve existing names where possible. Do not rename stable product concepts simply for stylistic consistency.

### 6.2 The taxonomy is a controlled vocabulary

The approved V1 commerce taxonomy defines the `commerce_category` ->
`commerce_subcategory` relationships that ZORY understands. It is the
authoritative vocabulary of product types for both product data and query
understanding.

Nothing - neither a classifier nor a conversational model - may introduce a new
category or subcategory at runtime. See section 14.

`seating_capacity` is numeric capacity only when explicitly known; a sofa with
unknown capacity is `NULL`. Never guess it, and never infer it from appearance
or width (see section 31).

---

## 7. V1 Seating Vocabulary

Keep the seating taxonomy intentionally compact.

Approved commerce subcategories include:

- `chair`
- `dining-chair`
- `lounge-chair`
- `office-chair`
- `recliner`
- `stool`
- `outdoor-chair`
- `single-seater-sofa`
- `sofa`
- `sectional-sofa`
- `sofa-bed`
- `sofa-set`
- `chaise-lounge`

Scope of the vocabulary - what these subcategories are intended to cover:

- `chair` covers accent chairs, armchairs, occasional chairs, guest chairs, and common living-room complementary chairs
- `lounge-chair` covers seating whose core function is prolonged comfortable sitting, such as a reading chair
- `single-seater-sofa` covers a one-seat sofa or sofa chair
- 2/3/4-seater are values of `seating_capacity` on `sofa`, not separate commerce subcategories

This is a statement of what each approved value means, so that both data
preparation and query understanding read the vocabulary the same way. It is
not an alias table and must not be implemented as runtime string mapping.

Do not expand chair subcategories without evidence that the distinction materially changes search/buying intent.

**How many a type seats is reviewed domain data** (`app/taxonomy/seating_v1.yaml`,
loaded and validated at startup). Every chair type, `stool` and
`single-seater-sofa` seat one person; `sofa`, `sectional-sofa`, `sofa-set` and
`sofa-bed` seat two or more. `recliner` and `chaise-lounge` are in neither list
until confirmed.

- A one-seat type carries no seat filter: the catalog records no capacity for
  them, so a filter could only ever hide them all. This is the one place a
  missing capacity is not "unverified" (13.5) - the type itself answers it.
- Combinations (27.1) use the reviewed `combination_main` types (sofa,
  sectional, sofa set) as main pieces - a sofa bed only when the customer asked
  for sofa beds - and the reviewed `combination_extras` (chair, lounge chair,
  single-seater sofa) as extra seats.
- A seat count of one on a multi-seat type is a misreading: what they want is
  a one-seat type ("make them single seaters"), which the agent searches
  instead.

---

## 8. Retailer / Store Context Is First-Class

The entire application must be easy to switch from one retailer/store to another without code changes.

For V1, `store_id` from the existing catalog is the mandatory catalog-scope key unless the existing domain already provides a separate authoritative retailer entity. Do not invent a second tenant identifier unnecessarily.

Every request must resolve a `RetailerContext` / `StoreContext` before agent execution.

Example conceptual contract:

```python
class RetailerContext(BaseModel):
    store_id: int
    currency: str | None = None
    supported_categories: dict[str, list[str]]
    feature_flags: set[str] = set()
```

The actual schema should stay minimal and evolve from real requirements.

The agent must never be responsible for remembering or choosing `store_id`.

Once request context is created, store scope is server-controlled and injected into repositories/tools.

Do not let the LLM pass arbitrary store IDs into product tools.

---

## 9. Catalog Capability Service

Whole-room planning must know what the active retailer actually sells before creating a furnishing plan.

Implement a `CatalogCapabilityService` that can answer, for the active `store_id`:

- supported commerce categories
- supported commerce subcategories
- active product counts
- optionally useful price ranges
- optionally room-relevant category availability

Derive capabilities from active catalog data rather than hardcoding retailer capabilities.

At minimum, capability queries must include:

- `store_id = current_context.store_id`
- `is_active = true`

Cache catalog capability summaries in Redis when useful, with configurable TTL and safe invalidation/expiration behavior.

Do not send the entire product catalog to an LLM.

The Interior Design Agent gets a compact capability summary; Product Discovery gets actual products.

### 9.1 Three distinct things that must never be conflated

- **The global commerce taxonomy** - every product type ZORY understands. Defined by the taxonomy registry (section 14). Independent of any retailer.
- **Retailer-supported categories** - what the active retailer actually sells, derived from live catalog data for the current `store_id`.
- **What happens to be present in a dataset** - for example the initial reviewed records used during development.

A type missing from a test or seed dataset is not removed from the global
taxonomy, and its absence does not by itself establish that the retailer does
not support it. Capability statements must come from a scoped query against
live catalog data, not from dataset observation.

---

## 10. Rooms, Searches and the Conversation

The agents decide the conversation: what to ask and when, what to show, and
what comes next (section 17). Their instructions are
`app/prompts/agents/v2.py`. Code decides none of it; code holds the facts, the
scope, the money and the reviewed domain data below, and refuses what breaks
them. What follows is what the tools guarantee, and what each agent is told to
do with it.

Reviewed domain data, loaded and validated at startup:

| File | What it says | Used by |
|---|---|---|
| `app/taxonomy/room_pieces_v1.yaml` | the pieces each room kind often holds, each essential, recommended or optional - a guide Nora reads, never a rule | `store_shelf` (`in_this_room`), `room_could_add` |
| `app/taxonomy/seating_v1.yaml` | how many each seating type seats; which never count as seats (chaise lounge, recliner); which types stand beside a sofa | seat filters, a room's seats (27.1), `store_shelf`, mixed sofa searches |
| `app/taxonomy/complements_v1.yaml` | the kinds reviewed to go with each kind | `what_goes_with`, `room_could_add` (`usually_paired`) |
| `app/taxonomy/compare_groups_v1.yaml` | which kinds compare with which | `compare_products` |
| `app/taxonomy/subcategory_questions_v1.yaml` | the questions the store's designers ask about for each kind (the subjects; the answer options are the agent's to decide) | `ask_about` and `search_products` facts (`a_designer_might_ask`) - a baseline the agent rephrases in its own words, choosing the options itself, never a script |

`app/taxonomy/briefs_v1.yaml` is kept but no longer loaded; only its question
kinds (`BriefQuestionKind`) are still imported.

### 10.1 A whole room (Nora)

Nora designs the room; code finds the products, does the sums and keeps the
rules. A room is a path of its own: the customer sees the room package, never
a list of search cards while it is built.

- **Asking first, one question a turn**, only what changes the design and the
  brief does not hold: the budget (only when unknown; saved with `note_room` the
  moment it is said), what the room is mostly for (`note_room` `purpose` -
  hosting guests, family TV, reading, children), the room's size once and why
  ("I can design your room better if I know its size" - declined is `note_room`
  `declined_size`, never asked again), how many sit there, the colours (several
  may be given) and look, what they must have, would rather not have, or already
  own. The budget, purpose, head count and colours or look are known before it is
  built; "just design it" builds now. An amount said without a currency is in the
  store's (`store_currency`).
- **What the store stocks** is in Nora's context while a room is planned
  (`store_stocks`: the kinds by family, names only - a few dozen words), so she
  plans without a tool call. `store_shelf` (Nora, Salman) gives a kind's price
  range, colours, styles and seats when asked (`range_depth` few, some, many -
  never a count; `seats_counted: false` for a chaise lounge or recliner). The
  room guide's `in_this_room` only marks kinds such a room often holds - never
  first or last; any stocked kind may belong. Cached in Redis
  (`catalog.overview_ttl_s`). (The whole shelf in her context was tried and made
  each design step several times slower.)
- **Nora plans the whole room first, then `design_room` builds it** (Nora
  alone), with no default shape: the layout in her words (`layout`, kept as
  `room_project.layout_idea`) - two sofas facing, an L, a U, majlis seating, a
  sofa with chairs, a reading corner, whatever the purpose, people and size
  call for; a palette (up to five colours from the approved list) and styles;
  `same_room`; the head count; and every piece - its role, the types that may
  fill it (one family; several when they would do equally), how many, must or
  nice, its colours, styles, what to avoid, its feel, size ranges (longer and
  shorter side), `seats_each` for a type that seats several, the wall it goes
  on, what it stands beside, `share_percent`, and `from_pick`. Everything is in this one
  plan, so the room is shown complete in one reply: the pieces it needs are
  must, finishing pieces (lamps, a ceiling light, a console, art, statues, a
  small vase, a plant) are nice - included when the budget allows, given up
  first when it is short; pairs and sets are a quantity, bought whole or not
  at all. No second build: budget still left is said, with what could finish
  it offered for the next message. `app/services/room_design.py` checks it: approved types
  only (a refusal names the family), unstocked types and looks left out and
  said, `seats_each` only on multi-seat types, shares at most the whole budget,
  no figure in prose, and a seat count no piece of its types has in this store
  refused with what each multi-seat type seats here (`no_piece_seats_that_many`,
  `seats_here`), so she re-plans - several straight sofas as readily as one corner
  sofa. An alternative is another type of the same piece, never a backup piece. The
  seats it gives are a fact, never a refusal (`seats_short`, `seats_over`).
- **Found by fit and look first, then the money** (27): each piece is one search
  across its types with no price bound - a room's pieces searched side by side,
  one on the request's session and the rest each on a session of its own
  (`agents.room_search_concurrency`), the room the same as one after another;
  their ranking texts embedded in one request beforehand and the pinned pieces
  of a change re-read in one query. Its colours and styles (else the
  palette) are search wording, ranked by meaning in Pinecone - never a stored
  name matched first, so a Sage rug can serve an olive room (12.4 / 16.1 stay
  for Sara's searches). Code orders only by facts - a type that counts as seats
  where its types differ, size in range, nothing to avoid, the type she listed
  first, relaxation depth - and the meaning order decides within. Nothing is
  excluded for fit. The optimiser keeps must before nice and never exceeds the
  budget; the best match the room can afford stays the pick, and a share only
  moves a piece **up** to a dearer product of the same standing whose look
  similarity is within `agents.share_upgrade_similarity_margin` of it - never
  down, never to a weaker look, never without a score.
- **Fit is Nora's judgement**, for the feel of the room, not a floor plan: she
  reads each piece's listed size beside the room's size and walls and her
  rules of thumb (a path through the room, space at the low table and beside
  the bed, the way in and windows clear, open floor left), swaps what does not
  sit naturally, and speaks of fit in rough terms ("about a metre"). No code
  check, no exclusion. `measure_fit` answers a customer's direct "will it fit?".
- **Nothing internal reaches the customer** (every agent): no tool, step, search,
  plan, filter, data field or handover - only what a salesperson or designer would say.
- **Presented as the best choice for their plan**: every piece with why it suits how
  many sit there, the room's purpose, their colours, size and budget - never the work
  behind it ("planned", "wasn't available", "instead"); only what they asked for and
  could not have is said, leading with what was chosen and why it works.
- **Checked before shown**: a room `design_room` built is drawn only when Nora
  replies with `show: "room"`. Each piece comes back with its actual colour and
  styles, its listed size and `size_in_range`, its share planned and used, the
  room's colours in one line (`colours_in_room`), the seats and each missing
  piece with why (not stocked, or not within the budget with the lowest real
  price) - whether it works and carries their colours is hers to judge. A piece
  that is not right is `revise_room_piece` - that piece found again, every other
  piece pinned. A piece she judges wrong is put right before the room is shown, in
  one round: one `revise_room_piece` call with every piece to put right (at most
  two), found again together and committed once - all or none - then the room is
  shown; a colour they asked for that the room misses is brought in through a small
  piece. Finishing pieces are decided in the first plan, enough to use the budget, and
  never removed for their look - put right or kept as the nearest and said; a removal
  is only for a real fit problem. No room is one colour: the customer's colours lead
  a designer's pattern (base, secondary, accent, a grounding wood or dark tone), and
  art and accents may contrast, tying back to a room colour. Options are never fetched while
  building.
  And the room as a whole: `budget_left` (summed in code) and, when budget is
  left, `could_still_add` - the stocked kinds whose cheapest piece fits what is
  left (kinds it already has stay on the list with `in_room`), marked when the
  guide lists them, when they pair with its pieces and when they come in its
  colours. Budget left buys dearer pieces of the same look first, then
  accessories - statues and antiques, small vases, a console, a chandelier,
  pendants, wall lights, mirrors, plants; ceiling and wall lights and what sits
  on a surface take no floor. A spare room stays spare and says so. Salman
  offers finishing touches within `budget_left` first (10.3).
- **The palette is the room's** (`room_project.palette`), apart from the
  customer's own preferences: a simple search never leans to a room's palette.
- **Styling** - small decorative pieces, only kinds the store stocks (`room_could_add`),
  never a fixed list - is offered after the room, on
  top of its budget (Salman, 10.3), unless they said everything must fit
  within it, when it is part of the design. Lighting, rugs, wall art and
  statement decor are pieces of the room. Pieces they own are designed around.
- **Built around their picks**: `from_pick` makes a pick that piece; with
  `around_picks` every pick of one of the room's types is locked in; locks
  keep their seats and their prices count against the budget.
- **Refine, never replan**: piece options and swaps, `revise_room_piece`,
  remove, lock or mark owned (taken off the bill; nothing else re-chosen), or
  `design_room` again with `same_room` for a new budget, size or look. A
  locked piece is kept by every later build. A change that would go over
  budget is held, never applied (`swap_budget_offer`): the reply says what the
  room would total (`room_total_if_accepted`) and asks; yes is
  `accept_over_budget` (the budget rises to the new total), no is
  `decline_over_budget` and cheaper options for that piece. A change to several
  pieces at once - two products in place of a pair, several finishing touches -
  is one `change_room`: what comes out and what goes in, costed once with every
  other piece pinned, and over budget held whole (`swap_budget_offer`
  `removed_pieces`, `added_product_ids`), so one yes applies all of it.
- **A piece's options keep its size floor**: the smallest it was designed to be for
  the people using it (`kept_at_least_cm`), applied where the data can answer it - a
  couple's bed is never offered as a single; Nora lowers it only when the customer
  opens up (`shorter_at_least_cm`, `longer_at_least_cm`), and shows only options she
  would recommend.
- **A piece's options are for the room** (`product_source: "room_options"`,
  `options_for_piece`): their card action is "Use this", a swap into the room.
  Neither `add_to_cart` (`option_is_for_the_room`, unless `instead_of_room`)
  nor a tap on `POST /v1/picks` (`room_option`) puts one in the cart.
- **Finishing touches**: `room_could_add` lists every stocked kind the room
  could take - those it has none of first, then the widest choice, kinds it
  already has with `in_room`; the guide's kinds and reviewed pairings are
  marked, never put first. The agent chooses, shows one with `search_products`, and `add_to_room` adds it - any
  stocked product, every other piece pinned, over budget asked first as above.
- **Their room, fit and placement** are Nora's (10.6).

### 10.2 A product search (Sara)

Sara leans to asking first - a few questions up front make the journey feel
personal. A vague kind ("a table", "a light") is a question - which kind - never
a guess (14.5). For a new kind, whether they state a need ("I want a recliner")
or ask to see it ("show me sofas"), she `ask_about`s it first - a cheap tool that
resolves the kind and returns the store's reviewed designer questions for it
(`a_designer_might_ask`: the subjects only, no answer options) and what this
store carries of it (`this_store_has`: its colours, styles and seats from the cached
catalog overview, never its prices; `kinds_stocked` before the kind is chosen), with
nothing searched - then asks **two or three** before showing anything, **one a
reply**, one after another, in her own words: two when that is all the kind
needs, three when more would change what she shows. She chooses the answer
options herself, never a fixed list: colour, style, seat and kind options only
from what the store carries; a feature the data does not hold (power, massage) is
asked as a wish that leans the search, never promised. Each answer moves to the
next question still open, never one already answered; what they said counts, so
"a beige sofa" leaves fewer to ask, and when they already named enough ("a
3-seater beige sofa under 4000") she shows at once. A kind chosen from her own
question (which kind of table, an alternative she offered) counts as an answer.
Once those questions are answered she searches with everything they said and
shows; "just show me", a decline or impatience shows at once.
Never the budget as an opener - budget comes once they are engaged (comparing,
choosing, talking price). Once products are on screen the rhythm drops to **at
most one follow-up a turn, or none**: the same questions ride on a
`search_products` result, for the one that narrows the results already shown.

- **Their words, as the search.** `search_products` takes the whole search each
  time, in approved values only (14). Colours and styles are preferences unless
  they are strict (12.4) - and asking for one again, insisting on it, or turning
  results down for lacking it makes it strict for that search and its pages
  ("more beige ones" after a mixed page means beige only). A budget is a limit
  unless softened ("around").
- **Colours and styles are plain lists.** An agent's search gives
  `colours_required` / `colours_wanted` and `styles_required` / `styles_wanted`,
  each a list of approved values; code writes them as the interpretation's
  attributes (required -> locked, wanted -> preferred), so one contract reaches
  query understanding. Every search reports what actually ran
  (`searched_for`: kind, colours, styles, seats, budget, sizes, words - with
  the taste and sizes code added), and the agent checks it against what was
  asked before showing anything.
- **"More like this" keeps the look.** The product's own colour and styles are
  requirements of the search (`colors_any_of`, `styles_all_of`), lifted only as
  the last resort of 12.4 and then said plainly.
- **Knowing when it has run out.** Every search says whether anything not yet
  seen still matches (`more_after_this_page`). When nothing does, the reply says
  these are all of them and offers the nearest alternative as a question -
  another size, a close shade - never a page filled with what was not asked.
- **Taste** they say - likes and what they would rather avoid - is kept with
  `remember_taste` (`customer_preferences`): likes lean every new search, what
  they avoid is pushed down in every search and its pages. Nothing is hidden.
- **Head count orders, seats filter.** How many usually sit there is
  `head_count`: pieces that seat them first, unknown seat counts next, fewer
  last. A seat count they require ("a 3-seater") is the search's seats.
- **Sizes stay with their kind** (13.5): sizes given for a kind come back on
  its next search that names none (`earlier_sizes_applied`); "any size is fine"
  is `earlier_sizes: false`. A search that is ours - what goes with a pick, more
  like this - neither restores nor saves sizes.
- **A sofa search shows sets and sectionals beside sofas**
  (`customer_agent.mixed_types`, `seating_v1.yaml` `shown_with`), with every
  other constraint alike, unless they asked for the type alone
  (`with_related_types: false`) or gave a size those listings cannot answer.
  Among equally good matches the cards take turns by type; an explicit sort is
  never interleaved.
- **The reply** picks out one or two by their place on screen and says why.
  It calls a card the best match only when their own words ordered the list,
  never beside a price sort, and never says how many products the store holds.
  A widened search, a lifted strict colour or style, or a request nothing met
  is said plainly, with what setting a requirement aside would find (13.5).
- **Comparing** is between similar kinds (`compare_groups_v1.yaml`), up to
  `comparison_max_products`; the server refuses a mixed set.
- **The card buttons**: Select adds to the cart (36); Like keeps it in the
  liked list (`max_likes`, a quiet taste signal, not a pick); More like this is
  the similarity search.
- **A group no single piece seats** ("sofas for 10") is chosen one piece at a
  time, never as a ready-made combination: Sara opens a seating goal (10.5),
  asks what the first piece should seat, and after each add the goal says how
  many seats are left; the next piece is searched to match what they chose
  (`match_goal`). Before the second piece Nora asks the room (10.6) - two sofas
  must fit together (she matches and sizes the next seat, so the room is hers to
  ask).

### 10.3 After a pick (Salman)

Salman comes in when what they set out to choose is chosen - a goal met, or a
pick with no goal open - never in the middle of one: a first sofa of ten seats
is followed by the next sofa, not a rug. With the room known, Nora's
`next_sizes` and `placements` come first: the next piece is searched at her
size (`designer_size`). Otherwise `what_goes_with` without a kind lists the
reviewed pairings the store stocks, most stocked first, no counts - and, for
a piece that seats several (a sofa, sectional or sofa set), another of its own
kind (`another_of_its_kind`: a second sofa for two facing, an L or a majlis),
shown with `more_like_this`; no other piece's own kind is listed. A kind
with no reviewed pairing the store stocks gets the stocked kinds instead
(`none_reviewed`), so the offer never ends empty; Salman chooses one, or another
kind `store_shelf` lists, and calls it again with `show_kind`: a page of that
kind leaning to the piece's look. It is a reason it could suit, never "usually
bought together" and never a verdict that it goes - whether it goes is Nora's.

After a room is in the cart he offers its styling, on top of the room's budget,
and `finish_room` asks in his own words which room is next. When a room-defining
pick - a sofa, a bed, a dining set, anything a sale could grow a room from (the
agent's judgement; a lamp or a vase is not one) - is in the cart and no goal is
still open, Salman offers the whole room **alongside what goes with it**, as one
choice: to design the rest of that room around their picks. A seating goal still
open finishes first - the offer waits until it is met. On yes Nora designs it
with `around_picks`.

### 10.4 What every agent is told, and what the screen shows

Each turn every agent gets the session brief (`SessionBrief`,
`app/schemas/agent_brief.py`, built in `app/agents/views.py`): the cards on
screen; the cart with its lines, quantities, line totals and totals per
currency (summed in code) and the room each line was bought for; every budget
on record and what it applies to; the search as it ran; the room - what they
told us - size, walls and what is on them - and, once built, its plan; their
goals with progress counted in code; finished rooms; liked products; their taste;
saved sizes; what waits on their answer (seating options with their pieces and
totals, an over-budget offer with the room's total if accepted, options for a
room piece). Products are named by place, never by id; in Arabic each card's
`name_ar` is the name on the customer's card. Sizes are a piece's longer and
shorter floor side and its height (15.1). Earlier replies show who wrote them
and the answers they offered (`[offered: ...]`).

The screen is built from what this turn's tools produced
(`app/agents/screen.py`), never from the reply's words. The reply's `show`
chooses among them - never something shown before the turn's last list, which
is the list "the second one" means - and the last thing shown is the default.
Chips (`choices`) must answer the question the reply asks; a reply that offers
them without a question is sent back as broken arguments. The store's support
channels are drawn from its contacts when `contact_store` ran.

### 10.5 Goals: what they are working toward

`AgentStateV1.goals` holds what the customer is trying to finish - seat ten
people (`seating`), furnish a room (`room`, whose engine is `room_project`), a
rug for the sofa (`piece`) - each `open`, `done` or `dropped`, with its members:
the cart products chosen for it (`add_to_cart` `for_goal`). Sara, Nora and
Salman keep them with `update_goal`: open, revise, done, drop - their
judgement, never a reflex. Code only counts progress for the brief: for
seating, the seats its members give (reviewed capacities only - a product with
no recorded seats counts none) and the seats left. The next step comes from the
open goals: an unmet goal first, and only when it is met, what goes with it
(10.3).

**Matching** what they already have is Nora's: `search_products` with
`match_with` (cards, picks, liked), `match_goal` (a goal's members) or
`match_room` and her `direction` - colours and styles that sit with theirs,
what to avoid, a size in proportion to the first anchor (`size_ratio`) and why
in words. Code keeps it to what the store stocks for that kind
(`direction_dropped`; `looks_unknown` when that cannot be read) and turns it
into a ranking lean that orders and never filters (`RankingLean`). A direction
from anyone else is refused before anything is searched (`matching_is_noras`):
Sara and Salman hand matching to her - the next piece of a seating goal
included. A tap on a card's Select is the customer's words ("I've added X to my
cart") routed like any message (17.3).

### 10.6 Their room: walls, fit, placement and the next size

What the customer says about their room is recorded by whoever hears it -
Sara, Nora or Salman - with `note_room`: its kind, length, width, doorway, and
each wall by their own name for it ("the TV wall"), its length and what is on
it (`WallFeature`: door, window, TV point, radiator, built-in, keep free). It is
kept on `room_project` (`geometry`, `layout`), merged wall by wall - a wall said
again keeps what it does not restate - and builds nothing. It is asked once,
after something has been shown, when sizes start to decide: a second sofa, a
rug, a whole room.

**Code measures; Nora judges** (`app/services/fit.py`):

- each piece against each wall and doorway: a wall against its longer floor
  side, a doorway against the smaller of its shorter side and height (15.1);
  a size not listed is said to be unknown, never guessed;
- how much of the floor the chosen pieces take, and each card beside them,
  only when every size is known.

A search in a known room carries `room_fit` per card. `measure_fit` (Nora)
gives the pieces asked about against each wall and doorway (`measured_fit`),
what is already theirs and the floor it takes - the only figures of fit an
agent may quote. Nora judges and keeps her decisions with
`record_design_notes` (needs a room on record):

- **`placements`** - which of their walls each piece goes on, what beside it,
  and why, in words only (no figures), and only for walls they described;
- **`next_sizes`** - the size the next pieces should be for their room and what
  they chose (a rug's longer side, a coffee table's length), as ranges in whole
  centimetres with a reason, for approved kinds only (the family is filed by
  the registry).

A placement with a figure in it, or a size that is out of order, has a figure
in its reason or names a kind outside the vocabulary, is dropped on its own;
the rest stands (21.1). The tool echoes no figure she wrote. Both are kept on
the room (`room_project.placements`, `room_project.next_sizes`, newest per
kind) and shown in every agent's brief, so a later turn searches at her size
rather than one an agent made up: `search_products` `designer_size` orders by
closeness to her longer side, never filters and never becomes a product fact,
and the reply says why that size suits their room (`size_reason`). Whether a
piece works there is never stated beyond what was measured.

`note_room` never gives up a room being designed or in the cart for another
(`another_room_in_progress`); the same room in other words ("the lounge") keeps
its walls; another room starts with no size, walls or sizes of its own. Walls
and doorways are measured apart (at most four walls and two doorways), so a
doorway is always checked.


## 11. Single-Product Discovery Flow

Use PostgreSQL first, then Pinecone conditionally.

```text
User query
    -> LLM query understanding (language -> structured commerce interpretation)
    -> deterministic taxonomy validation
    -> classify constraints
    -> PostgreSQL structured search
    -> enough good candidates?
        -> no: controlled relaxation -> PostgreSQL again
        -> yes: determine whether semantic ranking adds value
    -> Pinecone only when semantic/fuzzy ranking is useful
    -> final ranking
    -> the agent explains trade-offs and presents results
```

PostgreSQL decides structured eligibility.

Pinecone ranks semantic closeness when needed.

Do not call Pinecone for every query by default.

Examples where PostgreSQL may be enough:

- 3-seater sofa under SAR 6,000
- active dining tables under a strict price ceiling

Examples where Pinecone adds value:

- elegant and warm but not bulky
- Scandinavian but softer
- cozy reading chair
- something similar in feel to a selected product

---

## 12. Product Search Contracts

Define typed Pydantic contracts before writing agent prompts.

At minimum, model concepts such as:

- `ProductSearchRequest` - defined for structured discovery (see 12.1)
- `SearchConstraint` - architectural concept; see 12.2
- `ProductCandidate`
- `ProductSearchResult`
- `AgentState`
- `RetailerContext`
- `CatalogCapabilities`
- `InteriorDesignRequest`
- `InteriorDesignPlan`
- `BundleItem`
- `RoomBundle`
- `ToolResult`

Keep transport schemas and internal/domain schemas clearly named when their responsibilities differ.

Do not pass arbitrary dictionaries between layers when a stable typed contract exists.

### 12.1 The structured discovery contract

`ProductSearchRequest` is the minimum contract deterministic PostgreSQL
retrieval needs. It carries:

- `commerce_category` (required)
- `commerce_subcategory` (optional, validated as a pair against the registry)
- the types searched beside it, `alongside_subcategories` - each validated as
  a pair, set only by code and never with a measurement - and `single_type`,
  when the customer asked for the type alone (10.2)
- an optional price constraint, with an explicit currency
- an optional seating-capacity constraint
- exact colour and style requirements (see 12.4)
- a bounded `limit`, defaulted and capped from configuration
- a `sort` drawn from a closed set of deterministic orderings

It carries **no retailer identity**. Scope comes only from `RetailerContext`
(section 8), so the request type cannot express a store at all.

Material and semantic text are deliberately absent. They arrive with the
milestones that use them, designed against real requirements rather than guessed
at now.

It also carries measurements, as customer-facing roles with values already in
centimetres (15.1), and at most one constraint per role. It carries no strength,
no tolerance and no relaxation metadata of any kind: how firmly each measurement
was expressed lives on the semantics beside it (13.2).

Do not add speculative query-understanding fields such as:

- customer priorities
- intent scores
- confidence scores
- reasoning traces
- exclusions
- ambiguity metadata

unless they have been explicitly approved.

There is no relevance score anywhere in the discovery result. Nothing ranks
candidates yet, and an eligible set must never be presented as a ranked one.

Query understanding returns one of four typed outcomes, never a guess:

- a resolved search - the request, the constraint semantics of section 13, and
  any semantic preferences (see 12.4)
- an unresolved strict requirement - they were strict about something the
  catalog cannot guarantee (see 12.4)
- an unsupported requirement - the customer explicitly asked for something the
  search engine cannot enforce (see 12.3)
- a clarification - one question must be answered first

### 12.2 `SearchConstraint` is a concept, not a base class

The name stays approved as an architectural idea: a typed, validated
restriction on a structured search.

It is deliberately **not** implemented as a shared base class. Price and
seating capacity have different value types, different validation rules and
different SQL, and an abstraction over two members would hide that without
removing duplication. Concrete constraints - a price constraint and a seating
capacity constraint - are the implementation of the concept.

Extract a common type when a third constraint shows what it should actually
share. Do not create the abstraction ahead of that evidence.

---

### 12.3 Unsupported requirements are never silently dropped

The catalog carries material and dimension data that structured search cannot
yet filter on.

When a customer insists on one of these - only leather, it must be wool - the
result must say so. It is a distinct outcome type, not a flag, so that a caller
which only handles the resolved case cannot mistakenly present the results as
satisfying the whole request.

A material they simply want ("a wool rug", "something velvet") is a leaning,
exactly as a colour is (12.4): it is kept as descriptive wording that ranks by
meaning, nothing is filtered on it, and the reply never says a piece is made of
it.

Colour and style are no longer among these: they are handled by 12.4.

### 12.4 Colour and style: exact requirement or semantic preference

Colour and style are controlled vocabularies, stored per product:

- colour in `core_product.main_color`, one approved value per product
- style in `core_product.styles`, approved values comma-separated

Both are defined in a versioned catalog-attribute registry, on the same
principle as the commerce taxonomy: one source of truth, loaded and validated
at startup, and never restated in a prompt, an enum, a validator or a test.
Style values contain no spaces, which is what allows a stored string to be
split into exact tokens whatever spacing a merchant used.

A colour or style a customer mentions plays one of two roles, and confusing
them is the mistake to avoid.

**Ordinary shopping language is a preference, not a filter.** "I want a beige
sofa" says what they are drawn to, not what to hide from them. Filtering it
would discard products they might well have chosen. It becomes a semantic
preference, preserved for ranking, and no filter is applied.

**Only explicitly strict language becomes an exact filter** - "only beige",
"it must be Japandi", "nothing else". The customer has ruled the alternatives
out, so excluding them is what they asked for.

This is deliberately the opposite default from price and capacity (13.1),
where an unqualified bound is locked. A budget states a limit; a colour states
a liking.

Matching is exact:

- several colours are alternatives - any one satisfies the requirement
- several styles are all required - the product must carry every one
- a NULL colour, or absent styles, never satisfies a requirement
- style matching is by token, never by substring, so `Modern` must not match
  `Modern_Classic` or `Rustic_Modern`

**Customer words are translated onto the vocabulary by the model, never by
code.** The model interprets what the customer described and chooses every
approved value that fits - one-to-many: "dark grey" is Grey and Charcoal,
"something lighter" is the approved colours lighter than the cards on screen.
It is given the approved lists and its response schema only admits those
values (the search tool's arguments), so it cannot write
a value that does not exist. There are no tone tags and no synonym table:
customers say "warm", "earthy", "moody", and only the model's reading covers
that. Application code only normalises spelling (case, spacing, "_", "-") and
never maps one word onto another.

When nothing approved fits, the customer's own words are kept, and:

- as a preference, they rank by meaning (Pinecone);
- as a strict requirement, no product can match exactly, so the search runs
  without it as the last resort below, and the reply says plainly that
  nothing matched. It is never turned into a question about whether to show
  alternatives - the catalog, not the customer, answers that.

**Preferences order, they never filter.** Products whose stored colour or style
matches a preference come first, then semantic similarity. With an explicit
sort ("the cheapest beige"), matching products come first in price order, then
the rest in price order - nothing is hidden.

**A strict requirement filters, and is lifted only as a last resort.** If even
one product matches, only the matches are shown, however few. Only when nothing
matches - after every other permitted widening (13.5) - is the colour or style
filter lifted: every other constraint is kept, the requested values rank the
closest first, the lift is recorded as a `color`/`style` relaxation with an
exact-match count of zero, and the reply must say plainly that nothing matched
and that these are the closest. A lifted requirement is never presented as met.

Mixed strict requests follow the matching rules above: in "only red or beige",
beige satisfies the request (colours are alternatives), so red becomes a
preference and nothing is disclosed as unmatched; in "Modern and cottagecore",
a Modern-only piece does not satisfy it (styles are all required), so the
unexpressible style is reported as unmatched.

Each card the agent is given carries its stored colour and styles. When no
card carries one the customer asked or wished for, the reply says so - it never
describes the cards as that colour.

## 13. Constraint Semantics

The discovery system must record how firmly the customer expressed each
constraint, so that later relaxation can tell what it is allowed to change.

Three states, and no more:

- `locked` - an explicit requirement or bound; never relax
- `preferred` - explicitly a preference rather than a necessity
- `approximate` - the customer said the figure itself was loose

There is no fourth `open` state. A constraint the customer did not express is
simply absent, and absence already means "freely adjustable".

Examples:

- "cannot spend more than 6000" / "maximum 6000" -> `locked`
- "I'd prefer to stay under 6000" -> `preferred`
- "around 6000" -> `approximate`
- "I need a 3-seater" -> `locked`
- "roughly four seats" -> `approximate`

### 13.1 Unqualified means locked

A constraint stated without softening language is `locked`. "under 5000" is a
requirement, not a preference.

This default is deliberately conservative: reading an unqualified bound as
merely preferred would let a later policy quietly spend more than the customer
allowed. Do not infer `preferred` from a conversational tone - only from words
that actually soften the commitment.

### 13.2 Semantics are separate from execution

`ProductSearchRequest` is the exact query to execute and carries no relaxation
metadata. Constraint strength travels alongside it, on the query-understanding
result:

```text
ResolvedSearch
├── request:   ProductSearchRequest   what PostgreSQL should execute
└── semantics: ConstraintSemantics    how the customer expressed it
```

Product Discovery consumes only the request and has no knowledge of constraint
strength. It executes what it is given, exactly.

The split is a rule about content, not only about layout. The request holds the
facts PostgreSQL needs and nothing else; the semantics hold what a future policy
is permitted to change. Neither duplicates the other, so there are never two
figures that could disagree.

Price, capacity and the subcategory are singular, so each is one field. A
measurement is not: one message may carry several, and they are recorded **per
`DimensionRole`** rather than as a single figure for the query. A pair of sides
carries one strength for the pair, recorded separately again.

```text
ResolvedSearch
├── request.dimensions          OVERALL_WIDTH TARGET 220   DEPTH MAX 100
└── semantics.dimensions        OVERALL_WIDTH APPROXIMATE  DEPTH LOCKED
```

**One query may hold measurements of different firmness.** "Around 220 cm wide
but it must be under 100 cm deep" is an ordinary request, and a single strength
shared across the query could not express it - it would either widen a bound the
customer locked or refuse to widen one they offered.

Because the two structures are separate, their correspondence is an invariant
the contracts enforce at construction: every request dimension has exactly one
recorded strength, no strength is recorded for a dimension the request does not
carry, and a role appears at most once on each side. Looking a strength up is
therefore by role, never by position, and a role that was never recorded raises
rather than returning a default - a missing strength read as consent is exactly
how a locked bound would get widened.

**Knowing is not acting.** Relaxation can now read a measurement's strength; it
still widens no measurement at all. `LOCKED`, `PREFERRED` and `APPROXIMATE` say
what the customer committed to. They are not tolerances, they imply no
percentage, and no dimension tolerance is approved.

A measurement the catalog refuses (15.1) keeps the strength it was stated with,
as provenance. Nothing relaxes on it: the measurement was never applied, so
there is no result to widen.

### 13.3 The product category is not relaxable

Constraint strength is recorded for subcategory, price bounds, seating capacity
bounds, each stated measurement and any planar pair.

`commerce_category` has no strength and is never relaxed. The product family is
the customer's basic intent: someone asking for lighting has not asked for
tables, and no relaxation policy may decide otherwise.

### 13.4 Recording is not relaxing

Query understanding records semantics. It does not act on them, and neither
does discovery. A search that returns nothing is not silently changed - no
widened budget, no dropped capacity, no substituted subcategory - except
through the controlled, disclosed relaxation of 13.5, including the colour and
style last resort of 12.4.

An approximate bound is executed as the exact bound the customer named, which
is narrower than their intent and therefore safe: it cannot return something
they excluded. Widening it is the relaxation layer's job, using the
`approximate` marker as its licence.

Never silently violate locked constraints.

If a relaxed result is ever presented, the replying agent must explain the
trade-off clearly.

### 13.5 Controlled relaxation

Relaxation is a separate layer above Product Discovery, not a step inside it.
It is deterministic: no model is consulted, no customer language is read, and
no conversation history is involved. It reads the structured meaning query
understanding already produced and re-runs the existing exact search.

**The customer's exact request always runs first.** Even when every constraint
is approximate, nothing is pre-relaxed - their own question is worth asking
before any other. Widening begins only if that search returns fewer than the
configured target, and stops the moment the unique pool reaches it, so the
mildest widening that suffices is the one that stands.

Permission comes from constraint semantics alone:

- `locked` is never relaxed
- `approximate` is offered before `preferred`
- a missing strength is treated as locked; absent metadata is never consent

What may widen, and by how much:

- **Price** rises or falls by 10%, then 20%, of the **original** figure. Steps
  accumulate but percentages never compound: 5000 becomes 5500 then 6000, never
  6600. A bound is never removed, a floor never passes zero, and the currency
  never changes.
- **Seating capacity** moves by one seat, once. The filter is never removed: a
  NULL capacity means *unverified*, not *any*, so dropping it would release
  products whose capacity nobody confirmed.
- **The commerce category never moves**, under any strength.
- **The subcategory is not automatically widened either**, even when soft.
  Replacing one subcategory with another needs an approved alternative policy,
  and none exists. Its recorded strength is preserved for that future decision.

- **A measurement widens only where the approved allowlist permits it** (15.2),
  by 5% then 10% of the original, and a target widens symmetrically about its
  figure rather than becoming a ceiling.
- **A strict colour or style is the one exception to "locked never relaxes",
  and only as a last resort** (12.4): when the pool is still empty after every
  permitted widening, that filter alone is lifted, everything else is kept, and
  the lift is always recorded and disclosed. A requirement some product meets
  is never lifted. A strict value no approved value expresses cannot match at
  all, so its exact attempt is recorded as zero and the lift runs at once.

Within one strength, price is offered before seating, and measurements come
last. With two price steps, one seating step and two dimension steps per
strength, at most ten widened attempts can follow the exact search, plus the
single colour/style last resort.

Only a fully resolved search may be relaxed. A request carrying an unsupported
requirement (material, an unreliable measurement), or one awaiting
clarification, must be settled conversationally first.

Paging ("show me more") excludes products already seen from the same request.
Any change to the request's criteria starts a fresh set: exclusions from paging
do not carry into a refined search.

**A size belongs to the product type it was given for.** 60 cm for a side table
says nothing about a coffee table, so a measurement never follows the customer
to a different type - not even one the registry could measure the same way.
Each type's sizes are saved in session state
(`CustomerPreferenceState.measurements_by_type`) after a search the customer
asked for has run - a room plan's, a companion's or a similar-product search
never saves. Returning to a type, or a new
search for it that states no size of its own, brings its sizes back and the
reply says so (`earlier_sizes_applied`); a size stated now is used exactly, not
blended with the saved one. "Any size is fine" on the way back is the search's
`earlier_sizes: false`. A search that is ours - what goes with a pick, more
like this - neither brings sizes back nor saves them. A size the search could
not apply is reported (`sizes_not_applied`), so the reply never presents it as
met.
Room measurements are a different fact - they belong to the room and always
carry.

The original request is preserved unchanged beside the final one, and every
product records the attempt at which it first became eligible. That depth is
provenance, not a score - **nothing in relaxation ranks anything**.

Reaching the end of the permitted widenings without meeting the target is a
valid outcome, not an error. The catalog may simply not hold enough.

**Nothing found is never a dead end.** When even the widest permitted search is
empty, the relaxation layer also counts what each of the customer's own
requirements, set aside alone, would find (`set_aside`: price, seats, each
measurement, a size pair, strict colour, strict style - never the product
type), and for a budget nothing meets, the nearest real price in their
currency. Only on a customer search, never for room optimisation. The reply
offers the most useful one as a question they can say yes to ("sofas here start
at 990 SAR - shall I show you the most affordable ones?"). Nothing is set aside
for them: that stays their decision.

---

## 14. Taxonomy Registry and Query Understanding

Never pass raw customer category words directly into SQL.

Two components, with a hard line between them:

- the **taxonomy registry** defines the approved vocabulary
- **query understanding** decides what the customer meant

The LLM does the second. It never does the first.

### 14.1 The taxonomy registry

The registry is the authoritative, version-controlled definition of the
approved `commerce_category` -> `commerce_subcategory` relationships, for
example:

```text
seating  -> sofa, chair, lounge-chair, office-chair, recliner, ...
tables   -> center-table, service-table, nightstand, console, tv-table
```

Requirements:

- keep it in a versioned domain configuration source, not in environment variables and not scattered through Python
- make it straightforward to extend with new approved values
- load and validate it at startup; an invalid registry is a startup failure
- expose it to application code as a typed, queryable structure

Do not hardcode category/subcategory relationships throughout business logic.

```text
THE TAXONOMY REGISTRY DEFINES THE VOCABULARY.
APPLICATION CODE ENFORCES IT.
```

### 14.2 Query understanding is LLM-first

Customer language is open-ended and contextual. Correctness must **not** depend
on maintaining a large deterministic synonym or alias dictionary such as
`couch -> sofa` or `bedside table -> nightstand`.

Customers say things like:

- "I need a couch for four people"
- "something compact for me and my wife"
- "a small table beside my bed"
- "something next to my sofa for a lamp"
- "a chair where I can put my feet up"
- "something comfortable for reading"

Semantic interpretation of product language belongs to the LLM, using
conversation context. The intended flow is:

```text
User message
    -> LLM interprets customer intent using conversation context
    -> structured commerce interpretation
    -> deterministic taxonomy validation
    -> valid structured intent
    -> Product Discovery
```

The LLM may determine values such as `commerce_category`,
`commerce_subcategory`, `seating_capacity` when applicable, and `material` when
applicable. For example:

- "I need a couch for four people" -> `seating` / `sofa`, seating capacity `4`
- "I need a small table beside my bed" -> `tables` / `nightstand`
- "I need a small table beside my sofa" -> `tables` / `service-table`

The bed/sofa distinction is deliberately an LLM reasoning task. No hardcoded
`couch -> sofa` alias is required for any of these.

Small deterministic text normalization remains acceptable and belongs in
application code:

- whitespace normalization
- casing normalization
- punctuation handling
- numeric parsing
- measurement/unit parsing

If a small alias list is introduced later as a performance or cost
optimization, it must be optional and must never be required for correctness.

Do not build a large synonym registry.

### 14.3 The LLM does not own the taxonomy

The LLM interprets language. It must never invent taxonomy values.

`seating` / `sofa` may be valid. `living-room-furniture` / `luxury-couch` must
be rejected when those values are not in the approved taxonomy.

After interpretation, deterministic application code validates:

1. Is the `commerce_category` approved?
2. Is the `commerce_subcategory` approved **under that category**?
3. Are the applicable structured commerce attributes valid?

An unsupported value must never:

- reach SQL
- be persisted
- silently become a new taxonomy value

Rejection is handled by bounded correction or clarification behaviour, never by
accepting the invented value.

### 14.4 Category scopes subcategory

The hierarchy constrains model output. Conceptually:

```text
User request
    -> identify commerce category
    -> select only among subcategories valid for that category
    -> produce structured result
```

The model must not be able to pair `seating` / `chandelier`, or
`lighting` / `sofa`.

This does not require two LLM calls. A single structured call may perform the
interpretation, provided the output is validated deterministically against the
taxonomy afterwards.

### 14.5 Ambiguity

The LLM must not invent certainty.

"Show me a table." may support `commerce_category = tables` while giving no
basis to choose between `center-table`, `service-table`, `nightstand`,
`console` and `tv-table`.

The architecture must allow the agent to ask one useful
clarification question instead of inventing a subcategory. Guessing a specific
subcategory to avoid asking is a defect.

### 14.6 Responsibility boundaries

```text
A. PRODUCT DATA PREPARATION   (outside the FastAPI agent runtime)
   raw product information -> manual review / data processing
       -> commerce_category, commerce_subcategory, seating_capacity, material

B. QUERY UNDERSTANDING        (agent / query-understanding layer)
   customer natural language -> LLM interpretation
       -> approved commerce taxonomy values + applicable attributes

C. TAXONOMY VALIDATION        (application / domain code)
   structured LLM output -> deterministic validation against approved
       commerce_category -> commerce_subcategory relationships

D. PRODUCT DISCOVERY          (services / repositories)
   validated structured request -> PostgreSQL, then optional semantic ranking
       -> real retailer products
```

---

## 15. PostgreSQL Responsibilities

PostgreSQL is the first structured retrieval layer.

Use it for:

- store scoping
- active status
- commerce category/subcategory
- exact/normalized product-name lookup
- price constraints
- dimensions
- seating capacity
- style/color/material exact constraints when data quality permits
- brand
- other authoritative structured facts

Use parameterized SQL generated by repositories/ORM expressions only.

No string-concatenated SQL from LLM output.

Use indexes appropriate to real query patterns. Inspect existing indexes before adding new ones.

Avoid N+1 query patterns.

Bound candidate sets before downstream semantic work.

### 15.1 Dimension search

A customer says "no wider than 220 cm". What "wide" means depends on the
product: a sofa's width is its along-wall span, which the catalog stores in
`length`, while that sofa's `width` column holds front-to-back depth. There is
no global correspondence.

So the two are kept apart:

- **Physical role** - `LENGTH`, `OVERALL_WIDTH`, `DEPTH`, `HEIGHT` - is what
  the customer means, and the only thing query understanding ever produces.
- **Stored axis** - `length`, `width`, `height` - is a database column, and
  only `dimension_semantics_v1` turns a role into one.

**A model never chooses a column.** It proposes a role; the registry resolves
it. A role the registry does not map for that subcategory is refused, not
answered with a plausible-looking axis.

Support is per subcategory *and* per role. A product type may support height
and not width. **Absence of a role means unsupported, never "guess"**, and the
registry records a machine-readable reason for the refusals it states
deliberately.

**Not every product type qualifies.** Where the audit found the stored axes
unreliable - beds, whose two planar axes both sit in 190-230 cm with some rows
transposed; sectionals, which record one span or the other - planar filtering is
refused rather than offered approximately.

**Read by side (`discovery.size_by_side`, on by default).** Merchants put a
piece's floor measurements in either column, so a floor measurement is read as
the piece's **longer or shorter floor side** - the larger or smaller of its two
floor columns - never as a named column. The model says which side the customer
meant (a sofa's width is its long side, a bed's width its short side; depth is
always the short side, enforced in code by `side_of`); it never names a column.
A store that keeps a sofa's width in `width` gets exactly the answers store 50
does. Refusals about which column holds which side (beds, chairs) are answered
this way; what the shape or the data cannot answer - a corner set's width, a
rug's single side, an unreliable height column - is still refused. Off, each
role reads the column reviewed for store 50. On store 50's data the two agree
for every kind except rows whose columns contradict each other, where the side
is the true reading.

A **pair of sides** - "a desk 120 x 60", "a sofa 220 by 90" - works the same way
for any recorded piece with one footprint: the smaller figure is the shorter
side, the larger the longer, each matched within `discovery.pair_tolerance_share`
(5%). Rugs keep their exact unordered match; a corner set has no single
footprint and is refused. A pair nothing meets offers what setting it aside
finds - store 50 lists beds by frame size, so "a bed 160 x 200" finds none.
A pair given with no unit is centimetres (`PAIR_UNIT_WHEN_UNSAID`); a single
bare number is still asked about.

**No normalised columns exist.** `length_cm`, `width_cm` and `height_cm` are
not in the database and must not be added. The catalog stores whatever unit the
merchant supplied, and comparison converts in SQL from the same alias and
factor tables the runtime normalizer uses - one source of truth, so a new unit
is added once. An unrecognised or missing unit yields NULL and therefore matches
nothing: a dimension is never assumed to be centimetres.

Filtering happens in the query, before ordering and limiting. Fetching a bounded
page and discarding rows in Python afterwards would lose products the customer
could have had.

**Two failures that look alike and are not:**

- *Ambiguous intent* - "a sofa under 200 cm" names a number but no measurement.
  One question settles it, so it is a clarification.
- *Unsupported data* - "a bed less than 200 cm long" is perfectly clear. Asking
  again cannot make the stored axes reliable, so it is an unsupported dimension
  requirement carrying the role, the value and the reason.

**A target is a target.** "around 220 cm" is a figure to sit near, not a
ceiling, and is never rewritten as a maximum. Exact search seeds it with
equality; widening it is a later milestone. Its kind and its firmness are
separate facts: the request records `TARGET 220`, the semantics record
`APPROXIMATE`, and neither substitutes for the other (13.2).

**Vague size words are not measurements.** Compact, small, low-profile give no
number to quote, so they produce no constraint and invent no threshold. They
guide the product type, and may later serve semantic ranking.

Some products have sides with no agreed order: a rug described as 200 x 300 is
the same rug as 300 x 200. The registry marks those subcategories, and their
pair is matched unordered. Three-number input is not supported: no product
family showed a stable enough convention.

Numeric dimensions are deterministic facts and stay outside Pinecone. No
embedding decides whether 218 cm satisfies a 220 cm limit.

Whether a product fits a room, a doorway or a wall is spatial reasoning, and
belongs to the Interior Design Agent rather than product search.

### 15.2 Dimension relaxation is narrow recovery behaviour

Widening a measurement is what happens when the customer's exact request found
too little - not a routine broadening. Measured on the reviewed store-50 data,
86% of bounds already return enough without it, so the policy is deliberately
small and fails closed.

**Only two pairs relax in V1**: `sofa` and `service-table`, role
`OVERALL_WIDTH`. `LENGTH`, `DEPTH` and `HEIGHT` do not relax for any
subcategory, `OVERALL_WIDTH` does not relax for any other subcategory, and a
carpet's planar pair does not relax at all. A pair absent from the allowlist is
non-relaxable; absence is never read as permission.

That allowlist is **search policy, not product semantics**, so it does not live
in `dimension_semantics_v1`. The registry answers what a customer's physical
role can be trusted to mean in this catalog; the policy answers whether
widening that measurement helps. They change for different reasons.

**The store-50 caveat is part of the policy, not a footnote.** The 5% and 10%
stages were validated against one retailer's 1,036 reviewed products. Nothing
claims they suit another catalog. M8 carries no `store_id` and never branches on
one: scoping dimension policy per retailer is a separate, deferred design.

Permission and magnitude are separate questions. `LOCKED` never relaxes.
`APPROXIMATE` is offered before `PREFERRED`, exactly as for price and seating -
but they widen by the **same** amounts. Ordering expresses which invitation was
clearer; it is not a claim that one customer tolerates more centimetres.

Two stages, 5% then 10%, both computed from the customer's **original** figure.
Percentages accumulate but never compound: 220 widens to 231 then 242, never to
242.55. Every bound moves outward only - a maximum rises, a minimum falls and
never reaches zero, and a range expands each side from its own original bound.

**A target widens around its figure.** "Around 220" becomes [209, 231] then
[198, 242], symmetric by construction. It executes as a band because SQL has no
other way to express nearness, but it does not become a range: the recorded
provenance keeps `kind = TARGET` and the original 220, so a later layer can say
"a little wider than the 220 you wanted" rather than describing an interval the
customer never gave.

Provenance is structured, never prose. One `RelaxableField.DIMENSION` member
carries the field and a `DimensionRole` carries which measurement, so adding a
role never means editing an enum. A widened attempt records the role, the
customer's original kind, the strength, the stage, the original figures and the
bounds actually applied - enough for the replying agent to explain the
trade-off, and nothing that reads as a score.

Relaxation never admits a product whose dimension or unit is missing. That holds
because SQL comparisons against NULL stay unknown however wide the band grows,
not because a rule forbids it.

---

## 16. Pinecone Responsibilities

Pinecone is the semantic ranking/retrieval layer, not the source of truth.

The system already has Pinecone linkage through `pinecone_id` and existing image-embedding infrastructure. Inspect the existing Pinecone code, index schema, metadata, vector dimensions, namespaces, and model assumptions before adding text semantic search.

Do not mix incompatible image and text vectors in one index/vector field merely for convenience.

Text embedding content should include useful semantic product information such as:

- `name_english`
- commerce category
- commerce subcategory
- style
- color
- material when authoritative data exists
- concise semantic product description when/if enrichment exists

Do not rely on embeddings for exact numeric facts such as price or dimensions.

Exact product-name lookup belongs primarily in PostgreSQL/lexical search.

If Pinecone is used after PostgreSQL, restrict semantic ranking to the eligible candidate products and active store scope.

Always preserve `store_id` metadata in vector records as defense-in-depth even when PostgreSQL already scoped candidates.

### 16.1 Semantic ranking decides order, never eligibility

PostgreSQL decides which products a customer may see. Ranking decides only the
sequence, and the contract is built so it cannot do more: the candidate list it
receives is the candidate list it returns, reordered. A returned id outside the
eligible set discards the whole ranking rather than filtering it down - one
wrong id means the scope cannot be trusted.

**Never query the index first.** Eligibility comes from M6/M8, and the index is
restricted to those ids. Searching the catalog semantically and checking the
results against PostgreSQL afterwards is the inverted architecture, and it lets
an embedding decide what exists.

**The whole eligible pool is ranked.** There is no `semantic_candidate_limit`.
The 50-row presentation limit is presentation: reranking the first fifty of a
hundred and seventy-three would leave the best match unreachable, so
`search_eligible_ids` takes no limit at all. Both it and `search` build from one
predicate function, so the ranked set can never be a different set from the
searchable one. If a pool ever exceeds a transport limit, the filter is chunked
and every chunk scored with the **same** query vector - chunking is transport,
not relevance, and no candidate is ever dropped.

**Exact fidelity outranks similarity absolutely.** Ordering is
`relaxation_depth` ascending, then similarity descending, then `product_id`.
Depths are separate buckets, never blended: a product that satisfied the
customer's own request is not displaced by one that needed a widened bound,
whatever an embedding prefers. There is no combined score and no `final_score`.

**An explicit sort is an instruction, not a hint.** "Cheapest" means cheapest;
similarity only orders products the sort leaves tied. A semantic shortlist
followed by a price sort is forbidden - measured on store 50, it returned a
1,250 SAR sofa as "cheapest" when a 990 SAR one was eligible.

**Stated colour and style preferences lead, deterministically.** Within each
depth bucket, products whose stored `main_color` or style token matches a
preference come before the rest, then similarity decides. An embedding alone
can place warm stone above charcoal for "dark grey"; the stored value cannot.
With an explicit sort, matching products come first in the sort's order, then
the rest in the same order ("the cheapest beige" is the cheapest of the beige
ones), so nothing is hidden. The same ordering applies when the index is
unavailable.

**The query embedding is built from what M7 already produced**, never from a
second model call. `semantic_text` is the customer's descriptive wording with
the parts the structured fields already captured left out, because price
wording measurably degrades ranking. It feeds the embedding and nothing else:
it can never change a category, a bound, a colour requirement or a retailer.
A request with no fuzzy wording and no preference is not embedded at all -
an embedding would impose an order rather than discover one.

**The customer's own message ranks too** (`ResolvedSearch.customer_words`).
On a search the customer asked for, the message they typed that turn - never a
tap's stand-in words - is added to the query document as "Customer said",
with figures, currency words and units taken out (`ranking_words`: text
cleaning, no interpretation). Their words therefore order the cards even when
the agent's search left them out: "beige king beds" ranks the beige king beds
first though the agent searched only "beds". It is kept with the search, so
its pages rank the same way, and replaced by the next search. Searches that
are ours - what goes with a pick, more like this, the room engine - never take
it. It orders and never filters: eligibility, an explicit sort, relaxation
depth and stated preferences all stand exactly as above.

**Every failure degrades to deterministic order.** No index, no embedding, no
candidates, or an integrity failure: the eligible products still come back, in
M6/M8 order, with the reason recorded structurally. Discovery never becomes
unavailable because ranking did.

**A missing vector is an indexing gap, not low relevance.** Such a product keeps
its place in its depth bucket after the scored ones, and its similarity is
recorded as absent rather than as zero.

**Hydration re-reads from PostgreSQL.** The index supplies an order and nothing
else; names, prices, URLs, dimensions and commerce fields all come from the
database, and a ranked id the database no longer returns is dropped rather than
served from stale index metadata. Similarity stays internal and never reaches a
customer-facing shape.

---

## 17. Agent Architecture

A supervisor and four agents, built on LangGraph (`app/agents/`).

| Agent | Owns | Tools (`app/agents/tools/`) |
|---|---|---|
| Supervisor | who handles the turn | `transfer_to_<agent>`, `finish` - it never talks to the customer |
| Sara | finding, comparing, the cart, a group's seating one piece at a time | `ask_about`, `search_products`, `more_results`, `leave_out_product`, `more_like_this`, `product_details`, `compare_products`, `add_to_cart`, `remove_from_cart`, `like_product`, `show_cart`, `remember_taste`, `update_goal`, `note_room` - the only owner of the cart's adds and of comparisons |
| Nora | how it looks and whether it works: design, matching, their room, fit and placement, whole rooms | `design_room`, `revise_room_piece`, `measure_fit`, `record_design_notes` and a search `direction` (hers alone), `store_shelf`, `room_piece_options`, `swap_room_piece`, `remove_room_piece`, `set_room_piece`, `accept_over_budget`, `decline_over_budget`, `room_could_add`, `add_to_room`, `change_room`, `show_room`, `search_products`, `more_results`, `leave_out_product`, `product_details`, `remember_taste`, `update_goal`, `note_room` |
| Salman | what goes with a finished choice, finishing a designed room and its styling, rooms into the cart, value | `what_goes_with` and `add_room_to_cart` (his alone), `store_shelf`, `finish_room`, `search_products`, `more_results`, `leave_out_product`, `more_like_this`, Nora's room-editing tools, `remember_taste`, `product_details`, `update_goal`, `note_room` |
| Faysal | the store itself | `store_policy`, `contact_store`, `product_details` |

**One owner per job.** Only the owner has a job's tools, so no agent can
answer another's question from its own judgement: a design or matching
question that reaches Salman or Sara is handed to Nora, an "add it" that reaches Nora or Salman is
handed to Sara. The supervisor routes by what is asked, never by who spoke last:
"yes", "the second one" or a tapped button is sent to the owner of what it
does, whoever offered it. Every agent also has `calculate` (17.1).

Every agent also has `respond` (its reply to the customer) and `hand_back`
(pass the turn on with a typed reason). A hand-back naming a colleague
(`to`) with a need (`needs_*`) goes straight to them, with what was done this
turn and what they are asked for - no routing call; without one, or back to the
agent who just passed it on, the supervisor decides. The hop limit and the
turn's budget bound both. A turn in which Nora designs a whole room has the
room's limits instead (`agents.room_work_deadline_s`, `room_turn_deadline_s`;
the gateway in front must wait longer), and once its fixes are made the call
that words it runs at `agents.<agent>.wording_reasoning_effort` (unset: the
design's own effort, since at low the wording leaked the work behind the room). `update_goal` and `remember_taste` only keep a
record and may ride in the same step as the reply (`beside_reply`), costing no
model call of their own. The agents work within
`agents.work_deadline_s`; past
it the work stops where it stands, every finished tool's change kept, and the
agent whose hop was under way says what was done and what was not, before
`agents.turn_deadline_s`.

### 17.1 Rules the code enforces, whatever an agent decides

- **Allowlist.** Each tool names its owners; the loop refuses any other agent's
  call before reading it (`app/agents/loop.py`).
- **No record ids in tool arguments.** Products are referenced by their place on
  the customer's screen (`ProductReferenceSelector`); the registry refuses, at
  startup, any tool whose arguments could name a store, product or session.
- **Store scope** comes only from `ToolContext`, built from the request.
- **Approved values only.** Search arguments are the constrained interpretation
  schema, validated by `QueryUnderstandingService.resolve`; room kinds, pieces,
  colours and styles are closed choices. An agent names the product type, and
  its family comes from the registry (`CommerceTaxonomy.category_of`): every
  type is filed under one category, so "bedroom / nightstand" searches
  nightstands - they are filed under tables - instead of being refused.
- **A refusal says what is right.** A call a rule refuses is answered with its
  code and the safe details of why - the values sent and, where the registry
  knows, the correction ("nightstand belongs to tables") - never an id or
  anything internal, so the agent corrects itself instead of guessing.
- **Check before showing.** What a turn's tools find is a draft until the agent
  replies: it checks the cards answer what was asked and searches again if
  not. Rejecting everything, it replies with `show: "unchanged"` - the screen,
  the list "the second one" means and the search behind it stay as they were
  (`keep_screen`), while the rest of the turn's work (cart, likes, goals, room)
  stands.
- **Facts from services.** Tools return verified facts; the screen is built
  from tool payloads (`app/agents/screen.py`), never from the reply's words.
- **Figures are references, written in by code** (`app/agents/figures.py`).
  Every number in the session brief and in each tool's facts has a key
  (`cards_on_screen.card2.price`, `cart.totals.sar.total`, `budgets.room.at_most`)
  shown to the agent beside it; the agent writes `{key}`, and code fills in the
  figure, formatted alike everywhere. A key that names no figure is broken
  arguments, corrected in the same hop; anything between braces counts, so a
  malformed one is never sent. Keys never collide: an object in a list is named
  by its identity (card, pick, piece, goal, option, what a budget bounds), else
  by a label unique in its list, else by its place. Only the newest brief is
  cited, and its cards give way once a tool shows a new list; a refused call
  names no figures. A figure is written exactly as the facts hold it, never
  rounded. Only the customer's own numbers and a card's place are typed.
- **Sums are a tool's** (`calculate`, every agent): add, subtract, multiply,
  divide, a floor area from two lengths, a percentage - each input a figure key
  or a number the customer said, the result a new figure. An agent never works
  out a figure itself.
- **Every figure checked**, as a safety net (`app/agents/reply.py`): a number
  must come from a tool's facts, the brief or the customer's words. One
  correction; then only the sentences with such a figure are dropped (a
  question with one goes with its choices), and the fixed reply follows only
  when nothing is left.
- **Budgets.** Handoffs, model calls per turn, tools per hop, and a turn deadline
  (`agents.*` settings). A turn that runs out of steps, or breaks the argument
  rules twice, keeps what its tools already did - each tool's change is staged
  whole, so the state is never half-applied - and the agent who was working gets
  one reply-only call to say plainly what was done and what was not. Only when
  that reply fails too, when a reply's figures fail the check twice, or past the
  deadline, does the turn end on the fixed reply with the stored session
  untouched.
- **Typed handoffs.** Agents never message each other: a hand-back carries a
  typed reason and, at most, the colleague's name. The next agent reads the
  record of what earlier agents' tools did this turn.

### 17.2 Shared memory

One staged `AgentStateV1` per turn (`TurnWork`), changed only through the
reducers in `app/services/agent_state.py`, plus the ordered record of every
tool call. Redis compare-and-set (`save_if_revision`) stays the only store; no
LangGraph checkpointer. Each stored assistant message names the agent who wrote
it (`ConversationMessage.agent`) and the answers it offered to tap
(`ConversationMessage.choices`). Every agent reads the session through one
typed brief built fresh each turn (`SessionBrief`, 10.4) - never the catalogue
beyond what the customer touched. Fields the scripted flow kept are dropped
when an old session is read (`AgentStateV1` and `RoomProjectState` before
validators); the version does not move.

### 17.3 Taps

A screen button (`bundle_action`, `search_action`, `product_action`,
`support_action`) becomes a typed event (`TapEvent` in
`app/agents/screen_actions.py`: the button and the places it named, never an
id) that the supervisor routes like a message, told who offered the button and
each agent's tools - a hint, never a rule about which tool must run. The
console sends a Select as the customer's words ("I've added X to my cart").
Tapping and typing run the same agents, tools and checks.

---

## 18. Orchestration

Two graphs:

```text
outer (app/orchestration/graph.py):
load_session -> run_agents -> persist_session -> build_public_response

team (app/agents/team.py), inside run_agents:
START -> supervisor -> sara | nora | salman | faysal
              ^                    |  model -> tools -> ... -> respond | hand_back
              +---- hand_back -----+
```

The revision is checked before any model call; the reply is checked before
persistence; nothing is returned that was not committed. Both graphs compile
once per process; a request's tools and services travel in the team graph's
state.

---

## 19. Session State

Use Redis for short-term V1 state.

No S3 chat archive in V1.

No cross-session long-term memory in V1.

**The conversation's window and its rolling summary**
(`app/services/history_summary.py`). The last `session.max_history_messages`
(40) messages are kept word for word, and each agent reads up to
`agents.history_messages` of them. The turn whose exchange would reach the
window folds the oldest `session.summary.fold_messages` (10) into one rolling
summary (`ConversationContext.earlier_summary`): made beside the agents' work by
its own model call (`session.summary.*`), and saved in the same write that
removes the messages it replaces. A finished reply waits for it at most
`summary.wait_after_turn_s` (3 s); not ready, or failed for any reason - a
defect included, logged - the turn is saved unfolded and the next folds instead. The summary is written in the customer's language, so nothing is lost in translation, and is words only - a digit is asked for again once, then
the summary is given up - because budgets, sizes, seats and prices are kept
exactly in the state. Every agent and the supervisor read it before the
messages, as data, with the state right where they disagree. A summary that
fails or is too slow drops nothing: the turn is saved unfolded and the next
tries again, the messages kept up to `summary.hard_cap_messages` (50). With
summaries off, the oldest messages are dropped at the window. The summary lives
and expires with the session; it is not an archive.

Suggested session state concepts:

- session ID
- store ID
- current goal
- current intent
- room type
- room dimensions
- budget
- style preferences
- color preferences
- material preferences
- product constraints
- viewed product IDs
- selected product IDs
- rejected product IDs
- rejection reasons
- bundle items and per-item status
- current recommendation context

Key Redis data by store + session to reduce accidental cross-store collisions.

Example conceptual key:

```text
zory:store:{store_id}:session:{session_id}
```

TTL must be configurable.

Do not conflate Redis memory lifetime with any future commercial/billing engagement-session definition.

---

## 20. Guardrails and Sensitive Information

Guardrails are mandatory in V1 even though authentication is postponed.

Do not rely only on prompts for security.

### 20.1 Input guardrails

Validate:

- request shape
- message size
- session/store context
- tool eligibility
- suspicious prompt-injection attempts where relevant

Treat user input, merchant text, product descriptions, retrieved content, and external content as untrusted data, not instructions.

### 20.2 Tool guardrails

Use a strict tool allowlist.

The LLM must not have:

- arbitrary SQL
- shell access
- filesystem access
- unrestricted HTTP calls
- unrestricted database browsing

Store scope must be injected by application code, not provided by model output.

### 20.3 Data minimization

Do not put secrets or unnecessary internal data into prompts.

Agents must never receive:

- database credentials
- Pinecone API keys
- OpenAI API keys
- access tokens
- internal service secrets

Tools/services own credentials internally.

### 20.4 Safe DTOs / allowlisted output

Do not send raw ORM/database rows to the final-response LLM when they contain fields that should not be exposed.

Create explicit safe product/result DTOs containing only fields required for customer responses.

### 20.5 Never reveal

Do not reveal to customers:

- credentials/API keys/tokens
- internal infrastructure URLs
- database connection details
- raw stack traces
- system prompts
- hidden agent instructions
- internal ranking weights
- internal policy/guardrail implementation details that create security risk
- retailer-private configuration
- sensitive cost/margin information
- other retailer/store data
- other session/user data
- hidden debug metadata

### 20.6 Output guardrails

Before returning customer-visible content, validate that:

- product facts are supported by tool/service data
- results belong to the current store scope
- no sensitive fields are present
- no unsupported claims are stated as facts
- no raw errors or internal traces are exposed

If validation fails, block/regenerate/fallback safely.

### 20.7 Authentication caveat

V1 does not implement authentication/authorization.

Store scoping in V1 protects against accidental/model-driven cross-store access, but it is not a replacement for future identity and authorization controls. Do not claim otherwise.

---

## 21. Error Handling

Create a small explicit exception hierarchy for application/domain/integration failures.

Examples:

- invalid search request
- catalog unavailable
- product not found
- store context missing
- Pinecone unavailable
- Redis unavailable
- LLM timeout
- guardrail rejection

Map internal exceptions to safe customer/API responses.

Never return raw exception messages or stack traces to the customer.

Log internal details with correlation/trace IDs.

Do not swallow failures with broad `except Exception: pass` patterns.

Use retries only for transient operations and only with bounded configurable retry policy/backoff.

### 21.1 Unusable model output never reaches the customer as an error

Model output that breaks a contract rule, cannot be read, or cannot be applied
is our failure to understand, not the customer's. It follows one ladder, and
stops at the first step that works:

1. **Normalise** what can be read without guessing: spelling of approved
   values, thousands separators and "5k" for money, "20%" for a percentage.
   Anything ambiguous ("2,5", "5.000" as a price, "1,500 m") is refused, never
   guessed - a misread figure is worse than a refused one. Every figure read
   from model text goes through one reader (`app/core/numbers.py`); a figure in
   an agent's tool arguments is a typed number its schema validates.
2. **One corrective attempt**, told which rules were broken - our own rule text
   and the approved values, never the customer's words or the refused answer.
   The model may then ask the customer one question if their meaning is
   genuinely unclear. An agent gets one correction of broken arguments a
   turn; a turn the agents cannot finish is first worded by the agent who was
   working, keeping the work its tools did (17.1).
3. **Fall back** to a fixed, friendly "please say it another way" reply, with
   the conversation state exactly as it was before the turn. No model call,
   nothing half-applied. For the agent team, a turn stopped by a rule first gets
   one worded reply that keeps the work its tools completed (17.1); the fixed
   reply follows only when that fails.

Only outages (database, provider, index) and genuine defects surface as errors.
Every rejection is logged with the rule that failed, never the offending value.

---

## 22. Observability

Every request should be traceable without logging secrets or excessive personal content.

Track structured fields such as:

- request/trace ID
- session ID
- store ID
- route/intent
- agent/tool called
- latency per operation
- PostgreSQL candidate count
- relaxation round/count
- Pinecone used yes/no
- ranking candidate count
- model name/version
- prompt/template version
- errors/retries

Do not log API keys, credentials, full secret-bearing config, or raw sensitive data.

Prefer structured logging over ad-hoc `print()` statements.

---

## 23. Suggested Project Structure

Use this as a starting shape; adapt only when the repository already has a stronger established pattern.

```text
app/
├── main.py
├── api/
│   ├── dependencies.py
│   └── routes/
│       ├── health.py
│       └── chat.py
├── core/
│   ├── config.py
│   ├── logging.py
│   ├── exceptions.py
│   └── lifespan.py
├── schemas/
│   ├── agent.py
│   ├── session.py
│   ├── retailer.py
│   ├── product.py
│   ├── discovery.py
│   ├── design.py
│   └── bundle.py
├── agents/
│   ├── team.py             # supervisor + agents (LangGraph)
│   ├── loop.py, views.py, screen.py, reply.py
│   ├── operations/         # search, cart, room engine
│   └── tools/              # each agent's typed tools
├── orchestration/
│   ├── graph.py
│   ├── state.py
│   └── routing.py
├── services/
│   ├── retailer_context.py
│   ├── catalog_capability.py
│   ├── taxonomy.py         # loads the registry; validates interpretations
│   ├── discovery.py
│   ├── relaxation.py
│   ├── ranking.py
│   ├── comparison.py
│   ├── furnishing_planner.py
│   ├── budget_allocator.py
│   └── bundle_optimizer.py
├── repositories/
│   └── products.py
├── integrations/
│   ├── postgres.py
│   ├── redis.py
│   ├── pinecone.py
│   └── llm.py
├── guardrails/
│   ├── input.py
│   ├── tools.py
│   ├── output.py
│   └── public_dto.py
├── prompts/
│   ├── agents/             # the supervisor's and each agent's instructions
│   └── interior_design/
└── observability/
    └── tracing.py

tests/
├── unit/
├── integration/
└── fixtures/

evals/
├── discovery/
├── conversations/
└── whole_room/
```

Avoid creating empty abstraction layers only to match this tree. Add modules when there is real responsibility.

---

## 24. Dependency and Resource Management

Prefer modern Python packaging through `pyproject.toml` with a lock file.

Use FastAPI lifespan management for long-lived application resources such as:

- PostgreSQL engine/pool
- Redis client
- Pinecone client/index handles
- reusable LLM clients where SDK behavior supports it

Do not create a new database/Pinecone/Redis client per request.

Do not use blocking database/network clients inside async request paths unless explicitly isolated.

Centralize integration creation and dependency injection.

Dependencies should be easy to replace with fakes during tests.

---

## 25. API Shape for V1

Keep the initial API small.

At minimum:

- `GET /health`
- `POST /v1/chat`

The cart adds one read: `GET /v1/cart` (section 36).

A chat request should carry only what V1 actually needs, conceptually:

```json
{
  "session_id": "...",
  "store_id": 123,
  "message": "I need a modern sofa under 6000"
}
```

Do not add authentication fields before authentication exists.

Once `store_id` is resolved into request context, downstream agents/tools must consume context rather than accepting arbitrary store IDs from model-generated arguments.

Design the response so structured product/bundle data can coexist with assistant text; do not encode UI as arbitrary LLM-generated HTML.

**Each reply names the agent who wrote it** (`ChatResponse.agent`: `sara`,
`nora`, `salman` or `faysal`), and `ChatResponse.workflow` lists every agent that
worked on the turn, in order (`handoffs`), ending with the one who replied - the
console shows "Sara → Salman". Furniture Finder and room renders are Sara's.

`POST /v1/chat/stream` runs the same turn once and sends server-sent events:
`progress` (`{agent, phase}`, never arguments or products) while the agents
work, then exactly one `reply` with the committed `ChatResponse`, or one
`error`. A stream that ends without a reply means the session must be re-read,
never the request replayed.

**The server words what a client shows around products, in the reply's
language** - so a store's widget, like the console, shows it as it is and keeps
no copy of its own (docs/widget-display-contract.md). Each product on the
chat's cards, the product in focus, the products compared, the picks tray and
Browse Catalogue's tiles carries `display`: the name (in Arabic, the store's
`name_arabic` exactly as stored - the English name only where it is empty),
the kind, colour and styles in the registries' reviewed words
(`commerce_v1.yaml`, `catalog_attributes_v1.yaml`), and the seats, price and
listed size worded for the language, in Western digits
(`app/services/product_display.py`). Every piece of a room package and of a
seating combination carries `display` too (name, kind, seats, the price of one,
the line total), and its totals the worded new spend and budget. Every chat reply
carries `labels` - every word a client draws around the conversation: the card
buttons, the picks tray, the compare bar, Browse Catalogue's screens, the room package card,
the comparison table, a room picture, and the message a
tapped button puts on the customer's side - with placeholders for the client to
fill (`app/services/card_labels.py`; an Arabic label may leave one out, never add
one). A photo search's pick and a room picture carry `labels` too, and the pick's
cards `display`. The console keeps no words of its own beyond an English
fallback; `GET /v1/labels?language=` answers before any reply has. Browse Catalogue takes `language` and words its tiles, filter options
(`label`), room styles (`style_labels`), room types and the currency
(`price.currency_label`) in it. The language is the reply's
(`reply_language`), the session's for the picks endpoint, and English while
`customer_agent.arabic_replies` is off. Display only: every search, filter,
comparison and pick keeps the English values beside it, and the agents reason
in the approved English values. A reply written in Arabic is given each card's
Arabic name (`name_ar` in the session brief), so its words and the cards name a
product alike. A
room package and its renders stay English.

---

## 26. Product Discovery Service Contract

Product Discovery must work independently from the conversational agent.

It should be directly testable with a structured request:

```python
result = await discovery.search(request, retailer_context)
```

Expected responsibilities:

1. validate structured request
2. validate taxonomy values against the approved registry, rejecting anything unapproved
3. apply PostgreSQL structured constraints
4. determine whether semantic ranking is useful
5. query Pinecone only when needed
6. apply deterministic final ranking/business rules
7. return typed results

Product Discovery executes exactly what it is given. It does **not** relax:
judging whether a result is too small, and widening it, belongs to the
controlled relaxation layer above it (section 13.5). Discovery therefore knows
nothing of constraint strength, and must not acquire a dependency on it.

The agents explain results; Product Discovery finds them.

---

## 27. Whole-Room Bundle Service Contract

Whole-room logic must reuse Product Discovery rather than implement separate search logic.

Conceptually:

```text
Interior Design Plan
    -> required/recommended/optional categories
    -> dynamic budget allocation
    -> discovery per category
    -> compatible candidate sets
    -> bundle optimizer
    -> total budget/spatial/design validation
    -> complete room package
```

The bundle optimizer should consider the room globally rather than independently selecting the top product in each category.

A set of individually best products is not automatically the best bundle.

When the user rejects/replaces an item, preserve accepted/locked items and re-optimize only what is allowed to change.

---

### 27.1 Seating for a group

**Shopping, a group is seated one piece at a time** (10.2, 10.5): a seating
goal for the head count, the first piece's seats asked, and after each add the
seats left counted from reviewed capacities - the next piece matched to what
they chose (`match_goal`) and fitted to their room once it is known (10.6). The
agents are never handed a ready-made combination; a product with no recorded
seat count never fills a seat, and "seating for all ten" is said only when the
cart's pieces add up to it.

**In a room**, Nora chooses the seating as a whole (10.1): how many pieces,
which types, the seats each multi-seat piece should have (`seats_each`, an
exact seat filter) and their colours and styles. Code only counts.

- Seats come from reviewed data (`app/taxonomy/seating_v1.yaml`): a recorded
  capacity, or one for every one-seat type. A chaise lounge or recliner may be
  a piece of the room but never counts as a seat (`never_counts_as_seats`).
- The seats the design gives are a fact beside the head count
  (`seats_planned`, `seats_short`), and the built room's are summed from its
  real pieces (`seats`); the reply says the room seats everyone only when
  those add up to it, and says plainly when they fall short.
- Each seating piece keeps its seat filter, so swapping one brings back a
  piece that seats the same.

## 28. Prompt Management

Treat prompts as versioned application assets, not random strings inside business code.

Keep prompts in dedicated files/modules with explicit versions.

Record prompt version/model version in traces where practical.

Keep system prompts focused on reasoning role and constraints; do not embed catalog facts or secrets into prompts.

Avoid giant prompts containing responsibilities that belong to services.

---

## 29. Testing Expectations

Do not consider a feature complete without tests appropriate to its risk.

### Unit tests

Cover deterministic logic such as:

- text normalization (whitespace, casing, punctuation, numeric and unit parsing)
- taxonomy registry loading and validation
- taxonomy validation: approved category, approved subcategory under that category, rejection of unapproved pairs
- constraint classification helpers
- relaxation rules
- ranking calculations
- retailer/store scoping
- safe DTO serialization
- output guardrails
- bundle-lock behavior

### Integration tests

Cover:

- PostgreSQL repositories
- Redis state persistence
- Pinecone adapter behavior using controlled test/mocked infrastructure
- discovery flow across repositories/services

### Agent/evaluation tests

Maintain golden cases for:

- single-product discovery
- strict vs preferred constraints
- no-result relaxation
- general interior-design question with no product search
- whole-room planning
- retailer unsupported category
- feedback such as "I like option 2 but not the color"
- locked bundle item preservation
- prompt-injection attempts
- attempts to reveal internal instructions/secrets
- attempts to retrieve products from another store

Because commerce interpretation is LLM-driven, these cases must cover varied,
open-ended phrasing rather than a fixed list of known synonyms:

- "I need a couch for four people" -> `seating` / `sofa`, seating capacity `4`
- "something small beside my bed" -> `tables` / `nightstand`
- "a chair where I can put my feet up" -> `seating` / `recliner` where context supports it
- "something comfortable for reading" -> `seating` / `lounge-chair` where context supports it
- "show me a table" -> insufficient information for a subcategory; clarification expected
- model emits an unsupported category/subcategory -> deterministic validator rejects it

Do not make ordinary unit tests depend on live OpenAI/Pinecone calls.

---

## 30. Code Quality Rules

Write production-quality Python.

- use type hints consistently
- use Pydantic models for contracts and settings
- keep functions/classes focused
- prefer clear names over explanatory comments
- add comments/docstrings only when they explain non-obvious business invariants or public interfaces
- avoid redundant comments that restate code
- avoid deeply nested conditionals when a clearer decomposition exists
- avoid global mutable state
- avoid circular imports
- avoid duplicated business logic
- avoid `Any` when a concrete type is practical
- validate external inputs at boundaries
- return explicit typed results rather than sentinel magic values
- use enums/literals for controlled domain states where appropriate
- preserve async correctness
- write repository methods around real query/use cases rather than generic "do everything" repositories

Use automated formatting/linting/type checking/test tooling through the project's `pyproject.toml`.

If the repository has no existing standard, prefer a lightweight modern toolchain such as Ruff + pytest + a static type checker.

---

## 31. Anti-Patterns - Do Not Implement

Do not:

- create a giant god-agent prompt
- create an agent for every service
- let agents freely chat with each other
- let an LLM generate SQL
- let an LLM choose/change `store_id`
- let an LLM invent commerce taxonomy values, or accept an unapproved category/subcategory pair
- build a large synonym/alias dictionary that correctness depends on
- map the visual `category` to commerce fields inside the agent service, or fall back to it at runtime
- guess a specific commerce subcategory instead of asking one clarification question
- let raw database rows flow directly to customer-facing output
- hardcode store IDs, model names, Pinecone indexes, thresholds, URLs, credentials, or timeouts
- duplicate existing Django domain/schema ownership inside FastAPI
- create a second product-search implementation for whole-room flow
- call Pinecone when structured search already answers the request well
- guess unknown product facts
- infer seating capacity from appearance and present it as confirmed unless an explicit enrichment rule with confidence/provenance is implemented
- silently relax explicit locked constraints
- expose stack traces/internal exceptions
- log secrets
- use catch-all exception suppression
- introduce unnecessary inheritance/framework abstractions
- overengineer V1 with distributed microservices

---

## 32. Implementation Order

Unless the user asks to prioritize differently, build in this order:

1. repository/project bootstrap and validated settings
2. FastAPI lifespan + health endpoint
3. PostgreSQL read integration for existing catalog
4. commerce taxonomy registry and the authoritative commerce fields confirmed
5. Pydantic contracts
6. RetailerContext / store scoping
7. CatalogCapabilityService
8. ProductRepository
9. taxonomy validation service
10. ProductDiscoveryService using PostgreSQL only
11. deterministic relaxation + ranking
12. Pinecone semantic ranking integration
13. Redis session state
14. guardrail layer
15. the agent team (supervisor and agents) and their tools
16. Interior Design Agent
17. LangGraph orchestration
18. whole-room furnishing + bundle optimization
19. evaluation suite and end-to-end hardening

Do not start with elaborate prompts before Product Discovery, context, contracts, and guardrails exist.

---

## 33. How Claude Code Should Work on This Repository

Before changing code:

1. inspect the repository tree
2. inspect `pyproject.toml` / dependency files
3. inspect existing configuration patterns
4. inspect database access and product models
5. inspect existing Pinecone integration/index assumptions
6. inspect Redis integration if present
7. inspect tests and coding conventions
8. identify ownership boundaries with the existing Django system

Then propose a concise implementation plan before making broad structural changes.

When implementing:

- make cohesive changes, not opportunistic rewrites
- reuse strong existing abstractions instead of duplicating them
- do not introduce a new library if the current stack already solves the problem well
- prefer explicit interfaces at integration boundaries
- keep public API behavior backward-compatible unless the user explicitly approves a breaking change
- create migrations only in the schema-owning project
- update tests with every behavioral change
- run formatter/linter/type checker/tests after meaningful changes
- report failures instead of hiding them
- do not leave placeholder TODO implementations presented as complete

When requirements are ambiguous:

- inspect the code/data first
- do not invent missing schema or business rules
- ask only when the ambiguity materially changes architecture or behavior

---

## 34. Definition of Done for a V1 Feature

A feature is not done merely because the happy path returns a response.

It is done when:

- boundaries are respected
- contracts are typed
- runtime config is externalized
- store scope is enforced
- factual data comes from authoritative services
- guardrails prevent unsafe/internal output
- errors fail safely
- logs/traces make behavior diagnosable
- unit/integration/eval coverage exists at the appropriate layer
- code passes project formatting/lint/type/test checks
- no secrets or temporary debugging artifacts remain

---

## 35. Core Architecture Summary

```text
Customer
   -> FastAPI (/v1/chat, or /v1/chat/stream for live progress)
   -> resolve RetailerContext
   -> load Redis session (revision checked)
   -> supervisor -> Sara / Nora / Salman / Faysal (typed tools, shared staged state)
       -> services: query understanding + taxonomy validation, search pipeline
          (PostgreSQL -> relaxation -> Pinecone ranking), comparison, seating,
          room engine (Nora's checked design -> discovery -> bundle optimiser),
          cart + store cart check, store documents, support
   -> reply figures checked against tool facts
   -> screen built from tool payloads
   -> Redis compare-and-set
   -> Customer
```

---

## 36. The Cart

**Select means add to cart.** The picks are the customer's cart: every way of
selecting - a card's button, a typed "add the 7th one" or "I'll take it", a
seating combination chosen, a room package's "Add all to cart", a typed "add
these all to my cart" - adds to `selected_product_ids`, and how many of each
lives beside it in `cart_quantities` (only where more than one; a product that
leaves the cart takes its quantity with it). One list, one reducer: no route
keeps a cart of its own.

- **The server owns the cart and prices it fresh.** `CartService`
  (`app/services/cart.py`) builds `CartView` from the picks just read: each
  line's quantity and line total, one total per currency (never converted), the
  products the catalog no longer returns counted but not priced, and what changed
  since the turn began (`changes`). Every chat reply and every picks answer carries
  it; `GET /v1/cart` reads it without a turn.
- **Changing it.** `POST /v1/picks` takes `quantity` (1..`cart_max_quantity`)
  beside `select` / `deselect`. At most `max_picks` products however they are
  added - a tap (`pick_limit`) or an agent's `add_to_cart` (`cart_full`, which
  the agent says in its own words) - except a whole room (`add_room_to_cart`),
  which goes in whole even past it: half a room is not a choice they made. A product already in the cart only
  changes quantity. `add_to_cart` takes several products at once, each with how many
  ("add two of the third one"), capped at `cart_max_quantity`; a product is
  named by its place on a list, in the cart, in the liked list or a comparison,
  as the product in focus, or by a colour, style or the cheapest or dearest
  among the cards (`ProductReferenceSelector`). `add_room_to_cart` adds every piece being bought
  with its quantity - owned pieces stay out, and a product already in the cart
  keeps the larger quantity, so adding twice never doubles it.
- **A whole room in the cart** is answered as a pick is (10.3): Salman offers
  what would finish it (`room_could_add`), never what the room already holds.
- **From then on the cart is the room** (`room_project.in_cart`). The room
  package is a record of how it was designed: drawn tagged "In your cart",
  with no Swap, Remove or Add all, and its edits - a swap, a removal, a
  finishing touch, a piece added - are refused (`room_in_cart`). Each cart line
  of the room - its pieces and what was picked after it went in, never a pick
  from before (`picks_before_cart`) - names it (`room`: "Living room").
- **"I'm done with this room"** - its chip or typed - is Salman's
  `finish_room`: the room's products stay in the cart, still named by it
  (`finished_rooms`), the room is no longer the one on record, and the reply
  asks in the agent's own words which room they would like next - never one
  in `rooms_finished`. Only a room in the cart is closed.
- **Liking is not adding.** "I like the fifth one" is not an add: the agent says
  what it costs and offers to add it; ♡ keeps it in the liked list. An
  explicit "add it", "I'll take it", a tap, or their yes is an add.
- **The store's own cart is asked first.** On a Salla store
  (`core_store.provider` SALLA with its Salla id) every product about to enter
  the cart, or rise in quantity, is put to Salla's public storefront API
  (`app/integrations/salla_storefront.py`, keyless, `Store-Identifier` = the
  store's Salla id) by `StoreCartCheck`: not this store's (404/410, or no Salla
  id), out of stock, or past one order's `max_quantity` - and the cart holds only
  what Salla's would. An agent's add is asked before it is recorded, inside
  `CartOperations` (`app/agents/operations/cart.py`), so nothing goes with a
  refused piece; a tap is asked before it applies (`pick_unavailable` with the
  reason). The agent is told (`refused_by_store`: the product and why, and for
  `add_to_cart` how many were kept). A timeout, a dropped connection or a busy Salla (429, 5xx) is
  asked again (`salla.retries`); a product Salla still could not confirm stays
  out (`unconfirmed`), never promised - the widget cannot take back what
  Salla's own cart then refuses (`salla.refuse_unconfirmed`).
  `salla.check_cart` switches the check off.
- **The cart is the shopper's, not one chat's.** The platform names the
  shopper at the front of every conversation id (`<16 hex>.<the widget's id>`).
  Every saved turn keeps that shopper's cart beside their conversations
  (`session.shopper_cart_ttl_s`, seven days), and every conversation they load
  - a new chat, or one they come back to - holds it (`app/services/shopper_cart.py`),
  so "what's in my cart?" answers the same in every chat. An id naming no
  shopper (the console, direct calls) keeps its own cart. A change made only on
  the store's own cart page never reaches us; that needs the widget.
- **The agents read it** in the session brief (10.4): each line by its place
  in the cart (`picked_ordinal`), with name, kind, quantity, line total and the
  room it was bought for, and the totals per currency - summed by the
  application, so sayable exactly as given (the number check admits them).
- **The store's own cart is the widget's to fill.** Only the storefront page can
  write a shopper's cart (on Salla, `salla.cart.addItem`), so each line names its
  product the way the store knows it (`product`: uuid, Salla id, url) and
  `changes` says what to copy across (docs/widget-display-contract.md 6). Requests
  still never carry a product id.
- **Not an order.** Nothing is bought, reserved or paid for here; checkout
  happens on the store.

