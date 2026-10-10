# Designer-led shopping

Status: agreed 2026-10-08, including management's two-question requirement,
and refined the same day after a code review in four research tracks.

- Build on `bug-fixes`. The `feat/agent-loop` branch is set aside for now.
- Every new behaviour lives in its own small service module, with one hook
  into the coordinator. `turn_coordinator.py` (6,130 lines) must not grow
  scripted branches.
- Each phase ships behind its own switch, under the master setting
  `customer_agent.designer_led_search`.
- Each phase passes the gate, the live English and Arabic evals (flag off and
  flag on, side by side) and an independent QA review before the next one
  starts.

## Why

Today a new product search opens with a card of six rows of chips (CLAUDE.md
10.4). It has three problems:

- It reads like a filter form, not a designer.
- It shows nothing until it is answered.
- Anything outside its options has nowhere to go.

A good designer in a showroom works differently:

- They greet you and ask about your space and life.
- They show you pieces and learn your taste from what you react to.
- They explain why each piece suits you, and suggest what goes with it.

The goal is to help people **buy and design at the same time**.

## The decisions

### 1. Who does what

- **One assistant faces the customer: the sales agent** (the Customer /
  Commerce Agent).
- **The Interior Design Agent works behind it**, and never talks to the
  customer directly (CLAUDE.md 17).
- **Services and the database decide what exists.** Neither agent invents
  products, prices, sizes or taxonomy values (3.3).
- **Store data is the menu.** Every question's chips, kinds, colours, styles
  and suggestions come only from what the active store actually sells: the
  live capabilities, scoped by `store_id`.
- **English and Arabic** are covered throughout.

### 2. The opening: always two questions before products

