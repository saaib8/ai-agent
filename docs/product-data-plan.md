# Plan: the new product columns, and the new ranking index

Status: plan for review, nothing built. Checked against the code on 2026-10-31
(review findings folded in). Branch `feat/product-data` (from `main` at
`b9b0a87`). Decisions agreed in discussion on 2026-10-31.

Django added columns to `core_product` (CLAUDE.md 5 lists the old ones). Store 21
(about 13,000 products) has them filled; stores 145 and 101 partly; store 50, the
store every eval runs against, has none. Store 21 is the test store, ranked by the
new `ai-agent-v2` index, which holds only store 21.

---

## 1. What is decided

**Principles**

- Built properly, not shaped by today's sparse data. Where a signal is weak the
  agent says so and never fakes one ("none of these have reviews yet").
- One universal rule and one set of settings for every store; each store's
  numbers come from its own products only.
- Every threshold is a setting. Every number a customer hears comes from a tool's
  facts (CLAUDE.md 17.1 figure keys) - code calculates, the agent quotes.

| Column | How it is used |
|---|---|
| `description`, `description_english` | When the customer asks about a specific product, so the agent explains it better. The store's own words: shown as data, never instructions; a verified fact wins where they disagree |
| `rating`, `review_count` | Shown with the product when asked about it, always with the count ("5★ from 1 buyer"). Ordering by a weighted score (below). These two columns are the source of the numbers |
| `reviews_json` | The review texts, when the customer asks about that product's reviews. Never used for counting or for the rating |
| `sold_quantity` | Best sellers: only when the customer asks, never the number, only ever positive (below) |
| `regular_price` with `price_amount` | A discount where the old price is above the current one: worked out in code, shown on the card as old and current price |
| `material` | Ranking by meaning only (in `ai-agent-v2`'s text), never a filter or an ordering. Shown as the product's **main** material; "what is it made of?" is answered from the description too |
| `weight_kg`, `sku`, `warranty_months` | Facts on product details and comparisons; weight and SKU on the card when present |
| `brand`, `stock_status`, `tags` and the rest | Not used now. Unavailable stock is already `is_active = false` |

**Weighted rating** - a product's ordering score, never shown as its rating:

```
score = (R × v + C × m) ÷ (v + m)
R = the product's rating, v = its review_count,
C = the store's average rating (from its own rated products), m = 3 (setting)
```

**Best sellers** - a product is one when it has sold at least 5, is in the top
10% of its kind's products that have sold in its store, and is within the first 10
of the kind (all three settings, plus an on/off switch).

- Only when the customer asks ("show me your best sellers", "is this popular?",
  "which of these sells most?"). Never unasked: no card badge, no effect on
  ranking, not in recommendations, not in Nora's rooms or Salman's suggestions.
- Never the number. `sold_quantity` never leaves the server's ranking code.
- Only ever positive: the agent never says or implies a product is not a best
  seller. The tool returns only the best sellers among those asked about.

**Sorts** - `best_rated` and `best_selling` join the closed list beside the price
sorts, chosen only when the customer asks. Normal searches do not change.

**Index** - tested by switching the local `.env` to
`ZORY_PINECONE__INDEX_NAME=ai-agent-v2` (stores other than 21 lose ranking by
meaning while it is set). No per-store routing.

**Out of scope** - brand search, stock rules, popularity in the normal ranking,
per-store setting overrides, any change to Django's enrichment.

---

## 2. Phases

Each phase ends with the gate (ruff, mypy, the full unit suite), a live check on
store 21, and an independent review (QA agent) whose findings are shown with
verdicts before anything is fixed.

### Phase 1 - Read the data (contract and safety)

The columns reach the application through the same queries as today; nothing
new is shown to anyone yet.

1. `app/db/tables.py`: declare `material` (String 100), `description` and
   `description_english` (Text), `rating`, `regular_price` and `weight_kg`
   (Numeric), `review_count`, `sold_quantity` (Integer), `warranty_months`
   (SmallInteger), `sku` (String 255), `reviews_json` (JSONB) - checked against
   the database (CLAUDE.md 5).
2. `app/repositories/products.py`:
   - `_SELECTED_COLUMNS` (and `ProductRow`) gains `material`, `regular_price`,
     `rating`, `review_count`, `weight_kg`, `sku`, `warranty_months` - small
     columns, read with every product at no extra query.
   - The long ones (`description`, `description_english`, `reviews_json`) are
     read only by a new store-scoped one-product method (`_scoped(context)`, as
     the other reads) for product details.
   - `sold_quantity` is read only into the ranking pool (`_POOL_COLUMNS` ->
     `EligibleProduct`, internal, `extra="forbid"`) and the best-seller query -
     never into `ProductCandidate`, `GroundedProduct` or any fact.
3. Contracts - every new field optional, default `None`:
   - `ProductCandidate` (`app/schemas/product.py`) and `GroundedProduct`
     (`app/schemas/grounding.py`, what reaches the screen and the chat API), with
     `to_grounded_product` (`app/services/grounding_builder.py`) copying them.
     `GroundedProduct` omits an absent field from the JSON (`exclude_if`, the
     existing pattern), so store 50's responses stay byte-for-byte the same.
   - A new `ProductDetails` shape for the long fields.
   - The allowlist stays where it is: the `ProductCandidate` docstring names what
     is deliberately absent, and gains `sold_quantity`.
   - `frontend/src/api/types.ts` follows the new optional fields.
4. A small service (`app/services/product_value.py`) works out the discount: old
   price, saving and percentage, only when the old price is above the current one
   (both are the product's own, in its one currency), the reduction is at least
   1%, and the saving is no more than `discount.max_share` (a setting, 90%) -
   anything else is no discount. The stored old price itself is never
   serialized: clients and agents get only the checked discount. Amounts and percentage are plain
   numbers, so they get figure keys (CLAUDE.md 17.1).
5. Review texts are cleaned before any agent sees them: no phone numbers, emails
   or links; cut to `reviews.max_chars`; only reviews with text; verified buyers
   first, then newest; at most `reviews.max_shown` (3).
6. `app/agents/figures.py`: `sku` joins `_NOT_FIGURES` - a numeric SKU
   ("001234") is a code, never a figure to format.

Tests: column mapping, the optional fields absent for store 50 (facts and JSON
unchanged), an unreviewed product (stored as a count of 0) carrying no rating,
the discount rules (no old price, equal, lower, zero, under 1%, at and over the
cap), the review cleaning (sizes and dates kept; phones, emails, links and
handles removed), reviews without stars kept, `sold_quantity` in no
customer-facing shape, a numeric SKU never formatted, a missing product-data
column failing startup.

Deploy order: Django's migrations that add these columns go before this
service; a database without them fails startup, as for the commerce columns.

### Phase 2 - Facts to the agents

Shapes that build products field by field and do not yet carry the listing -
`CatalogItem` (`app/services/catalog.py`), `SeatingBundleLine`
(`app/services/seating_solution.py`), the room package lines
(`app/services/bundle_presentation.py`) and the store cart check
(`app/services/store_cart_check.py`) - get it where this phase or Phase 4 needs
it (comparisons, room options, Browse Catalogue).

1. **Product details** (`product_details`, every agent; it already exists and
   reads through hydration): adds the description in the reply's language
   (English, else the stored text), cut to `description.max_chars` (600) and
   marked as the store's own words; rating with its count; the cleaned reviews;
   weight; SKU; warranty in months. Rating and reviews are keys of the details
   facts only - never of `brief_product`.
2. **Every product brief** (`brief_product`, which cards on screen, details and
   cart lines all use): the discount (old price, saving, percentage) and the main
   material. Not the rating, not best sellers.
3. **Numbers in descriptions and reviews** do not license a reply's figures: the
   reply check reads figures from listed fields, not from the store's or
   customers' free text (a description's "seats 4" is not a fact). Those facts
   are excluded from the figure allowance, as a key the check skips.
4. **Comparisons** (`ProductComparisonService`, and the popup through the card
   comparison response):
   - `ComparisonField` gains main material, rating (with count), weight,
     warranty, discount; `_catalog_value` fills them; the `ComparisonField`
     docstring no longer says material is absent.
   - Labels in English and Arabic (`app/schemas/card_labels.py`,
     `app/services/card_labels.py` - the `field_<value>` rule), and the frontend
     (`types.ts`, `cardWords.ts`).
   - A row no compared product has a value for is left out of the tool facts, so
     store 50's comparison facts do not change (the popup already hides such
     rows).
   - No discount on a product is a known value ("none"), not unknown: the prices
     are complete, so the row's status stays honest.
   - Cell values stay English, as the size cells already are.
