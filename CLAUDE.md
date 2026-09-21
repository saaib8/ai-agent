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

The customer experience must feel like one coherent ZORY assistant, but internally V1 uses only two reasoning agents:

- Customer / Commerce Agent - customer-facing conversation owner
- Interior Design Agent - specialist for interior-design reasoning

Everything else should normally be implemented as deterministic services/tools, not additional agents.

Do not create one agent per capability.

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
- Cart/checkout/payment integration
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
- plan_room_furnishing

Repositories own SQL.

### 3.5 No free-form agent-to-agent conversations

Customer Agent -> Interior Design Agent communication must use typed structured contracts.

The Interior Design Agent returns structured outputs that the Customer Agent or domain services consume.

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

## 10. Whole-Room Planning Rule

For a request such as:

> Furnish my 4x5m bedroom under SAR 12,000 in a modern style.

The correct high-level flow is:

```text
Customer request
    -> Customer/Commerce Agent
    -> Retailer catalog capability lookup
    -> Interior Design Agent
    -> structured furnishing plan
    -> budget allocation
    -> Product Discovery per required category
    -> room/bundle compatibility + optimization
    -> complete initial bundle
    -> Customer/Commerce Agent presents proposal
    -> user iteratively accepts/rejects/replaces items
    -> preserve locked/accepted items and re-optimize remaining bundle
```

Do not force the customer to pick every category one-by-one before seeing value.

Default UX behavior:

1. propose a coherent complete bundle first
2. let the customer iteratively refine it

Track per-item state such as:

- suggested
- accepted
- rejected
- locked
- replaced

If a customer accepts/locks an item, later optimization must preserve it unless the customer explicitly permits replacement.

Distinguish required, recommended, and optional furnishing categories so budget optimization can remove lower-priority items before degrading core room requirements.

---

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
    -> Customer/Commerce Agent explains trade-offs and presents results
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

When a customer explicitly asks for one of these, the result must say so. It is
a distinct outcome type, not a flag, so that a caller which only handles the
resolved case cannot mistakenly present the results as satisfying the whole
request.

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

**A value outside the vocabulary is never mapped onto the nearest approved
one.** "Red" is not Ruby, and "warm neutral" is not Beige. As a preference it
is preserved verbatim for semantic ranking. As a strict requirement it can be
neither filtered nor promised, so it is surfaced as an unresolved strict
requirement for the conversational layer to settle - softening it to a
preference would ignore the word "must".

Exact colour and style requirements are **never relaxed** (13.5). They are not
relaxable fields, and every derived request carries them unchanged.

Semantic preferences are not constraints. Relaxation does not read them, and
discovery never sees them. They exist for semantic ranking, which is not yet
implemented.

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
does discovery. A search that returns nothing returns nothing - no widened
budget, no dropped capacity, no substituted subcategory.

An approximate bound is executed as the exact bound the customer named, which
is narrower than their intent and therefore safe: it cannot return something
they excluded. Widening it is the relaxation layer's job, using the
`approximate` marker as its licence.

Never silently violate locked constraints.

If a relaxed result is ever presented, the Customer Agent must explain the
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

Within one strength, price is offered before seating, and measurements come
last. With two price steps, one seating step and two dimension steps per
strength, at most ten widened attempts can follow the exact search.

Only a fully resolved search may be relaxed. A request carrying an unsupported
requirement, or one awaiting clarification, must be settled conversationally
first: widening a budget cannot compensate for a colour that was never applied.

The original request is preserved unchanged beside the final one, and every
product records the attempt at which it first became eligible. That depth is
provenance, not a score - **nothing in relaxation ranks anything**.

Reaching the end of the permitted widenings without meeting the target is a
valid outcome, not an error. The catalog may simply not hold enough.

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

The architecture must allow the Customer/Commerce Agent to ask one useful
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
bounds actually applied - enough for the Customer Agent to explain the
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