- **Two questions are always asked before any product is shown**
  (management's requirement).
  - They come in one soft, polite, showroom-style message.
  - There is always something useful left to ask, so there are always two.
- **Exceptions:**
  - "Just show me" goes straight to products. This is an assumption: confirm
    it with management.
  - A head count no single piece seats ("a sofa for 8"): the seating-shape
    question (27.1) replaces the opening questions. It never adds a third.
- **Tone:**
  - an invitation, not a form;
  - optional wording ("roughly", "if you'd like");
  - a reason in a few words;
  - about life and space, not specs;
  - the answer is acknowledged;
  - the chips are shortcuts, never the only way to answer.
- **The agent chooses the two questions (decision B).**
  1. **Code prepares the allowed question kinds** for this customer and
     product. Each kind must:
     - fit the product type;
     - not be already known (from this message, earlier messages, the room
       profile, likes, selections or earlier searches);
     - not have been asked before;
     - not be budget;
     - have chips available from live store data.
  2. **The reply writer chooses the two best** from the allowed set, in the
     reply call that already happens: no extra model call, and it happens
     after the product type is known. It writes them as one natural
     invitation, building on the customer's own words.
  3. **Code attaches the chips** for the chosen kinds, from store data.
  4. **A choice outside the allowed set is replaced** with the next allowed
     kind from a default order. The customer never sees a bad question.
- **Question kinds** are a small shared set (about 10), defined once. Each one
  must change something, either the results, their order, or the designer's
  next recommendations:

  | Kind | What it changes |
  |---|---|
  | Which room is it for? | **The designer's next recommendations** (office → desk and office chair; living room → centre table, rug, armchair or second sofa; bedroom → nightstands, wardrobe). It also shapes the ordering by meaning and the wording. It never filters: the catalog's `room_types` is empty. |
  | How many people? | Seat count, for multi-seat seating and dining |
  | How much space / how wide? | Width, for types whose width is trusted (dimension registry) |
  | Which kind? | The subcategory, from the store's kinds |
  | What feel are you after? | Ordering by meaning |
  | How do you like to sit? | Ordering by meaning (seating) |
  | Anything you'd rather avoid? | Pushes matches down; never hides them |
  | Colours you like | Colour preference (store's stocked colours) |
  | Style you like | Style preference (store's stocked styles) |

  - **Kinds with no data behind them are left out**: table shape, storage,
    bed size and door type. They wait for Django data.
  - **Chips:**
    - Room: Living room · Bedroom · Office · Dining room · Other. **No Majlis
      chip**; "majlis" typed is understood.
    - People: **numbers**, `1 2 3 4 5 6+`.
- **How answers are used:**
  - **A tapped number is strong guidance, not a hidden filter.** Products
    known to seat that many come first, and products with no recorded seat
    count come after them. Nothing disappears.
  - **Budget is never asked up front.** It is used only when the customer
    raises it. A whole room still asks its total first (10.1).
  - **Anything the customer has said counts as answered**, including indirect
    wording ("family room" → living room, "me and my wife" → 2). It is never
    asked again.
  - **Any reply while the questions are open counts as the answer.** The
    opening is never re-asked, even if the reply changed nothing.
  - **Vague answers get a building question**: "for my family" → "How many
    are in your family?".
  - **A head count from an earlier search is used and mentioned** ("for the
    five of you you mentioned"), never carried in silently (10.1).
- **The opening state** (asked kinds, the answers, chip keys) is stored per
  product family. Stale chips are refused as `questions_expired` does today.

### 3. After products are shown

- **Five product cards** per list.
- **Narrow down is always beside the latest list**, merged with the **brief**:
  - known values appear as chips, each removable with ✕;
  - it opens pre-filled;
  - **+ Add** reaches budget, all stocked colours and styles, fabric feel,
    width, and an "Anything else?" line.
- **Soft taste questions come after the products**, one at a time:
  - "which of these two feels more like you?";
  - mood;
  - things to avoid.
- **Budget comes later**, only once they are engaged, and is asked lightly,
  once.
- **Free text is welcome everywhere**, and nothing known is ever asked again.

### 4. The buttons

| Button | Meaning | What happens |
|---|---|---|
| **Select** | "I want this" | Added to the selection list (picks); **what goes with it is shown after every selection** (decision Q5 B). |
| **♡ Like** | "I like this look" | Added to the **liked list** only, as a taste signal. Silent: nothing else changes. |
| **More like this** | "Similar ones" | A similarity search (same kind, its colour and style leaning), as typed "more like this" works today, available on any card, including older lists. |
| **Compare** | Side by side | As today. |

- **Typed "I like this one"** means Select: it adds the product to the
  selection list, as the decision model already does. Only the ♡ button
  means Like.
- **The liked list:**
  - It is shown on request ("what have I liked so far?") and in the picks
    tray, beside the selected products.
  - Products can be selected from it. That needs a liked-list reference,
    like `PickedOrdinal`.
  - Un-liking removes the product from the list and from the taste.
- **A ♡ tapped while a reply is in progress** is held, then saved when the
  reply finishes. This avoids a session conflict.

### 5. Taste

- **What the customer said** stays in `customer_preferences` (expressed
  preferences only, CLAUDE.md 12.4).
- **Like and selection taste is derived when needed** from the liked and
  selected products' real colour and style. It is never stored as numbers or
  aggregates, so un-liking removes it and inactive products drop out on
  their own.
- **Suggested values** inferred by the designer live in a separate field.
  They are shown as *suggested ✕* and never count as answered.
- **Taste guides; it never filters.** Only strict words filter ("only beige",
  "it must be Japandi").
- **Colours do not copy across kinds.**
  - The interior designer decides which colours go well with what was chosen
    (a beige sofa → ivory or terracotta rugs).
  - A liked colour leans only within the same kind.
  - Style may carry across kinds.

### 6. The Interior Design Agent's role

- **For every recommendation after a selection**, and for next pieces and
  rooms, the designer decides **what comes next** and **one direction**:
  palette, styles, character, what to avoid, and a size role.
- It works from:
  - the room they said;
  - what they selected and liked;
  - their taste;
  - the room profile;
  - what the store stocks, including **the store's stocked colours and
    styles per kind**, so the direction can actually match products.
- **The search brings the products, as today.**
  - The direction ranks and never filters.
  - It ranks below the customer's own stated preferences.
  - Products that satisfy the request exactly still outrank widened ones
    (16.1).
- **No price role** (dropped 2026-10-08, see phase 4 in detail): the
  designer never sees prices, and price comes only from the customer.
- **Size balance** is a typed figure, never in prose, used as a ranking key
  on trusted measurements only.
- **"More like this" stays a similarity search.** It is not a designer
  complement.
- **The designer does not see product images.**
- **Later upgrades, only if evals show the need:**
  - 2-3 "looks" with the cards spread across them (one search plus an
    alternating ranking mode);
  - the designer picking the final five from candidates' data.

### 7. How the agent talks about products

- **The reply highlights one or two cards.**
- **The reply explains its one or two highlights** - no separate line under
  each card (decided 2026-10-08, see phase 4 in detail).
- **The designer's direction is passed to the writer** on result turns. Today
  it reaches the writer only for design advice.
- **Only real data plus the direction** may be said: colour, style, size,
  price, and what the name says. No invented material, texture, quality or
  popularity. This is guarded by the prompt and by evals, since only numbers
  are checked by code.

## Known constraints found in the review

- **The current card's machinery is reused** and only its form UI is replaced:
  - key-based answers, `PendingBrief`, live chip counting;
  - the known-answer checks;
  - stale-card handling;
  - the kind, budget, width and taste transforms.
- **Two questions in one message** collide with the one-question rules:
  - the writer's question swap;
  - `already_asks`;
  - the numeric guard (chip digits must count as grounded).

  All three are updated in phase 1.
- **Session compatibility:**
  - `AgentStateV1` forbids unknown fields, so every new field is defaulted
    and left out when empty (the `reply_language` pattern).
  - A release that can read the new fields ships before one that writes them,
    so a rollback never breaks live sessions.
  - Every state reducer that rebuilds state must carry the new fields.
- **Evals:** about 150 of 203 cases assume today's card.
  - The runner's `search` step learns to skip either opening.
  - Card-specific cases are tagged `legacy_card`.
  - Flag-off and flag-on servers run side by side.
- **CLAUDE.md** 10.2, 10.4, 16.1 and 17 are updated at the start of phase 1,
  under a flagged section, not at the end. 10.4 already disagrees with today's
  cross-sell, which shows products.

## Speed

Pick turns take 18-22 s today. The aims:
- **about 15 s** for a selection turn;
- **under 10 s** for paging and "More like this";
- **unchanged** for the first message.

How:
- **The designer gets its own reasoning effort.**
  `interior_design.reasoning_effort` is measured at `low` against `medium`.
- **Its direction is cached** in the session, keyed by selections, taste and
  room, and reused for paging and refinements.
- **The search runs in parallel with the designer** whenever the kind is
  already known.
- **The typing effect is capped** at about 1.5 s per reply. Today it adds
  about 8 s after the reply is ready. The cards show at once.
- **The writer's output is capped.** The "why" lines add about 1-2 s.

## Phases

| Phase | Delivers |
|---|---|
| 0 | Live eval baseline (English and Arabic). The designer's reasoning-effort setting. The typing-effect cap. Direction caching. |
| 1 | **The two-question opening**: code filters the allowed kinds; the writer chooses and words two; store-data chips; any reply counts as the answer; "just show me"; the seating-shape exception; CLAUDE.md updated. |
| 2 | **Products, the brief and Narrow down**: five cards; brief chips; Narrow down pre-filled with + Add and "Anything else?", on the latest list. |
| 3 | **The buttons**: ♡ Like with the liked list (tray and "what have I liked?"); More like this on every card; likes queued during a reply; Select keeps its meaning. |
| 4 | **The designer's direction**: the room drives the next pieces; one direction (colours, styles, character, avoid, size role) from the stocked colours and styles; the shape rule for sizes; the reply explains its best one or two. |
| 5 | **Taste after products**: soft taste questions; suggested values; derived like taste; colours not copied across kinds. |
| 6 | **Room handoff**: the room flow starts from the room, profile, taste and selections, so only the budget is new; search colour bridges into the room. |
| 7 | **"Will it fit?"**, built last: a way for the customer to say which wall goes with which piece; horizontal fit from trusted measurements. |
| Later | 2-3 looks, and designer curation from candidates, only if evals show the need. |

Progress: phases 0 to 4 are done (2026-10-08), phases 5, 6 and 7 on 2026-10-09 (phase 6: head count carried unasked, said taste only, picks only on request, wall carried; phase 7: fit judged by the designer with the room's size, measured in code, no chips). The convention detector (4.2a) is not needed: sizes are read by the piece's longer and shorter floor side, so no store convention matters (CLAUDE.md 15.1, `discovery.size_by_side`), and a wall size orders with the designer's ideal width (CLAUDE.md 10.10). The question card of
CLAUDE.md 10.4 remains for `designer_led_opening` switched off; its eval cases
are tagged `opening: "off"` and run with `--opening=off` against a server with
the opening and the brief switched off.

Decided while building (2026-10-08):
- The opening is asked once per kind of product in a session: "I need a bed"
  said again later searches, it is not asked again.
- A request that already states everything still gets the two opening
  questions.

## Phase 4 in detail: the designer's direction

Proposed 2026-10-08, for approval before any code. Behind one setting,
`customer_agent.designer_direction` (on by default once evals pass; off is
today's behaviour exactly).

**Goal.** After a pick, the designer says what comes next for *their* room and
in which direction, the cards are ordered by that direction, and the reply and
each card say why - using only real data and the direction.

### 4.1 The room drives the next piece

- The complement request already sends the picks as anchors. It also gets the
  room they named in the opening (`customer_preferences.room`, phase 1) when no
  room is being designed, so an office pick leads to a desk or an office chair,
  a living-room sofa to a centre table, a rug or an armchair.
- The kinds offered are still only what the store stocks; nothing else changes
  in how the kind is chosen.

### 4.2 One direction, typed

The complement need gains a typed `direction`, written by the designer in the
same call:

| Field | Type | Used for |
|---|---|---|
| `colours` | approved colours, at most 3, **only from that kind's stocked colours** | ranking |
| `styles` | approved styles, at most 2, only from that kind's stocked styles | ranking |
| `character` | the existing `semantic_intent` ("low and light") | ranking by meaning |
| `avoid` | approved colours or styles to push down, at most 2 | ranking (last) |
| `size_role` | `smaller` / `similar` / `larger` than the pick, on one role | ranking by trusted measurements |

- **The store's stocked colours and styles per kind** go into the request
  (from the catalog overview; styles are added to its query). Values outside
  them are refused by validation and the corrective call, as for every model
  output (CLAUDE.md 21.1).
- **Never a filter.** The direction becomes preferences and ranking keys only.
  The customer's own stated colours and styles rank first; the direction fills
  only the families they left open; exact matches still outrank widened ones
  (16.1).
- **No price role** (decided 2026-10-08): price is not how things match, our
  catalog has no quality field it would stand in for, and budget is the
  customer's to state. Price comes only from them - a budget, "cheaper ones",
  a price sort. Revisit only if evals show suggestions at odd price levels.
- **Size role**: only when both the pick and the kind have a trusted
  measurement on the same role (the dimension registry decides). Code turns
  "smaller than the 260 cm sofa" into a target and ranks by closeness; an
  untrusted or missing size ranks after, never out.
- **Kept with the search**, so "show me more", "cheaper ones" and paging keep
  the same direction without asking the designer again. A new pick asks again.

### 4.2a Size, in any store (decided 2026-10-08)

- **Phase 4 ranks by size with the shape rule**: for any kind the store's own
  data shows to be clearly long and shallow (one floor side at least 30%
  longer in nearly every row - sofas, beds, TV units, desks, wardrobes,
  rectangular rugs), the longer floor side is the width and the shorter the
  depth, product by product, whatever column the merchant used; a near-square
  row is unknown. For other kinds (armchairs, square and round pieces,
  L-shapes) only the longest side and footprint are used. A plausibility check
  per kind sets wild values aside. The designer's size role ("centre table, smaller, about two-thirds of
  the sofa") orders by closeness; a product without usable sizes ranks after,
  never out. The reply never states a size the data does not show.
- **Then a general convention detector**, its own step after phase 4: for any
  store and any kind, it learns from the store's own data which stored column
  means width, depth, height or length - names that state a size ("180cm TV
  Stand"), seat counts against span, the typical range of each role, and
  agreement across rows (rows that disagree are flagged as swapped and
  ignored). It changes no data. Each result carries its confidence and
  evidence; only confident results are used, the rest stay unknown as today.
  It replaces the hand-written, store-50-only mapping (CLAUDE.md 15.2) and is
  what size filters and "Will it fit?" (phase 7) build on. A person approves
  each store's findings before they drive any filter; until then the shape
  rule orders only.

### 4.3 The reply explains the best one or two (decided 2026-10-08)

- **No line under each card.** The reply itself picks out the best one or two
  cards, by position, and says why each fits: "Two of these stand out - the
  first ... ; the third ...". At most two, never all five.
- **On every list it shows**: on what goes with a pick the reasons come from
  the designer's direction and the pick; on a plain search, from what they
  asked for. It is the same reply as today, so it costs no extra time.
- **The writer is told the direction** on these turns (colours, styles,
  character, size role) - today it reaches the writer only for design advice.
- **Only real data plus the direction** may be said: the card's colour, style,
  size, price and the words of its name. No invented material, texture,
  quality or popularity (prompt and evals); numbers stay checked by code, and a
  card it names must be on screen (grounding refs).

### 4.4 Out of this phase

- Whole-room plans keep today's designer call (phase 6 brings the room
  handoff).
- Running the search in parallel with the designer, and a separate direction
  cache - the direction kept with the search covers paging; parallelism is a
  later speed step.

### 4.5 Tests and evals

- Unit: the direction schema and validation (stocked values only, caps), the
  shape rule and the size ranking key, the ladder (customer > direction >
  pick's style), flag off unchanged.
- Evals, English and Arabic: an office chair after a desk in an office; a rug
  after a beige sofa in the living room leaning away from beige; the reply
  names at most two cards, each on screen, with no material claim; paging
  keeps the direction.
- QA agent after the phase, as before.

## Phase 5 in detail: taste after products

Built 2026-10-09 as decided below. Behind one setting,
`customer_agent.designer_taste` (off is today's behaviour exactly).

**Goal.** Once products are on screen, the agent learns their taste the way a
designer does - from what they react to - one light question at a time, and
everything it learns leans later searches the right way: what they said
first, what they liked and picked next, and never a colour carried where it
does not belong.

### 5.1 Three kinds of taste, kept apart (decided 2026-10-09)

| Kind | Where from | Used for | Shown as |
|---|---|---|---|
| **Said** | their words, or a tapped taste answer | every later search, any kind (as today) | a plain chip |
| **Learned** | the real colour and style of what they ♡ liked, picked, or tapped More like this on - caught quietly, never asked | colour only for the **same kind**; style for any kind | a *suggested* chip with ✕ |
| **Designer's** | the direction after a pick (phase 4) | that suggestion only | (not a chip) |

- Learned taste is worked out when a search starts, from the products' current
  catalog colour and style - never stored as numbers. Un-liking or unpicking
  removes it; a product leaving the catalog drops out. More like this taps are
  remembered (a short list) as a signal of their own.
- Ranking order: said, then learned, then the designer's; all order, none
  filters. A suggested chip's ✕ drops it from that search.
- **What we learned counts as known**: a question about something likes,
  picks or More like this already told us is not asked.

### 5.2 The soft taste questions (decided 2026-10-09)

Asked softly, after products, at most one per reply, each at most once a
session, and only about what nothing has told us yet (the seats question for
multi-seat seating stays first). They replace today's colour-or-style
follow-up.

1. **"Which of these two feels more like you?"** - two cards on screen that
   differ most in colour or style, picked by code; chips "The first" / "The
   fourth" / "Neither". The answer becomes *said* taste: that card's style for
   any kind, its colour for this kind. **It is never a pick.**
2. **"Which style feels right?"** - the styles the store really has for this
   kind, the ones on screen first (no mood words). The answer is a said style.
3. **"Anything you'd rather avoid?"** - chips of the colours and styles on
   screen. The answer **pushes those down; nothing is hidden**.

Answers are keys read back through a pending question in the session, like
the card's; a typed answer ("the first feels more me") is read into the same
keys, and is never a pick.

### 5.3 Out of this phase

- Budget stays as today (asked lightly, once, only when engaged).
- No change to the opening (phase 1) or rooms (phase 6).

## How we judge it

Every phase adds English and Arabic eval cases. Live metrics:
- time to the first product;
- the opening answer rate;
- likes and selections per session;
- "More like this" use;
- rooms started after a search.

## Out of scope

- Photos of the customer's room.
- The designer seeing product images.
- Memory across sessions (CLAUDE.md 19).
- Questions with no catalog data behind them (shape, storage, bed size).
- The `feat/agent-loop` branch.

## To confirm with management

- Customers who say "just show me" skip the two questions.
- Time to the first product rises by design, because there are always two
  questions first. It is measured from phase 1.
