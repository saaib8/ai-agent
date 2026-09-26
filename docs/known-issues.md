# Known Issues

Issues reported from using the application, with the cause found in the code
and the proposed fix. Status is **Open** until a fix is made and verified.


## Agreed direction (2026-09-24)

The fixes below make the app a better assistant, but on their own they add
**scripted rescue paths** for situations predicted in advance. A situation
nobody predicted, such as "a sofa that fits through an 80 cm door", would
still hit a dead end. Adding each fix as another special case in
`turn_coordinator.py` would also keep growing the code that is already too
large.

What makes the agent smart is the **"look and decide" loop** (issue 1): the AI
sees the result of each step and chooses its next move from a toolbox of safe,
store-locked tools. That is how it handles **unknown paths that appear
later**.

**Agreed order of work:**

1. **Quick wins in the current system.** These are needed either way:
   - the 502 recovery ladder (issue 6);
   - colour mapping (issues 2 and 5);
   - typed "show more options" (issue 7);
   - writer tone (issue 10).
2. **Conversation test suite.** Define "smart" as real scripted conversations
   (8-seat sofa, dark gray, "only the sofa", etc.) before building the loop.
3. **Agent loop with tools** (issue 1), reusing the existing search pipeline,
   bundle optimiser, designer and catalog capability service.
4. **Remaining fixes as tools, not scripted paths:**

   | Fix | Tool |
   |---|---|
   | Seating bundles (issue 4) | `build_combination` |
   | Dead-end recovery (issue 9) | the loop itself |
   | Category suggestions (F2) | `catalog_overview` |
   | Household size (issue 8) | asked when it matters |
   | Memory (issue 3) | `update_preferences` |
   | Room checklist (F3) | a step in the room flow |


## Plan review (2026-09-24)

The agreed direction covers every reported issue. Checking it against
everything found in the code review left the gaps and decisions below; they
are folded into the revised order at the end of this section.

**Verified:**
- **Pinecone text ranking works today** (confirmed by the user). The index
  holds text embeddings, so semantic wording ("cozy", "lighter") is ranked
  correctly. The preference-based fixes can rely on it.

**Decision - missing seat counts (confirmed by the user):** skip the seat-count
filter where the data is not present. Seat-count coverage in store 50:

| Coverage | Types | Seat-count handling |
|---|---|---|
| Well covered | sofa (166/173), sofa-set (31/31), sectional-sofa (13/15), sofa-bed (9/10) | Filter on it |
| Implied one-seaters (issue 4 decision) | chair types, stool, single-seater-sofa | Treated as 1 seat |
| Mostly missing | dining-table (1/4), chaise-lounge (1/2) | No filter; the requirement is used for ranking and mentioned in the reply |

Rules:
- A product with an unknown seat count is **not hidden**. It is shown after
  the products that confirm the seat count, and its card says the seat count is
  not listed.
- It is **never described as meeting the requirement**, and never counted in a
  seating-bundle total (issue 4).
- Designer plans ("a dining table for 6") follow the same rule, so a room is no
  longer left without a table because seat counts were never recorded.
- Rule change needed in CLAUDE.md 13.5, which currently says a NULL capacity
  must never be released.

**Decision - colour and style: soft by default (agreed 2026-09-24):**
Colour and style are handled by ranking, not filtering, except when the
customer explicitly rules alternatives out. This replaces the fixes in issues
2 and 5.

| Situation | Behaviour |
|---|---|
| Ordinary wish ("beige", "dark gray", "cozy", "Modern") | **No filter.** Products whose `main_color` / `styles` match the approved values the wish maps to come first (a deterministic code rule), then Pinecone similarity. Nothing is hidden. |
| Wish + explicit sort ("cheapest beige") | **Matching products first, by price**, then the rest by price. Without this, the price sort would show the cheapest sofa of *any* colour first. |
| Strict ("only black", "must be Japandi", "no other colours") | **Exact filter.** Reliable because `main_color` and `styles` are 100% approved values in the reviewed data. Zero results go down the recovery ladder: "No black ones, but here are our charcoal sofas." |

- The AI maps everyday words onto approved values, one-to-many
  ("dark gray" → Grey, Charcoal); code checks every value is approved.
- Pinecone alone is not enough for the "matching first" guarantee: to an
  embedding, charcoal and black are close, so a partition by the stored value
  is needed.
- CLAUDE.md 12.4 must be updated: one-to-many mapping allowed; a strict value
  outside the vocabulary goes to the recovery ladder instead of being
  unresolvable.
- **No tone tags on colours (decided by the user).** Customers describe colour
  in open-ended ways ("darker", "warm", "earthy", "soft"), so fixed tags such
  as dark/light or warm/cool would never cover them. The AI decides, per
  request, which approved colours match the customer's words. It is given:
  - the approved colour list;
  - the colours of the cards on screen, so relative words like "darker" mean
    darker than what is shown.

  Code checks that every chosen value is on the list.