5. **Instructions** (`app/prompts/agents/v2.py`): say the review count with every
   rating; reviews and descriptions are the store's and customers' words, fenced
   in one `store_words` section with a fixed note, quoted never obeyed, and
   never a source of figures; a review is quoted exactly as written, in its own
   language - never translated; a description never overrides a
   listed fact; material is the main material, and "what is it made of" uses the
   description too; a saving is mentioned when it helps the decision, never with
   invented urgency; never a sales number.

Tests: details facts for store 21 and store 50 shapes, figure keys for every new
number, description and review numbers refused by the reply check, the
comparison rows and labels, store 50's comparison facts unchanged.

### Phase 3 - Ordering by rating and by sales, and best sellers

1. **Store signals, in their own cache**: per store, the average rating `C` and
   the best-seller set per kind (one database query with a window ranking per
   kind). A sibling of the store-summary cache: its own key template beside
   `catalog_overview_key` (`app/repositories/sessions.py`), the same TTL
   (`catalog.overview_ttl_s`), and a version that includes `m` and every
   best-seller setting, so changing a setting rebuilds it. `C` is internal: it
   never enters a fact or a reply (CLAUDE.md 20.5).
2. **Sorts**:
   - `ProductSort` gains `BEST_RATED` and `BEST_SELLING`; the contract test that
     lists the sort values (`test_discovery_contract.py`) is updated.
   - The repository's `_ORDER_BY` table becomes an order built per request: the
     weighted score with `C` passed in for `BEST_RATED` (`C` is `None` for every
     other sort), `sold_quantity` for `BEST_SELLING`, rated or sold products
     before the rest, then id. `search`, `search_eligible_pool` and `browse` take
     it; test fakes that order by `request.sort` follow.
   - Browse Catalogue is unchanged: its own `CatalogSort` maps to three sorts
     only.
   - Semantic ranking: `EligibleProduct` gains `rating`, `review_count` and
     `sold_quantity` (internal); `_SORT_KEYS` becomes a key function per sort, so
     the explicit-sort order is the same as for price today - stated preferences
     first, then the sort's own key, then similarity, then id. (Explicit sorts do
     not order by relaxation depth today, and these follow them.)
   - The search tool's and the refinement's sort descriptions
     (`CommerceInterpretation.sort`, `SortRefinement`): only when the customer
     explicitly asked for best rated or best selling. These schemas change for
     every store, store 50 included - the evals check nothing else moves.