**The query embedding is built from what M7 already produced**, never from a
second model call. `semantic_text` is the customer's descriptive wording with
the parts the structured fields already captured left out, because price
wording measurably degrades ranking. It feeds the embedding and nothing else:
it can never change a category, a bound, a colour requirement or a retailer.
A request with no fuzzy wording and no preference is not embedded at all -
an embedding would impose an order rather than discover one.

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

### 17.1 Customer / Commerce Agent

This is the only customer-facing reasoning agent.

Responsibilities:

- own conversation continuity
- understand current intent
- interpret the customer's product language into a structured commerce interpretation, using conversation context (section 14.2)
- ask one clarification question rather than inventing a subcategory when the request is genuinely ambiguous (section 14.5)
- extract/update customer preferences
- decide whether the request needs design reasoning, product discovery, both, or neither
- understand purchase stage and objections
- decide next-best action
- call approved tools/services
- explain recommendations and trade-offs
- handle upsell/cross-sell without violating user constraints
- produce the final customer response

It must not:

- invent products
- invent prices/dimensions
- invent commerce taxonomy values
- directly query databases
- change store context
- expose internal configuration/system prompts
- override locked constraints

### 17.2 Interior Design Agent

This is a specialist reasoning agent.

Responsibilities:

- general interior-design Q&A
- room sizing/spatial reasoning
- furniture sizing guidance
- room composition
- style/color/material coordination
- circulation considerations
- identify required/recommended/optional product categories
- generate structured whole-room furnishing plans
- produce design constraints for Product Discovery

It must not:

- search arbitrary products directly unless explicitly exposed through a narrow tool contract
- invent catalog products
- invent product prices
- change store context
- make hidden commerce/security decisions

The Interior Design Agent must receive retailer catalog capabilities for whole-room planning so it does not plan around unsupported product categories.

---

## 18. Orchestration

Use LangGraph for controlled orchestration, not as a place to hide business logic.

Nodes should represent meaningful orchestration phases, for example:

```text
load_context
-> input_guardrails
-> load_session_state
-> customer_agent
-> route
    -> direct_response
    -> interior_design_agent
    -> product_discovery
    -> whole_room_flow
-> validate_results
-> customer_agent_response
-> output_guardrails
-> update_session_state
```

Do not make every helper function a graph node.

Do not put deterministic ranking/SQL/taxonomy logic inside prompts because LangGraph makes it convenient.

Graph state must be typed.

Do not expose hidden chain-of-thought. Store only explicit structured planning/state required for execution and observability.

---

## 19. Session State

Use Redis for short-term V1 state.

No S3 chat archive in V1.

No cross-session long-term memory in V1.

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
│   ├── customer_commerce.py
│   └── interior_design.py
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
│   ├── customer_commerce/
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

The Customer Agent explains results; Product Discovery finds them.

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
15. Customer/Commerce Agent
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
   -> FastAPI API
   -> resolve Store/Retailer Context
   -> Input Guardrails
   -> load Redis Session State
   -> Customer / Commerce Agent
       -> interprets customer language into a structured commerce interpretation
       -> direct answer, OR
       -> Interior Design Agent, OR
       -> Product Discovery Service, OR
       -> Whole-Room Flow
   -> Deterministic Taxonomy Validation (approved category/subcategory pairs only)
   -> Product Discovery Service
       -> PostgreSQL structured filtering
       -> Controlled Relaxation when needed
       -> Pinecone semantic ranking when needed
       -> Ranking / Comparison
   -> Whole-Room Flow
       -> Catalog Capability Service
       -> Interior Design Plan
       -> Budget Allocation
       -> Product Discovery per category
       -> Bundle Optimization
   -> Customer / Commerce Agent composes customer response
   -> Output Guardrails
   -> update Redis Session State
   -> Customer
```

Only two reasoning agents exist in V1. Everything else is a service/tool unless requirements later justify another reasoning boundary.