- **The same colour set drives both behaviours.** For a wish ("show me darker
  sofas") it decides which products come first. For a strict request ("only
  dark ones") it becomes the PostgreSQL filter.
- **Must work for new searches and follow-ups alike.** Today only query
  understanding (new searches) has the colour list; the decision model (which
  handles follow-ups) does not. The fix must give follow-ups the same list and
  the same rules, preferably by routing follow-up colour and style changes
  through query understanding (issue 11).
- **Consistency risk:** the AI may choose slightly different sets for the same
  word on different turns. Mitigate with the conversation test suite ("darker",
  "lighter", "warm tones", "earthy", "only dark ones", as new searches and as
  follow-ups).

**Decision - when to ask before showing products (agreed 2026-09-24):**

| Situation | Behaviour |
|---|---|
| Single product search ("show me sofas") | **Ask and show in the same reply.** Products appear immediately, with one question and tap-to-answer chips: "Here are some sofas to start with. How many people usually need a seat?" [2] [3] [4] [5+]. The answer reorders or narrows the results. Never a question *instead of* products. |
| Whole room ("design my living room") | **Ask first**, on one screen: budget + household size (at most two questions, once), together with the category checklist (F3). Then build. |
| Genuinely ambiguous request ("show me a table") | Ask **one** question first, with chips of the store's real options, because guessing would show the wrong products. |
| Problem recovery ("only neon ones") | One question with chips (recovery ladder step 4). |

- Never ask for something already known (budget, household, room size on
  record).
- Never more than one question per reply, except the whole-room opening
  screen.
- Not adopted: always asking before showing for particular categories (e.g.
  seat count before any sofa is shown). This is possible later as a
  per-category rule if customer data shows it helps; the trade-off is one
  extra step before anything is on screen.

**Decisions - scope and success (agreed 2026-09-24):**
- **Language:** English only for now. Arabic is not supported in this phase.
- **Success criteria:** the work is done when every issue in this file is
  resolved (every numbered issue, every accepted gap and risk, and the planned
  features). Each one gets at least one scripted conversation in the test
  suite that fails before the fix and passes after it.
- **Catalog data** (material, `room_types`, descriptions, missing seat counts,
  other stores' commerce fields) is a known gap, worked on separately on the
  data side (G5, R3). The agent reports honestly what the data does not cover
  rather than guessing.

**Gaps not covered by the plan:**

| # | Gap | Fix |
|---|---|---|
| G1 | Plain searches ("show me sofas") show the 5 oldest products (sorted by id). The agent loop does not fix this. | A deliberate default order: a spread of prices and styles, or a popularity signal |
| G2 | "Sofas under 5000" asks which currency, although every store-50 product is SAR | Derive a store's currency from its catalog when it has exactly one clean currency; ask only otherwise |
| G3 | The "sofa 5" kind check is broken: the decision model has no category list, and "sofa" wrongly rejects a sectional sofa | Give it the taxonomy; match related types |
| G4 | Speed is not addressed, and the loop plus a better writing model make it slower | Latency target, streaming replies, a "thinking" indicator, faster models for simple steps, parallel room searches |
| G5 | Data gaps code cannot fix: no `material` column, `room_types` empty, dining-table seat counts, the recliner decision | Django / data-preparation work, tracked separately |
| G6 | "Matching colour first" is not guaranteed by Pinecone similarity alone | Code rule: products with the preferred colour first, then by similarity |
| G7 | Several fixes contradict current CLAUDE.md rules (12.4, 13.4, 13.5, 10) | Update CLAUDE.md before coding |

**Conflicts to resolve:**
- **Question budget for a whole room.** Budget + household (issue 8) + the
  category checklist (F3) must fit one screen, then no more questions.
- **"Only neon" must not be softened silently.** Recovery-ladder step 3 applies
  to preferences only; a strict request skips to step 4 (ask), or shows
  alternatives with a clear explanation.
- **Button confirmations:** F1's instant fixed confirmation is an agreed
  exception to issue 10's "every reply through the writer".
- **Tests come first.** Some current golden cases require the old behaviour
  (e.g. "sofas under 5000" → currency clarification) and must be updated with
  the fix.

**Revised order of work** (supersedes the order above):

0. **Prepare:**
   - update the CLAUDE.md rules (G7, seat-count decision);
   - write about 15 starter conversation tests.
1. **Quick wins:**
   - 502 recovery ladder;
   - colour mapping with "matching first" (G6);
   - typed "show more";
   - writer tone;
   - default product order (G1);
   - store currency (G2);
   - the decision model's category list (G3);
   - seat-count handling.
2. **Speed and timeouts (G4, R1):**
   - shorter per-call AI timeout and fewer retries (today one slow call can
     take ~3 minutes: 60 s x 3 attempts);
   - separate reasoning effort per step (fast and low for routing and query
     understanding, higher for the designer and writer);
   - an overall time limit per reply, with a friendly "that took too long, try
     again" message instead of a gateway error;
   - a request timeout in the frontend;
   - streaming replies and a "thinking" indicator;
   - parallel room searches.
3. **Full conversation test suite.**
4. **Agent loop**, with a question budget.
5. **Features as tools:** seating bundles, category suggestions, memory, room
   checklist.
6. **In parallel, data side (G5):** material, `room_types`, dining-table seat
   counts, the recliner decision.


## Open risks that can break the system (2026-09-24)

Ranked by how badly they break things. "Break" means an error screen, a
stuck conversation, a wrong answer stated as fact, or an outage, not just a
weaker reply.

| # | Risk | What breaks | Where tracked |
|---|---|---|---|
| R1 | **Turns can exceed gateway timeouts.** A reasoning call takes 9-20 s, and a search turn makes 3 in a row (decision, query understanding, writer); a room turn adds the designer. `LLM__TIMEOUT_S=60` with `MAX_RETRIES=2` means one slow call can take ~3 minutes. Any load balancer or proxy with the common 60 s limit returns a 504; the frontend `fetch` has no timeout, so the user just waits. | Error or endless spinner in production | G4, revised order step 2 |
| R2 | **502s from AI output rules.** Decision-model rule violations, rejected follow-up changes and invalid room measurements all reach the customer as errors. | Error screen, frequent | Issue 6 |
| R3 | **Every store except 50 is effectively empty.** Only 1,036 rows (all store 50) have commerce fields; the other ~76,000 are NULL. Switching to another store makes capabilities empty and every search return nothing. `price_unit` outside store 50 also contains non-currencies (Arabic nouns, numbers, "test"). | The multi-store promise, silently | G5 (data) |
| R4 | **Wrong product selected or refused.** The decision model has no category list, so the "sofa 5" kind check switches off for multi-word types, and "sofa" wrongly refuses a sectional sofa. | Wrong item recorded, or a pointless question | G3 |
| R5 | **Wrong price stated as fact.** The numeric guard only checks that a figure appears *somewhere* on screen, not on the card being talked about; the prompts also contradict each other on whether prices are visible. | "The second one is 2,450" when that is the fourth card's price | Issue 10 / G3 |
| R6 | **No rate limiting and no authentication.** Any client can send unlimited messages for any `store_id`; each message costs 3-4 reasoning calls. | OpenAI bill, capacity under abuse | New - needed before public launch |
| R7 | **Product ids change when Django edits a product.** Editing a product's category or image deletes and re-creates the row (`core/views.py` ~line 3740). Selections and room lines pointing at the old id silently disappear; the commerce fields are lost unless the pending Django fix shipped. | Customer's picks vanish mid-conversation | Data / Django |
| R8 | **Expired sessions confuse the screen.** Sessions live 1 hour in Redis (`ttl_s=3600`). After that the backend starts fresh while the UI still shows old cards, so "the second one" resolves against nothing. If the "send expected revision" setting is on, every message returns 409 until the chat is reset, because the frontend never updates the revision after an error. | Stuck or confused conversation | New |
| R9 | **Double submissions.** Two requests on the same session (a chip tap plus Enter, or F1's extra buttons) make one fail with 409 "This conversation moved on". Controls are disabled while sending, which covers most, but not all, paths. | Occasional error | New - check with F1 |
| R10 | **Plain searches show the oldest 5 products.** Not a crash, but every first impression is effectively random. | Relevance | G1 |

**Suggested order:** R2 and R1 first (they break most often), R6 before any
public launch, R3 before onboarding a second store, then R4 and R5 (wrong
facts).

---

## 1. The assistant behaves like a search box, not a smart agent

**Status:** Open - fix direction agreed (see "Agreed direction")

**Reported:** The application does not act smart. The LLM has no real
influence on which products are shown; the application just runs the search.

**Cause:** Each turn is a one-way pipeline:
understand → search once → describe the result. The AI is used only at the
start (reading the message) and at the end (writing the reply). It never sees
the search result, so it never gets a second move to recover, suggest an
alternative or combine products. CLAUDE.md rules also forbid it from
interpreting colours or styles outside the approved list, and from switching
product type after an empty result.

**Proposed fix:** Redesign as one agent with a toolbox of safe, store-locked
tools (search, catalog overview, combinations, designer, preferences) and a
capped loop of about 4 steps: act → look at the result → decide the next move.
Rewrite the CLAUDE.md rules to allow alternatives and combinations, provided
every product shown comes from the database and every change is explained to
the customer.

Target design details:
- **Toolbox:**
  - `search_products` (hard limits plus soft wishes);
  - `catalog_overview` (store's types, counts, colours, seat and price
    ranges);
  - `get_product` / `compare_products`;
  - `build_combination`;
  - `ask_designer`;
  - `plan_room`;
  - `update_preferences`.
- **Customer profile** in the session: budget (and how firm), household size,
  liked/disliked colours, styles and materials, room and size, chosen and
  rejected items. Read every turn; the customer can clear it.
- **Final check** before every reply: every product, price and number shown
  must come from a tool result this turn.
- **Safety kept:** the store is locked by code, no database access for the AI,
  no invented products or prices.
- **Step cap** of about 4 tool calls per message; simple requests finish in
  one.
- **Traces** of every step (thought → tool → result) for debugging.
- **Short instructions** (1-2 pages) about behaviour; past incidents go into
  the test suite, not the prompt.

---

## 2. Follow-up colour requests fail ("dark gray shades of sofa")

**Status:** Done (2026-09-25).
- The decision model and query understanding both pick from the approved
  colour/style lists (a constrained schema), one-to-many.
- Matching colours come first in ranking.
- There are no errors on follow-ups.
- Live: "dark grey shades" → Charcoal/Grey first. See issue 6, stages A-D.

**Reported:** After seeing sofas, the user says "I want some dark gray shades
of sofa". The assistant does not understand it, finds nothing, and shows an
error.

**Cause:**
- The approved colour list has Light Grey, Grey and Charcoal but no "Dark
  Grey".
- Follow-ups are handled by the decision model, which is not given the colour
  list.
- CLAUDE.md 12.4 forbids mapping a word onto the nearest approved colour.
- If the decision model writes a colour not on the list, the code rejects it
  and the customer gets a 502. If it treats the colour as a soft preference
  instead, the results barely change.

The store has 16 Grey and 2 Charcoal sofas that should have been shown.

**Proposed fix:**
- Let the AI map everyday colour words onto approved values, one-to-many
  (dark gray → Grey, Charcoal), with the code still checking each value is
  approved.
- Show matching products first, then the closest others, so the screen is
  never empty.
- Never return an error for an unmapped word; reply conversationally instead.

---

## 3. Customer preferences are not carried across follow-up queries

**Status:** Open

**Reported:** Preferences the customer states are not remembered in follow-up
queries.

**Cause:** Stated likings are tied to the current search only. The rules treat
"show me Modern sofas" as that one task's criteria rather than a lasting
preference, so moving to another product (for example sofas → rugs) drops them.
There is no structured customer profile that is read and updated every turn.

**Proposed fix:** Keep a structured customer profile in the session (budget,
household size, liked and disliked colours/styles/materials, room, chosen and
rejected items). Read it every turn, update it when the customer states
something, and apply it to related searches. The customer can clear it
("forget that").

---

## 4. "Sofa for 8 or 9 people" returns "nothing in the catalog"

**Status:** Done on branch `feat/agent-catalog-awareness` (2026-09-26),
reviewing and reworking a teammate's first version.
- One-seat part (2026-09-25): chair types, stool and single-seater sofa seat
  one and are never hidden by a seat filter.
- Seating combinations: every real combination is worked out - two or three
  sofas, or sofas with armchairs - holding every piece to the customer's
  colour, style, wishes and sizes, within budget. "Beige, 8 people, under
  5000" went from one sofa bed + 5 chairs to a beige 6-seat set + loveseat
  (3,440), two 3-seaters + a loveseat (3,490), or the set + 2 sofa chairs.
- The shape is asked first (with real "from" prices, colour if unknown, never
  budget), once; the answer, "either", or ignoring it all work; "I'll take the
  second option" saves that combination to their picks.
- The reply never claims a colour, style or size a combination did not meet.
- "Show more options" with combinations on screen showed the same three again
  and called them new; now it shows the next best, never repeats, and says
  honestly when a shape has run out (offering the other one). "I don't like
  the second option" replaces just that one. Live: paging and not-this-one
  6/6, 0 repeated combinations across 6 "show more" turns.
- Only one product per kind of piece was ever used, so "9 people, dark,
  under 5000" showed 2 combinations and then "no more" although 6 exist (12
  dark 3-seaters). Now each layout comes in several real versions, paging
  looks deeper each time, and all 6 are reached before "no more". Switching
  shape ("sofas only", "armchairs instead") now works first time at any
  point (it needed a second model call before).
- Open: "only dark shades, remove option 3" in one message is still refused
  (turning a combination down is accepted only on its own) - left as is by
  the user's choice (2026-09-26).
- Checked: unit suite 4430; each rule deliberately broken and caught; live
  seating cases 27/27 (9 cases x 3) plus paging 6/6.
- Done 2026-09-26 (user decisions): extra seats stay chair, lounge-chair and
  single-seater sofa; recliner and chaise lounge stay out; a sofa bed is a
  main piece only when asked for; "a sofa for 6" is shown the sofa set that
  seats them, as the best fit; over budget, the reply offers the closest real
  total ("about 3,700 - shall I show it?") and a yes shows the combinations.
  Live 12/12 on repeat.
- Still open:
  - swapping one piece of a chosen combination (needs combinations and room
    packages unified - agent-loop step);
  - "Boucle Fabric Swivel Makeup Chair" is classified `chair`, so it can be
    an extra seat when no colour is asked - a Django data fix;
  - "armchairs" is often read as `lounge-chair` (1 product) instead of
    `chair`.

**Reported:** Asking for a sofa for eight or nine people returns "I don't have
anything like this". It should suggest sofa sets, or buying two sofas
together.

**Cause:**
- In store 50 the largest sofa seats 5, sofa sets go up to 7, and
  sectional sofas up to 4.
- The seat count is treated as a firm requirement because it was not softened.
- Firm requirements are never loosened, the product type is never switched,
  and an empty search simply returns nothing.
- Nothing looks at the empty result and tries another approach.

The designer's knowledge ("a larger piece with extra seating beside it") and
the room optimiser, which can combine products, exist but are only used for
whole-room planning.

**Proposed fix - seating bundles:** Offering a combination does not break the
"category is fixed" rule. Sofas, sofa sets, sectionals and chairs are all in
the `seating` category. The customer's seat count is met exactly, not
relaxed; it is just spread over several pieces.

| Step | Owner |
|---|---|
| Detect that no single product meets the seat count (compare the request with the largest seat count per stocked type) | Code |
| Propose 2-3 combinations of *types and seat counts*, not products (e.g. sofa set 7 + 1-2 chairs; sofa 5 + sofa 4) | Interior Design Agent, new "seating solution" task |
| Validate each plan: approved seating types, stocked by the store, seats add up to the target (the sum is computed by code, never trusted from the AI) | Code |
| Find real products for each piece and pick a matching, in-budget combination with real prices and a real total | Code: existing search pipeline + bundle optimiser |
| Present 2-3 bundle options; the customer picks one, then can swap pieces with the existing room controls | Writer + existing bundle cards |

**Decision (confirmed by the user, 2026-09-24):** every chair type and the
single-seater sofa seat exactly one person:

- `chair`
- `dining-chair`
- `lounge-chair`
- `office-chair`
- `outdoor-chair`
- `stool`
- `single-seater-sofa`

In store 50, all 96 single-seater sofas and all chairs have an empty
`seating_capacity`. The implied capacity must be recorded as reviewed domain
data in the taxonomy registry, not guessed in code or by the AI.

Not included, pending confirmation: `recliner` (some recliners seat 2-3).

**Rule change needed in CLAUDE.md:** allow offering same-category combinations
that together meet a seating requirement exactly, with every product from the
catalog, the seat total computed by code, and the reply saying it is a
combination.

**Longer term:** in the agent redesign this becomes the `build_combination`
tool.

---

## 5. "Cozy lighter shade sofa" becomes a hard database filter

**Status:** Done (2026-09-25).
- Wishes are preferences by default, and only "only"/"must" filters.
- A strict colour nothing matches is lifted as a disclosed last resort.
- The writer is told how many cards match the wish.
- Live: "something cozy in a lighter shade" shows lighter sofas first,
  unfiltered. Rules recorded in CLAUDE.md 12.4 and 13.5.

**Reported:** Descriptive wishes such as "cozy lighter shade sofa" should
reorder results through Pinecone, not become a hard PostgreSQL filter.

**Cause:**
- As a *new* search this works as designed: the Translator (query
  understanding) treats a wanted colour as a preference, and "cozy" goes to
  Pinecone.
- As a *follow-up*, the decision model handles it. Its instructions never
  explain preference vs requirement, give only the strict example "only
  beige", and include no colour list.
- So it often marks the colour as a requirement. It then either picks an
  approved value like "Light Grey", which becomes a PostgreSQL filter, or
  writes a value not on the list, which gives a 502.

Pinecone text ranking is confirmed working (see "Plan review"), so once the
colour stays a preference, "cozy lighter shade" is ranked correctly.

**Proposed fix:**
- Follow-up colours and styles become preferences by default. They become
  filters only with explicit words such as "only" or "must".
- An unapproved value becomes a preference instead of an error.
- Longer term, route follow-ups through query understanding, which already has
  the right rules and the colour list.

---

## 6. Frequent 502 errors

**Status:** Done (2026-09-25). Unusable model output no longer reaches the
customer as an error. Verified by reproducing all 25 original error cases, by
the independent verifier, and in live runs (0 error responses from our own
logic). The two remaining error sources are not model-output errors and are
tracked separately:
- outages (e.g. a staging-database timeout) still show an error card, under
  "friendly error for outages";
- timeouts, under R1.

History - step 1 (safety net and rule logging) is done and was
verified on 2026-09-24:

- Unusable model output now ends as a friendly, fixed "didn't catch that"
  reply. No model call is made, the customer's state is unchanged, and the
  conversation continues. This covers typed turns and button actions.
- The log records which rule failed (`llm_response_schema_violation`,
  `customer_turn_not_understood`), without customer text.
- Also fixed: the 500s from unreadable figures ("NaN", "sNaN", "1e999999",
  "1e-999999", zero or negative room sizes, inverted budgets). One shared
  reader, `app/core/numbers.py`, now handles every figure taken from model
  output.
- **Stage A done:** format clean-up.
  - Colour and style names are matched regardless of case, spacing, "_"
    and "-" (`CatalogAttributes.canonical`).
  - Money accepts "5,000", "5k" and "12.5K".
  - Percentages accept "20%".
  - Ambiguous figures are refused rather than guessed: "2,5", "0,500",
    "1,500 m" (as a length), and "5.000" as a price.
- **Stage B done:** the decision model gets the approved colour/style lists
  and its response schema only admits those values (`build_constrained_decision`,
  converted back with `to_plain_decision`).
  - It maps words to a *set* of values ("dark grey" → Grey, Charcoal).
  - A wish is a preference by default; only "only"/"must" makes a filter.
  - Query understanding follows the same rules.
  - A strict colour or style nothing in the list expresses now asks the
    customer (unresolved strict requirement).
- **Stage C done:** corrective retries before any fallback.
  - A rejected decision is re-asked once, with our own rule text.
  - A valid but unappliable decision is re-decided once, and the turn re-runs
    from the untouched state.
  - Query understanding is re-asked once, including a mismatched
    category/subcategory pair, which was previously a 422.
  - The model may ask the customer one question when genuinely unclear.
  - Limits and safeguards: at most 3 decision calls per turn; nothing is
    applied twice; screen actions and outages are never retried.
  - "Didn't catch that" is the last resort, and always leaves the state
    unchanged.
- Verified by an independent agent after every stage (real route, graph,
  runtime and services; only the providers faked).
- **Stage D done:**
  - **Strict colour/style with zero matches:** after every permitted widening,
    the colour/style filter is lifted as a last resort.
    - Every other constraint is kept (type, budget, size).
    - The requested colours become ranking preferences, so Pinecone puts the
      closest first.
    - It is recorded as a `color`/`style` relaxation, and the writer says
      plainly that none matched.
    - A strict requirement with even one match is never lifted.
  - **Preferences rank matching products first:** stored colour/style match →
    Pinecone similarity. This also applies with an explicit sort ("cheapest
    beige" = beige ones by price first, then the rest).
- **Verified:**
  - All 25 original error cases (A1-A12, B1-B4, C1-C2, plus the 500s and the
    QU 422) were reproduced on the original code and return a normal reply
    on the current code.
  - Live OpenAI run, 21 conversations: before 16/21 passed with 2 error
    responses; after 20/21 with 0 errors. The one failure is issue 7, which
    is not fixed yet.
  - Reports: `evals/conversations/last_report.txt`.
- **Follow-up fixes (2026-09-25):**
  - **Writer:** positions in the customer's current message refer to the
    previous screen. This fixed a live false statement ("none is 20%
    cheaper"); now 4/4 correct.
  - **A strict colour/style no approved value expresses** ("only red") no
    longer asks a question: it runs the stage-D last resort
    (`ResolvedSearch.unmatched_strict`, `_search_unmatchable`). Live: "only
    red" 4/4, "only purple" 4/4.
  - **Mixed strict requests:**
    - "red or beige": beige filters, red is a preference, and no false "no
      match";
    - "Modern and cottagecore": Modern filters, and cottagecore is reported
      as unmatched, because styles are all-of.
  - **A criteria change starts fresh results** (exclusions reset), so "show
    more" → "under 3000" no longer returns nothing.
    - Trade-off: a card the customer turned down ("not this one") can
      reappear after a later change.
    - Keeping explicit rejections separately is possible later.
  - **Known limit:** the "no exact match" disclosure appears on the first
    page only, not on "show more" pages after it.
  - **Writer honesty for wished colours:** the writer now gets
    `wished_colour_matches` / `wished_style_matches`, counts of cards on
    screen in a colour or style the customer asked or wished for.
    - Before: "make them red ones instead" beside black, gold and white tables
      was described as red being "the deciding factor".
    - Now: "I don't have any red coffee tables to show you here..." (3/3
      live).
  - **Live check (2026-09-25), 28 cases:**
    - 56/56 on the run before the writer fix;
    - 26/28 on the regression run after it. The 2 failures were a transient
      staging-database timeout (503), and both passed on re-run.
  - Reminder from that run: a database blip still shows the customer an error
    card, which is the "friendly error for outages" item below.
  - **Saved-session check (2026-09-25, live, reading Redis after every turn):**
    - A refined search keeps budget + strictness, seats, width + "approximate",
      colour/style wishes and descriptive words across price changes, a
      sort, and "show more".
    - A changed request resets paging exclusions.
    - A room request saves the budget, household size, room type, style and
      measurements ("4 by 5 metres" → 400 × 500 cm).
    - A selection is saved and survives new searches.
    - "I usually like warm beige" is saved to the profile (Beige, Sand, Taupe)
      and seeds later searches, e.g. rugs.
    - A strict colour stays with its own search, and is not forced onto a new
      product type.
    - **Gap found and fixed:** a measurement dropped on a type change (e.g.
      sofa width → sofa sets, which have no single footprint) was never
      mentioned. The writer received `dropped_roles` but its prompt never used
      it. Now disclosed.
    - Still open: wishes stated only inside a search ("beige sofas") do not
      carry to a new product type - issue 3.
  - **Dropped-measurement wording** (user disagreed with the first version):
    no longer blames the catalogue; it offers help instead ("these come in
    quite different shapes, so I've shown a range rather than holding to one
    width - tell me the space you have..."). Used for any type a measurement
    cannot be applied to (sofa sets, sectionals, beds, chairs). Live 3/3.
  - **Dimension changes parked (2026-09-25, user decision).** A "longer side /
    shorter side" rule was trialled for sectionals and evaluated for all types,
    then undone: size filtering stays exactly as committed. Findings kept for
    later: most furniture stores the longer side consistently; 8 of 15
    sectionals, 5 beds and 6 mattresses are swapped; chairs are too near-square
    to tell width from depth; data to review on the Django side - sectional
    173472 height 159, beds 172972 and 172974, missing bed/mattress heights.
  - **Sizes belong to a product type (2026-09-25, done).** A product size used
    to follow the customer to any type in the same family that could be
    measured the same way: a side-table 60 cm limit carried to coffee tables
    left 2 of 41, and a coffee-table 100 cm limit left 0 of 16 TV stands.
    - Now each type keeps its own sizes in the session; they come back (and the
      reply says so) when the customer returns to that type, and "any size"
      drops them. Room measurements are separate and always carry.
    - Checked: unit suite 4277/4277; an independent verification agent drove
      the real coordinator (all behaviours pass after two fixes it found - a
      system search erasing a saved size, and a restated size reported as
      dropped); live 15/15 (side table -> coffee tables not limited 3/3, sofa
      size comes back and is mentioned 3/3, "any size" clears 3/3, size kept on
      a price change 3/3, sofa -> sofa sets disclosed 3/3).
    - Session format stays `agent_state_v5`: the new field has an empty
      default, so live sessions still load. Rolling back to older code would
      refuse sessions that hold the new field.
    - Open, found during the check:
      - **Existing crash (not caused by this change):** a type change that
        states a size the new type cannot take - "make them sofa beds under
        100 cm wide" - sends it to discovery, which raises
        `UnsupportedDimensionRoleError`, and the turn fails with an error.
      - Minor: after a similar-product search, restating one size replaces the
        whole saved entry, so another saved size (e.g. a depth) is forgotten.
  - **Found in the carry check (2026-09-25):**
    - Fixed: "make them single seater sofas" searched for a one-seat sofa and
      found nothing (see issue 4), and a search that found nothing ended the
      conversation (see issue 9). An independent verification agent checked
      both against the real turn engine; one defect it found (a saved one-seat
      sofa search could not be refined) is fixed.
    - Open: "armchairs" is sometimes read as `lounge-chair` (1 product in
      store 50) instead of `chair`, which CLAUDE.md 7 says covers armchairs -
      2 of 3 live runs showed a single card.
    - Minor: the seating registry accepts any approved subcategory, not only
      seating ones.
  - **Unit tests added (2026-09-26)** for the per-type sizes, the seating
    registry and the zero-results next steps: 60 tests in
    `tests/unit/test_sizes_per_product_type.py`, `test_seating_rules.py` and
    `test_zero_result_next_steps.py`, running without the model or database.
    Each fix was then deliberately broken one at a time (15 breaks) and every
    break was caught. Full suite 4337 passed; live suite 38/38; the 16-turn
    saved-session check passes on the final code.
    - Noted while testing: a colour or style set aside never appears among the
      next-step counts in practice - when only the colour stood in the way,
      the colour last resort has already shown those products.
- Still to do:
  - A friendly frontend error card, plus timeouts and a retry cost budget.
    Worst case today: 3 decision + 4 query-understanding calls before a
    fallback.
- Open decisions for the user:
  - ~~update CLAUDE.md 12.4 (colour mapping) to match~~ - done 2026-09-25:
    12.4, 13.4, 13.5, 16.1, 17.1 and the new 21.1 updated;
  - re-label golden eval sections M and N;
  - bump the prompt version names (kept as v1 to avoid breaking a pinned
    test).

**Reported:** Many 502 errors appear in the application.

**Cause:** A 502 is `LLMResponseInvalidError`: the AI's answer broke a code
rule, and the code treated that as a server failure. Sources, most frequent
first:

1. **Decision model output breaks one of ~60 cross-field rules** in
   `CustomerAgentDecision` and its nested schemas. OpenAI enforces the JSON
   shape but not these rules. There is no retry and no fallback
   (`app/services/customer_decision.py`, `app/integrations/llm.py`).
2. **A follow-up change is rejected by the composer**: an unapproved colour or
   style, a malformed amount, or an unresolved relative price
   (`app/services/refinement_composer.py`, re-raised at
   `app/services/turn_coordinator.py:2995`).
3. **Invalid room measurements** from the decision model
   (`app/services/proposal_mapping.py`).

The log line `llm_response_schema_violation` does not record *which* rule
failed, so the real causes cannot be seen. Query understanding and designer
failures are already caught and turned into friendly replies; these three are
not.

**Proposed fix:**
1. Log which rule failed (field location and our own message, never the
   customer's input).
2. Retry the decision call once, telling the model which rule it broke.
3. If it still fails, give a friendly reply instead of a 502.
4. Turn harmless violations (such as an unapproved colour) into preferences
   rather than errors.
5. Longer term, reduce the decision model's responsibilities so there are fewer
   rules to break.

**Recovery ladder (agreed direction, 2026-09-24):** A 502 must never reach
the customer. Every AI-output problem goes down this ladder, stopping at the
first step that works:

| Step | What happens | Example |
|---|---|---|
| 1. **Repair** | Code fixes what it safely can without guessing: case and spacing ("beige" → "Beige"), "5k" → 5000 | "only beige ones" |
| 2. **Re-ask the AI once** | Send back *which rule* was broken, plus the approved values for that field; the AI answers again | "dark gray" → AI maps it to Grey + Charcoal from the list it is now given |
| 3. **Soften** | A value that still is not approved becomes a preference (ranking only) instead of a filter | "only neon ones" → preference, results still shown |
| 4. **Ask the customer** | If the meaning genuinely cannot be settled, ask one clear question with quick-reply chips built from real options | "By darker, do you mean Grey or Charcoal?" [Grey] [Charcoal] [Show both] |
| 5. **Friendly fallback** | Only if all else fails: a warm reply with a next step (issue 9), never an error screen | "Let me try that another way - want to see all grey sofas?" |

Only real outages (database, OpenAI or Pinecone down) remain errors, returned
as 503 with a friendly message and a retry option.

**Per source:**
- *Decision model rule violations:* steps 2 → 5, logging which rule failed.
- *Unapproved colour or style in a follow-up:* steps 1 → 2 → 3 → 4.
- *Malformed amount / unresolved relative price:* steps 1 → 4, e.g. "Cheaper
  than which one - the first or the second?"
- *Invalid room measurements:* step 4, e.g. "Is that 4 by 5 metres?"

**Measure it:** count each ladder step in the logs. If step 4 fires often for
the same kind of message, that is a prompt or vocabulary gap to fix, not a
customer problem.

### Every 502, with cause and reproduction (verified 2026-09-24)

A 502 happens only when `LLMResponseInvalidError` escapes the turn. It was
reproduced through the real `/v1/chat` endpoint, graph, runtime, coordinator,
composer and proposal mapping, with only the AI provider, catalog and Redis
faked. Each case is turn 1 "show me sofas" (200), then the follow-up below.

**Group A - the decision model's answer is rejected (most common).** The
decision call has no retry and no catch, so any rejected answer, or a refusal
with no structured output, becomes a 502 ("We could not interpret that
request. Please rephrase it.").

| # | Customer says (likely trigger) | What the model produced | Rule broken |
|---|---|---|---|
| A1 | "show more options" / "show me more" | refinement with no change | "a refinement delta that changes nothing is not one" |
| A2 | "20% cheaper than the second one" | percent written as `"20%"` | "a percentage must be a decimal figure" |
| A3 | "something cheaper than the second one" | relative price plus a strength | "a relative price operation must not carry max_strength" |
| A4 | "compare the second one" | only one reference | "a comparison needs at least 2 references" |
| A5 | any turn | no follow-up allowed, but a goal named | "a turn offering no follow-up has nothing to ask about" |
| A6 | "we're 40 people, it's for a majlis" | household of 40 | seating count must be 30 or less |
| A7 | a long multi-turn request | restated request over 300 chars | "String should have at most 300 characters" |
| A8 | "I like the first, make them sectionals" | design anchor on a refinement | "only a design handoff carries a design anchor" |
| A9 | ambiguous choice ("I'll take that one") | a clarifying question that also selects | "a clarification turn changes nothing" |
| A10 | "remove this one" in a room | replacement with no card named | "replace_product names the piece it changes" |
| A11 | "show me tables, I like this one" | focus on a product plus a new search | "focus cannot accompany a search that replaces results" |
| A12 | unusual or injection-like messages | refusal, no structured output | "no structured output returned" |

Each rule was confirmed to reject the shape shown. Which exact wording makes
the live model produce each shape is a likely trigger, not a guarantee;
logging which rule failed (fix step 1) will show the real frequencies.

**Group B - a follow-up change is rejected by the composer.** Confirmed 502.

| # | Customer says | What the model produced | Why it fails |
|---|---|---|---|
| B1 | "only beige ones" | colour `beige` (lowercase), strict | exact, case-sensitive match against `Beige` |
| B2 | "only dark grey ones" | colour `Dark Grey`, strict | not in the approved list |
| B3 | "under 5,000 SAR" | amount `"5,000"` | a comma breaks the number parser |
| B4 | "under 5k" | amount `"5k"` | shorthand not parsed |

Correct forms (`Beige`, `5000`) pass. Code: `refinement_composer.py`
`_approved` / `_decimal`, re-raised at `turn_coordinator.py:2995`.

**Group C - customer facts from the decision are rejected.** Confirmed 502.

| # | Customer says | What the model produced | Why it fails |
|---|---|---|---|
| C1 | "my budget is 12k" or "12,000 SAR" for a room | budget `"12k"` / `"12,000"` | not a plain decimal (`proposal_mapping.py` `_amount`) |
| C2 | "the room is 4 m long... actually 5 m" | room length stated twice | "a room has one of each" (`proposal_mapping.py` geometry) |

**Group D - unreachable today.** `MALFORMED_PERCENT` (`turn_coordinator.py`
`_relative_price_outcome`) and `RELATIVE_PRICE_NOT_RESOLVED` cannot fire: the
first is caught earlier as A2, the second is resolved before composition.

**Not 502 (already handled):**
- query understanding failures → friendly "search unavailable" (confirmed:
  200);
- designer failures → friendly design message;
- writer failures → fixed fallback text;
- database, Pinecone or OpenAI outages → 503.

Side note: `/v1/chat` accepts messages up to 2,000 characters, but query
understanding refuses anything over 1,000. A 1,001-2,000-character search
therefore gets a misleading "search unavailable" rather than a 502.

**Mapping to the recovery ladder:**

| Cases | Ladder step |
|---|---|
| B1, B3, B4, C1 | Step 1, repair (case-insensitive match; strip commas; parse "k") |
| Group A | Step 2, retry with the broken rule, then step 5, friendly fallback |
| B2 | Step 2, re-ask with the colour list, then step 3/4 |
| A1 | Issue 7, a "show more" action |
| A6 | Raise or remove the 30 limit - **done: one shared limit of 200 (`MAX_REGULAR_SEATING_COUNT`)** |
| C2 | Ask the customer which figure is right, or keep the latest |

### Agreed fix plan: the customer never sees an error (2026-09-24)

**Principle (user decision):** no customer ever sees an error screen. The
AI's reasoning resolves ambiguities; when the customer's intent genuinely
cannot be settled, they get one clear question with tap-to-answer chips.

**Layers, in order:**

1. **Format clean-up in code** (not ambiguity, just formatting):
   - case-insensitive colour/style match (`beige` → `Beige`);
   - strip commas ("5,000");
   - read "k" ("5k", "12k");
   - strip "%" ("20%").

   Instant, and saves an AI call.
2. **AI retry with reasoning.** When the AI's answer breaks a rule, send it
   back once with:
   - which rule was broken (our own rule text);
   - the approved values for that field (colour list, taxonomy);
   - the cards on screen.

   The AI reasons about what the customer meant and answers again. This covers
   group A (A3, A5, A7, A8, A11, A12) and B2 ("dark grey" → Grey, Charcoal).
3. **Ask the customer** only when *their intent* is unclear, never about our
   own mistakes. One question, with chips built from real options:

   | Case | Question |
   |---|---|
   | A4 | "Compare it with which one?" |
   | A9 | "Which one did you mean?" |
   | A10 | "Which piece?" |
   | C2 | "Is it 4 m or 5 m?" (if not obvious; otherwise keep the latest) |
   | B2 | "Grey or Charcoal?" (if still unclear) |
4. **If the retry still fails,** drop the part that cannot be understood, do
   the main action, and say what was not done, with a next step.
5. **Safety net:** anything still unresolved becomes a warm reply with a next
   step. The session is left unchanged, so the customer can simply carry on.

**Structural fixes that remove cases entirely:**
- A1 "show more options" → a proper show-more action (issue 7);
- A6 → raise the 30-person household limit (e.g. majlis, events).

**Beyond 502 - every other error also reaches the customer as conversation:**

| Situation | Today | After |
|---|---|---|
| Outage (database, OpenAI, Pinecone) | 503 error card | Friendly message plus a "Try again" button |
| Reply took too long | Endless spinner or gateway error | Time limit plus a friendly message (revised order step 2) |
| Conversation conflict / expired session | 409 error card | Silently start from the latest state, or "Let's pick up from here" |
| Unexpected bug | 500 error card | Friendly message; details logged with a trace id |

**Frontend:** `ErrorCard.tsx` currently shows the HTTP status, error code and
trace id to the customer ("HTTP 502 · llm_response_invalid"). Replace it with
an assistant-style message and a "Try again" button. Keep the trace id hidden
(available to support only).

**Monitoring:** log every ladder step:
- which rule failed;
- whether the retry fixed it;
- whether a question was asked;
- whether the safety net fired.

A rule that fails often is a prompt or schema problem to fix at the source.

**Done when:** every case A1-A12, B1-B4 and C1-C2 has a conversation test that
returns a normal reply (answer, results or one question), and no test can
produce an error status for a customer message.

---

## 7. Typing "show more options" shows the same products

**Status:** Fixed (2026-09-24).
- The decision model has `show_more` and `exclude_reference` on a search.
- Both run the buttons' own code path (`_rerun_excluding`), so typing and
  tapping give identical products (verified).
- "I don't like the second one, show me others" may set both.
- With no search on screen, it asks instead of failing.
- Live run: typed "show more options" now shows a new page; before it
  repeated the same 5 sofas.

**Reported:** Clicking the "show more options" button shows different
products, but typing "show more options" does not.

**Cause:**
- The button sends a fixed `more_options` action. It skips the AI and re-runs
  the current search, excluding the products on screen
  (`_apply_search_action` in `app/services/turn_coordinator.py`).
- A typed message goes to the decision model, which has no way to say
  "exclude what is shown": no such action or refinement field exists, and the
  prompt never mentions it.
- The prompt also says that for "just show me options", *"the options they
  asked for are the ones already there, and nothing needs running again."*

The model's possible choices therefore all fail:

| Choice | Result |
|---|---|
| Answer | Same cards stay on screen |
| New search | Same query in fixed id order, so the same 5 products |
| New search from the literal words | No product type found, so a clarification question |
| Refine search | No change to make, so a validation failure and a 502 |

The same gap exists for typed "not this one", which the frontend's exclude
button handles.

**Proposed fix:**
- Give the decision model a "show more" option, and a "not this one" option
  pointing at a card.
- Route both to the same code the buttons use.
- Remove the misleading prompt instruction.

---

## 8. The assistant does not ask how many people will sit, and does not use the answer for sofa searches

**Status:** Open

**Reported:** When designing a living room, and on a plain sofa search, the
assistant does not ask how many people will be seated. It should, so the
answer can be used.

**Cause:**
- **Asking:** the decision prompt discourages questions ("When in doubt, do
  not ask"). For a whole room, the household size is only asked if the budget
  is already known, as the second of at most two opening questions. For a plain
  sofa search, `seating_requirement` is an optional follow-up the prompt steers
  away from.
- **Using:** when the customer does state it ("family of five"), it is stored
  only as `room_project.regular_seating_count`. That field is read only by the
  Interior Design Agent for room plans, complements and advice. The single
  product search never reads it: `refinement_composer.py` does not use it, so
  a later "show me sofas" is not sized for that household.

**Proposed fix:**
1. **Sofa and seating searches:** show results first, then ask "how many people
   usually need a seat?" as the one follow-up question, unless it is already
   known. This answer changes what is shown, so it earns the question.
2. **Living room design:** include household size in the opening questions
   together with the budget (at most two, asked once), instead of only when the
   budget is already known.
3. **Store it once, use it everywhere:** keep household size as a
   session-level customer fact rather than only on the room project, and apply
   it to seating searches as a soft preference (rank matching seat counts
   first), not a hard filter.
4. **Link to issue 4:** when the household is larger than any single piece
   seats, go straight to the seating-bundle options.

---

## 9. Replies sometimes dead-end the conversation

**Status:** Partly done (2026-09-25).
- Done: zero results. When nothing matches even after the allowed widening,
  code counts what setting each requirement aside would find (and, for a
  budget, where prices actually start), and the reply offers it as a yes/no
  next step - "Sofas here start at 990 SAR - shall I show you the most
  affordable ones?", "without the width there are 19 - want to see those?".
  The fixed zero-results fallback also names a next step now. Live 6/6.
- Still open: the other fixed fallback sentences, the prompt rebalance ("stop
  there" -> "always leave a next step") and quick-reply chips.

**Reported:** Sometimes the sales agent writes a message that stops the
conversation and leaves the user at a dead end, so they have to start
searching again themselves. This should never happen.

**Cause:** Three sources.

1. **The prompts push the agent to stop.** The decision prompt says "Deliver
   the useful thing and, most of the time, stop there", "When in doubt, do not
   ask" and "Most searches should come back with ... no question at all." The
   response prompt says "When the summary names no subject, ask nothing" and
   "say what you would do, and stop." These rules were written to stop the
   agent badgering. They over-corrected: *not asking a question* turned into
   *not offering any next step*.
2. **Fixed fallback sentences have no way forward**
   (`app/services/response_wording.py`), for example:
   - "I couldn't find anything matching that. It's worth trying a different
     description." (zero results)
   - "I wasn't able to put that together just now." (design handoff that
     produced nothing)
   - "I couldn't find another one of those to offer you, so I've left your
     current choice as it is."
   - "You haven't picked anything out yet, so there's nothing to show you
     here."
   - "I wasn't able to run that search just now. Please try again in a moment."
3. **Zero results are reported, never recovered from.** The response prompt
   says for zero results: "Say so plainly, and do not guess what the catalog
   holds." Nothing supplies alternatives (see issue 4), and a complement that
   finds nothing is silent.

**Proposed fix:**
1. **A "never a dead end" rule.** Every reply ends with a way forward: a
   suggestion, an offer, or quick-reply options. This is different from asking
   a question. "Here are the grey ones - I can also show sofa sets if you want
   more seats" guides without interrogating.
2. **Code supplies the next steps, the AI words them.** On zero results or a
   failure, code attaches concrete next moves built from facts:
   - related types this store stocks;
   - "raise the budget to X";
   - "show all sofas";
   - "remove the colour filter";
   - the seating bundles from issue 4.

   These are shown as quick-reply chips; the frontend already has
   `quickReplies.ts`.
3. **Rewrite the fixed fallbacks** so each one includes a next step, for
   example: "I couldn't find that exact match. Want to see the closest options,
   or try a different colour?"
4. **Rebalance the prompts:** keep "don't ask unnecessary questions", but
   replace "stop there" with "always leave them a next step".
5. **Add eval cases** that fail any reply with no next step, covering zero
   results, failures and empty complements.

---

## 10. Replies sound flat and AI-generated, not like a welcoming salesperson or designer

**Status:** Open

**Reported:** The writer should produce good sales and interior-designer
responses in a welcoming tone, instead of dead, AI-generated sounding
messages.

**Cause:**
1. **Many replies never reach the writer.** These branches use fixed sentences
   from `app/services/response_wording.py`, and they are the flattest replies
   in the app:
   - failures ("I wasn't able to run that search just now.");
   - room edits ("That piece will stay in the room.");
   - design handoffs that produced nothing;
   - fallbacks ("Here's what I found.").
2. **Clarification questions are shown verbatim from the decision model.** The
   decision model is a router, not a writer, and its question is passed
   through unedited.
3. **The writer's prompt is mostly prohibitions.** About 430 lines, dominated
   by "never", "do not" and "you are not given". The persona section asks for
   warmth, but the rest teaches caution. Models given that balance write
   defensive, hedged prose.
4. **The writer knows little about what the customer chose.** It is told how
   many products were picked and what kinds, never which ones or what they look
   like, so it cannot talk about them specifically.
5. **A failed figure check falls back to a fixed sentence.** When the numeric
   guard rejects a draft twice, the customer gets something like "Here's what I
   found."
6. **Same model settings for every job.** One reasoning-effort setting applies
   to routing, extraction, design and writing (`ZORY_LLM__REASONING_EFFORT`).
   The writer gets no settings of its own.

**Proposed fix:**
1. **Route every customer-facing reply through the writer.** Code decides
   *what happened* (failure, lock, clarification); the writer phrases it
   warmly with a next step. Keep the fixed sentences only as the last-resort
   fallback.
2. **The writer phrases clarification questions.** The decision model supplies
   the subject; the writer asks it naturally, with chip options.
3. **Rewrite the writer prompt voice-first.** A short voice guide (warm,
   knowledgeable, like a showroom designer), then 10-15 good vs bad example
   pairs covering search results, zero results, design advice, bundles,
   selections and follow-ups. Move the safety rules into a compact checklist
   at the end; the code guards (numeric guard, grounding) already enforce them.
4. **Give the writer design facts about chosen pieces** (kind, colour, style,
   size - no ids), so it can say "that walnut coffee table will warm up the
   grey sofa" instead of "your choice".
5. **Show a welcoming first message** for new sessions, with store-based
   category chips (F2).
6. **Give the writer its own model settings,** and consider a model chosen for
   natural writing.
7. **Add eval cases scored for tone**: warmth, specificity, a next step, no
   robotic phrasing ("Here are some options", "I've pulled together"). Use a
   rubric reviewed by a person, not only automatic checks.

**Related:** issue 9 (dead ends), issue 6 (fallbacks that replace errors must
also sound human).

---

## 11. The decision model is overloaded, and one slip fails the whole turn

**Status:** Open

**Reported:** Raised in the architecture review, and confirmed by the user as
a concern: the decider does too many jobs, which makes it hallucinate.

**Cause:**
- One structured call fills about 19 fields:
  - action;
  - product references;
  - refinement constraints (price, colour, size);
  - room edits;
  - design scope and revision;
  - customer facts;
  - purchase stage;
  - follow-up policy and goal;
  - commercial reason;
  - restated requests.
- Its prompt is about 600 lines, much of it patches for past incidents.
- OpenAI strict mode forces every field on every answer, so irrelevant fields
  get filled with junk. Cross-field validators then reject the answer (issue
  6).
- It is not given the category or colour lists it needs (G3, issues 2 and 5).

**Proposed fix - give each step one job:**

| Job today | New owner |
|---|---|
| Pick the action | Router (AI, small and fast) |
| Which product "the second one" means | Router points; code resolves (as today) |
| Restate a request spanning turns | Router |
| Price / colour / size changes | Query understanding, given the conversation (one extraction owner for new searches and follow-ups) |
| Room edits (keep / swap / remove) | Room-edit step (AI), only when needed |
| Customer facts (budget, household, likes) | Memory step (AI), run in parallel |
| Purchase stage | Code (count selections, comparisons) |
| Whether to ask a follow-up, and about what | Code (what is missing and not already asked) |
| Wording of any question | Writer |
| Commercial reason | Remove (only used for logging) |

The router's answer shrinks to about 4 fields: action, product pointer,
restated request, refinement yes/no.

**Techniques:**
- **One answer shape per action** (a discriminated union), so the model never
  fills fields that do not apply.
- **Give each step the lists it needs:**
  - the full taxonomy to any step that names a category;
  - the colour and style lists to any step that names one;
  - restricted schemas so it cannot write a value that does not exist.
- **Retry once with the broken rule, then fall back politely** (issue 6
  ladder).
- **Incidents go into the test suite, not the prompt.**
- **Right-size models:** fast models for router and memory; the stronger model
  for designer and writer.
- **Log each step separately.**

**Which AI step gets which knowledge:**

| Step | Full taxonomy | This store's categories |
|---|---|---|
| Query understanding | Yes (has it today) | **No, deliberately.** Otherwise it forces requests onto the nearest stocked type ("chandelier" → table lamp) |
| Designer | Yes | Yes, with counts and max seat counts |
| Router / decider | Yes (missing today, G3) | No |
| Writer | No | No; it sees the cards |

Code always decides validity and whether the store sells something.

**Order:**
1. Retry and fallback plus rule logging (issue 6).
2. Purchase stage and follow-up decision into code.
3. Refinements through query understanding (fixes 2, 5, 7).
4. Split out the memory step.
5. Shrink the router with one shape per action.

In the agent redesign (issue 1) these steps become tools.

---

## 12. "We don't sell that" is a dead end

**Status:** Open

**Reported:** Discussed with the user: when a customer asks for a product type
the store does not stock, the reply should say so honestly and offer an
alternative.

**Cause:** Query understanding correctly reads the request (e.g. `lighting` /
`chandelier`), but nothing checks it against the store's stock before
searching. The search simply returns nothing, and the customer hears "nothing
found", which reads like a failed search rather than "we don't carry that".
`CatalogCapabilityService` already knows what the store stocks but is not
used on this path.

**Proposed fix:**
- Before searching, code checks the resolved type against the store's
  capabilities.
- If it is not stocked, the reply says so plainly and offers the closest
  stocked alternatives as chips, chosen by the AI from the store's menu (F2)
  and validated by code: "We don't carry chandeliers, but here are our pendant
  lights."
- The alternative is always presented as an alternative, never as what they
  asked for.
- In the agent redesign, this is `catalog_overview` plus the loop.

---

# Planned Features

## F1. Select and Compare buttons on product cards

**Status:** Planned

**Requested:**
- A button on each product card to select it, so the selection is added to
  the chat.
- When two or more products are selected, a Compare button appears to compare
  them.

**Approach:** Follow the existing button pattern (`search_action`,
`bundle_action`). Button actions skip the decision model and run deterministic
code, so they are instant, cost no model call and cannot be mis-routed.

1. **Select / Deselect (toggle on each card).** The UI sends the card's
   position on screen, never a product id. The server resolves it against
   verified state and updates `selected_product_ids`. The reply is a short
   fixed confirmation with no model call, and the chat records it so later
   turns know about it.
2. **Compare (appears at 2+ selected).** The UI sends `compare_selected`. The
   server reads the selection from the session and runs the existing
   `ProductComparisonService`. The Writer may add a short comment on the table.

**Things to get right:**
- **Compare the selection, not screen positions.** Selections can span several
  searches, so earlier picks may no longer be on screen. The Compare action
  carries no positions; the server uses the stored selection.
- **The server is the source of truth for what is selected.** Each reply
  should tell the UI which cards are selected, and the UI renders the ticks
  from that. Otherwise a refresh or a typed "I'll take the second one" makes
  the screen and the assistant disagree.
- **Maximum 4 in a comparison** (the existing schema ceiling). With more than 4
  selected, disable Compare or let the customer choose which 4.
- **Typing and clicking must run the same code.** "Select the second one" and
  "compare the ones I picked" must route to the same handlers as the buttons,
  to avoid a repeat of issue 7.

**Changes:**

| Where | Change |
|---|---|
| Backend request (`app/schemas/`) | New button actions: `select` / `deselect` (by card position), `compare_selected` |
| Backend logic (`turn_coordinator.py`) | Route them to the existing selection update and comparison service; no new search or model logic |
| Backend reply | Include which presented cards are selected |
| Frontend | Select/Deselect toggle on `ProductCard`; a Compare button shown at 2+ selected |
| Decider | Ensure typed equivalents route to the same handlers |

## F2. Always show store-based category suggestions that follow the sale

**Status:** Planned

**Requested:** The agent should always show the categories the store offers,
and keep suggesting relevant ones based on the room type or wherever the
conversation is going. The list must come from what the store actually sells,
never be hardcoded.

**Current state:**
- `CatalogCapabilityService` already derives each store's categories,
  subcategories and active product counts from live catalog data. This is the
  right source.
- It is only used for whole-room planning and complements, never shown to the
  customer.
- It has no cache, so every call is a database query.
- `core_product.room_types` exists but is empty for all 1,036 store-50 rows,
  so the catalog cannot yet say which products belong in which room.

**Approach:**

| Part | Owner | Notes |
|---|---|---|
| The store's category menu | **Code** (`CatalogCapabilityService`) | Live, store-scoped, only types with active products; cached in Redis with a configurable TTL |
| Which 3-5 to suggest right now | **AI** (designer / agent) chooses **only from the store's menu**; **code** validates | Based on room type, what is selected or in the room, and where the conversation is going. Same reasoning the complement flow already uses |
| Fallback when no AI choice is available | **Code** | Store's types ordered by product count, minus what is already chosen |
| Shown on every reply as chips | **Frontend** | Also serves as the "next step" required by issue 9 |
| Clicking a chip | **Code**, button action | Deterministic search for that category, same handler as typing it (lesson from issue 7) |

**Rules:**
- Never suggest a type the store does not stock; code filters every suggestion
  against the store's menu.
- Leave out what the customer already chose, already owns, or turned down.
- Respect scope. If the customer says "only the sofa" or "just browsing",
  stop suggesting more pieces until they say otherwise (already a rule in the
  decision prompt).
- Show display names in customer words (`center-table` → "center table").
- No category list lives in code or prompts. Switching store changes the
  suggestions automatically.

**Later:** once Django populates `room_types`, room relevance can come from
catalog data rather than AI judgement.

**Related:** issue 9 (dead ends), issue 4 (seating bundles), and the
"we don't sell that" gap.

## F3. Let the customer choose the room's categories before the bundle is built

**Status:** Built on `feat/agent-catalog-awareness` (2026-09-26), in a different
shape from the plan below - see "What was built". Uncommitted.

**What was built (agreed with the user 2026-09-26):**
- **Rooms:** living room and bedroom, from a reviewed registry
  (`app/taxonomy/room_pieces_v1.yaml`) rather than from the designer's plan.
  - Tiers: essential (pre-ticked, removable), recommended (pre-ticked),
    optional (unticked). Only stocked pieces are offered.
  - Living room: essential sofa, center table, rug; recommended side table, TV
    table, floor lamp, wall art.
  - Bedroom: essential bed, nightstands x2; recommended wardrobe, rug, table
    lamps x2, TV table, wall art; mattress optional.
- **One question per turn, each once:** budget, pieces (chips with "Design my
  room" and "Choose for me"), how many will sit (living room), colour. "Just
  design it" skips the rest. Not the one-step checklist planned below: the
  user asked for separate turns.
- **The room is exactly the chosen pieces.** The designer adds only how each
  piece should feel.
- **Living-room seating sized to the head count.** A single piece, or a
  combination, weighed together with the rest of the room within the budget.
- **Missing pieces named, with a next step.** Fixes the reported reply "4
  pieces covered... 1 needed piece couldn't be included... remains to be
  resolved".
- **Head count confirmed, not assumed.** A head count from an earlier sofa
  search is confirmed ("is it for the 9 you mentioned?"), never reused
  silently. Fixes the reported "I did not specify seats": a sofa search for 9
  had silently sized the room, and the designer then planned an impossible
  single set for 9.
- Also: a chair need in a room never carries a seat filter, and room
  alternatives no longer trigger a seating combination.

- **Swapping one seating piece** keeps the head count: each seating piece
  carries its seat count, so its alternatives seat the same, and the reply
  counts real seats - "seating for all nine" only when true, and says so
  plainly when a room falls short (2026-09-26).

**Still open:**
- **Recomposing a built room** ("make it Japandi") goes through the designer
  as before, and does not re-apply the chosen pieces.
- **Other room types** (dining room, office) still use the designer-planned
  flow with the model's two opening questions.
- **The chip picker** has only been type-checked, not viewed in a browser (the
  browser tool was unavailable).

**Original plan (kept for reference):**

**Requested:** When the customer asks for a whole-room plan, show the
categories that would go into the bundle and let them choose which to include,
before jumping straight into building the bundle.

**Current state:** The whole-room flow goes straight through in one turn:
designer plans the needs → discovery per need → optimiser → bundle. The
customer first sees the categories only inside a finished bundle, and can then
only remove or swap pieces one at a time.

**Approach:** Split the existing flow at the point where it already divides:
after the designer's plan, before discovery.

1. **Plan.** The designer proposes the room's needs, filtered to what this
   store sells (existing behaviour). The plan is saved in the session as
   `room_project.design_needs`, but not yet searched.
2. **Show a checklist**, one step, all categories at once:
   - required and recommended needs are pre-ticked, optional ones unticked;
   - each shows the customer-facing name and its priority;
   - the customer can add a category from the store's menu (F2).

   Buttons: **Build my room**, and **Just build it** (accept the defaults).
3. **Confirm.** A button action carries the chosen positions. Code filters the
   saved plan and runs the existing discovery and optimiser. There is no second
   designer call: the plan is already made, and re-running it could change it.
4. **The bundle is shown** as today, with the existing swap, keep and remove
   controls.

**Rules:**
- **One step, smart defaults.** Show the whole list pre-ticked, never one
  category per question. The customer who does not care taps "Just build it"
  and gets a room immediately.
- Ask it **together with** the opening budget / household questions where
  possible, to keep the "at most two questions, once" promise.
- Only categories the store actually sells (F2 source).
- A category the customer adds is added to the plan as an optional need;
  budget allocation stays the optimiser's job.
- If they untick a required category, build without it and say what the room
  will be missing, rather than refusing.
- Typing ("skip the rug, add a floor lamp") runs the same handlers as the
  checklist buttons (lesson from issue 7).

**Changes:**

| Where | Change |
|---|---|
| `turn_coordinator.py` whole-room path | Stop after the plan; save it as pending; new confirm path runs discovery + optimiser on the chosen needs |
| Session state | Mark the plan as pending confirmation vs built |
| Backend request | New button actions: confirm chosen categories, add a category, build with defaults |
| Backend reply | The plan as a checklist (names, priority, pre-ticked) |
| Frontend | Checklist component with "Build my room" and "Just build it" |

**Rule change needed in CLAUDE.md:** section 10 says "propose a coherent
complete bundle" and "do not force the customer to pick every category
one-by-one." A single pre-ticked checklist is compatible with the second rule,
but section 10's flow and the "at most two opening questions" rule should be
updated to include the confirmation step.

**Related:** F2 (store category menu), issue 8 (household size in the opening
questions).

## F4. Simple "will it fit?" check

**Status:** Planned

**Requested:** Customers constantly ask whether a product will fit their room.
The agent should answer with simple approximate maths, e.g. "it's 220 cm wide
and your wall is 300 cm, so there's about 80 cm to spare". No floor plan, no
canvas, no exact geometry; approximate figures are enough today.

**Current state:**
- The designer is told never to promise a fit.
- Nothing compares a product's size with the room.
- What already exists:
  - room measurements the customer states are stored in the session
    (`RoomGeometry`: room length and width, ceiling height, usable wall
    lengths, doorway widths);
  - product sizes are in the catalog;
  - `dimension_semantics_v1` knows which stored column means "overall width",
    "depth" and "height" for each product type;
  - unit conversion already exists (`to_centimetres`).

**Approach: code does the maths, the AI explains it.**

| Check | Maths (code) | Example reply |
|---|---|---|
| Along a wall | product overall width vs usable wall length | "It's 220 cm wide; your 300 cm wall leaves about 80 cm." |
| Into the room | product depth vs room length/width, leaving walking space | "It's 95 cm deep, leaving roughly 2.3 m across the room." |
| Under the ceiling | product height vs ceiling height | Wardrobes, shelves, floor lamps |
| Through the door (rough) | product's smallest side vs doorway width | "Its smallest side is 85 cm and your door is 90 cm, so it should just go through. Worth measuring to be sure." |
| Room package | total footprint of chosen pieces vs floor area | "Together these take up about a third of the floor, which leaves comfortable space." |

**Rules:**
- **Always approximate and always hedged:** "about", "roughly", "should fit",
  "worth measuring". Never a guarantee. Placement, angles and doors opening
  are not modelled.
- **Code computes every number;** the AI never does the arithmetic. The
  numeric guard allows only figures the check produced.
- **Only trusted axes.** Use `dimension_semantics_v1`. For product types whose
  stored sizes are unreliable (beds, sectionals, sofa sets), say the fit cannot
  be checked reliably and ask for the product's measurements to be confirmed.
- **Missing data is said, not guessed.** If the room size is unknown, ask for
  it with one question (what is needed for this check only, e.g. "How long is
  the wall it's going on?"). If the product has no size or unit, say so.
- **Clearance figures are not hardcoded.** Walking-space allowances (e.g. about
  60-90 cm) come from configuration or from the designer's measurement
  guidance, recorded as general rules of thumb.

**Where it is used:**
- product detail and comparison when a room size is known;
- search results, where fitting products can be ranked first and a note can
  say which do not fit;
- room packages;
- a direct question ("will the second one fit?").

In the agent redesign this becomes a `check_fit` tool.

**CLAUDE.md change needed:** 15.1 and 17.2 say room fit belongs to the
designer and must never be promised. The update should allow deterministic,
clearly approximate fit checks by code, while still never guaranteeing a fit.