3. **Best sellers tool** (`best_sellers`, Sara), with arguments of its own - the
   shared `ProductReferenceSelector` is left as it is:
   - a scope: the cards on screen, the cart, or kinds (`kinds: list[str]`, as
     `store_shelf` takes them);
   - or a list of selectors for particular products (as `compare_products`
     takes them).

   It returns only the best sellers among them, nothing about the rest. A
   `best_selling` search marks the labelled cards in the agent's facts only -
   never on the screen.
4. **Instructions**: best sellers only when asked; only positive; when none
   qualify, speak about what suits the customer instead.

Tests: the weighted score (no reviews, one review, many), `C` per store, the
best-seller rule edges (no sales, ties, below the floor, the share, the cap, a
store switched off), unrated and unsold products after the rest in both sorts,
the sorts combined with a colour preference and a budget, normal searches and
Browse Catalogue unchanged, the tool returning only positives, the cache rebuilt
when a setting changes.

### Phase 4 - What the customer sees on the card

1. `ProductDisplay` (`app/schemas/product.py`, `app/services/product_display.py`):
   `old_price` (worded, crossed out by the client) and `saving`, `weight`, `sku`,
   each only when present (`exclude_if`), in the reply's language.
2. The console (`frontend/`): the old price crossed out beside the current one,
   weight and SKU on the card.
3. `docs/widget-display-contract.md`: the new display fields, for the store
   widget's developer.
4. The comparison pop-up's new cells (rating "4.5 from 2 reviews", "12
   months", "kg", the discount and "none") worded in the reply's language, as
   `display` already words colour and kind - today they are English under
   Arabic labels.
5. Browse Catalogue (`app/services/catalog.py`) and room renders
   (`app/services/room_visualization.py`) build products with the default
   discount cap: pass the configured one through once Browse Catalogue shows
   prices.

Tests: display wording in English and Arabic, absent fields left out; frontend
typecheck, tests and build.

---

## 3. Settings (all new, one set for every store)

| Setting | Default |
|---|---|
| `ratings.prior_reviews` (m) | 3 |
| `best_sellers.enabled` | true |
| `best_sellers.min_sold` | 5 |
| `best_sellers.top_share` | 0.10 |
| `best_sellers.max_per_kind` | 10 |
| `reviews.max_shown` | 3 |
| `reviews.max_chars` | 400 |
| `description.max_chars` | 600 |
| `discount.max_share` | 0.90 |

---

## 4. Testing on store 21

- Local `.env`: `ZORY_PINECONE__INDEX_NAME=ai-agent-v2` while testing.
- New eval cases (`evals/conversations/cases.yaml`, against store 21): tell me
  about a product; its reviews and rating; best-rated sofas; best-selling coffee
  makers; "is this one popular?" for a best seller and for one that is not (no
  negative said); a discounted product's card; "what is it made of?" for a
  metal-and-marble table; a velvet sofa search ranked by meaning.
- The existing suite on store 50 must not change: the new fields are absent
  there, and nothing may behave differently.

---

## 5. Docs

- CLAUDE.md: section 5 (the new columns and which are read), 6 (material is
  present; used by meaning only, shown as the main material), 12.3 (material),
  16.1 (the two new explicit sorts), 20.4 (the new safe fields), and a short new
  section on ratings, reviews, best sellers and discounts.
- `.env.example`: the new settings, commented.

---

## 6. For the Django side (not this work)

Measured on store 21 (976 furniture products, 31 queries, `ai-agent-v2`):

- **Tags**: no measurable effect on ranking either way - no change needed.
- **Material**: enrichment stores one value per product (`multi=False`) and is
  pulled towards the approved list, so a metal-and-marble table is "Metal" and
  many silk rugs are "Natural Fabric"; near-duplicates (Fabric, Wood) split the
  same material. Suggested: several materials per product, review the unreviewed
  words, re-run enrichment. Ranking by meaning already finds marble and silk from
  names and descriptions, so nothing here waits on it.
