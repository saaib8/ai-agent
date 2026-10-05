# Agent loop - implementation plan (draft for review)

Branch: `feat/agent-loop` (from `main` @ fe3fb86). Status: plan only, no code.
Revision 2 (2026-10-02): revised after an independent code review - see
section 10 for what changed and why.
Addresses known issues 1 (search box, not an agent), 11 (decision model
overloaded) and 12 ("we don't sell that" is a dead end).

---

## 1. The requirement

The assistant must act like a salesperson who looks at what the shop has before
answering, not like a search box. Concretely, when the first attempt does not
answer the customer well, the model should **see the result and choose the next
move** - try a related type the store stocks, build a combination, check what the
store carries, or ask one good question - instead of the application running a
fixed rescue chain written for situations predicted in advance.

Unchanged non-negotiables (CLAUDE.md 3, 8, 13, 20):

- the store is injected by code; no tool takes a store id;
- no SQL or database access for the model; tools are typed domain services;
- every product, price and number shown comes from a tool result in this turn;
- locked constraints are never silently loosened;
- the product category is never silently changed - an alternative is offered
  only as a disclosed alternative.

## 2. What happens today (facts from the code)

| Piece | Today |
|---|---|
| Turn shape | `CustomerTurnCoordinator._run_decided_turn`: one decision call → `_execute` dispatch on `AgentAction` → writer. Screen actions (`bundle_action`, `search_action`, `product_action`) bypass the model. |
| Decision | `CustomerAgentDecision`, 27 top-level fields (~138 across 33 nested models), prompt `customer_commerce/v1.py` (931 lines), 9 actions. |
| Search | `_new_search` → `_interpret` (query-understanding call, 4 typed outcomes) → card or `_seed_and_execute` → `_run_search` → `SearchPipeline.execute` (relaxation, semantic ranking). |
| Recovery | Hard-coded in `_run_search` (~line 4814), in this fixed order: `_another_type_seats_them` → `_maybe_compose_seating` → `_offer_closest_type_instead` (its own model call, `closest_type.py`). Relaxation adds `set_aside` counts. |
| LLM client | `StructuredLLMClient.parse(instructions, user_input, schema)` - **structured output only, no native tool calling**; it only validates. The corrective retry (21.1) is written per service (`customer_decision.py`, `query_understanding.py`, `closest_type.py`). |
| Question card | `_ask_or_search` shows the card **before any search** for most new needs; the search then runs on the next turn as a screen action (`_answer_brief`). There is no stock check before the card. |
| Writer | `CustomerResponseGenerator` + numeric guard + `_ends_on_a_question`; consumes `CustomerTurnResult` (already carries `offered_instead_of`, `unstocked_type`, `seating_solution`). |
| Orchestration | LangGraph is linear (load → run turn → render → persist → respond); all branching is in the coordinator. |
| Size | `turn_coordinator.py` 5,803 lines, 78 private async methods. |

Observed failures this design must fix (live, 2026-10-02):
"I need a bunk bed for my kids" → *"help find the right bunk bed"* + the bed card;
"do you have bean bags?" → silently chairs; "show me a treadmill" → honest, but
no stocked alternative offered.

## 3. Options considered

| Option | What | Verdict |
|---|---|---|
| A. Full loop from the first step | Replace the decision model with a tool loop for every typed message | Right destination, wrong first step: rewrites 9 actions at once, rebuilds rules that took weeks (question card, room questions, constraint semantics), high regression risk, latency added to every turn |
| B. Observe-and-decide after the search | Keep today's decision + first search; when the result is weak, the model sees it and may take up to N more moves | **Revised: phase 2, not phase 1.** The review showed most failures happen *before* any search (the card comes first), so a post-search hook misses them |
| D. **Stock-and-fit check before the card, then the loop** | Phase 1: before the card, check the resolved type against the store and whether it fits what was asked; one constrained alternative choice, disclosed. Phase 2: the loop, hooked after real searches including card answers | **Recommended** |
| C. More scripted branches | Add "bunk bed", "bean bag" rules | Rejected - the pattern this plan exists to stop |

Plan = D: the stock-and-fit check first, then B where results exist, growing toward A as each action becomes a tool.

> Sections 4-5 below describe the loop (now phase 2). Section 10 is the
> authoritative phase order and the corrections it brings to 4.

## 4. Design

### 4.1 Where the loop sits

```
typed message
  → decision model (unchanged)               ┐ today
  → first search via _new_search             ┘
  → assess(result)  ── good ──────────────────→ writer (no extra call)
        │ weak
        ▼
  agent loop (≤ max_moves, ≤ time budget)
     observe → Move → tool → observe → ... → Finish
  → CustomerTurnResult (same contract) → writer → number check
```

"Weak" is decided in code (deterministic, configurable), e.g.: zero products;
an empty pool after full relaxation; the asked type not stocked; a seat count no
single piece meets; query understanding returned `UnsupportedRequirement` or an
unstocked/unknown subtype; or the decision's wording names something the
resolved type cannot express (bunk, bean bag - surfaced by a new
`unmet_detail` field on the interpretation, see 4.4).

Screen actions, the question card, room questions, picks/cross-sell and the
next-step engine are untouched.

### 4.2 The loop on a structured-only client

No native tool calling is needed. Each step is one `parse()` call returning a
typed **Move** (a discriminated union); the loop executes it and appends a
compact observation. This reuses the existing schema validation, the corrective
retry and the fallback ladder unchanged.

```python
Move = SearchProducts | CatalogOverview | BuildCombination | Finish
```

- `SearchProducts`: category (fixed to the turn's category unless
  `as_alternative=True`), subcategory, wishes, seats, price - schema-restricted
  to approved taxonomy values (same constrained-schema technique as
  `closest_type.py`) and re-validated in code.
- `CatalogOverview(category)`: stocked types, counts, seat and price ranges -
  wraps `CatalogCapabilityService.overview`.
- `BuildCombination(seats)`: wraps `seating_solution.py`.
- `Finish(result_ref, disclosure)`: which observed result to present (or none),
  plus typed disclosure - `asked_type`, `shown_type`, `reason`
  (`not_stocked | cannot_seat | detail_not_carried | nothing_matched`) - mapped
  onto the existing `offered_instead_of` / `unstocked_type` fields, so the writer
  already knows how to word it.

Tools run in code with the store from `RetailerContext`; they return
**summaries** (count, type, colour/price spread, top refs), never raw rows. The
products presented are re-read from PostgreSQL as today.

### 4.3 Guards enforced in code, not in the prompt

- **Locked bounds:** a `SearchProducts` that loosens a locked price/seat bound
  relative to the customer's semantics is refused with a rule message (one
  correction allowed, then the move is dropped). Loosening is only ever *offered*
  in words (the existing `set_aside` facts), never executed (CLAUDE.md 13.5).
- **Category change:** allowed only with `as_alternative=True` and the target
  must be stocked; the result is always disclosed (`offered_instead_of`).
- **Budget:** `max_moves` (default 3) and `loop_timeout_s`; on exhaustion the
  best observation so far, else today's result, is presented.
- **Grounding:** `Finish.result_ref` must name an observation from this turn.
- **Failure:** any provider/validation failure → today's result, unchanged
  (never worse than now).

### 4.4 The small additions outside the loop

- `ResolvedSearch.unmet_detail: str | None` - the customer's own words for a
  detail the approved type cannot express ("bunk", "bean bag"). Set by query
  understanding; it is what makes the bunk-bed case "weak" and lets the reply
  say "we don't carry bunk beds - here are our beds" instead of pretending.
  No new taxonomy values are created (CLAUDE.md 14.3).
- Settings: `AgentLoopSettings` (enabled, max_moves, timeout, model,
  reasoning_effort) in `config.py`; default **off**.
- Prompt: `app/prompts/agent_loop/v1.py`, short (behaviour, not rules - rules
  live in the guards and the tests).
- Traces: one structured log per move (tool, arg summary, result count,
  latency); never customer text.

### 4.5 What is removed (phase 1, once the flag is on by default)

The fixed recovery chain in `_run_search` (`_another_type_seats_them`,
`_maybe_compose_seating` trigger, `_offer_closest_type_instead`) - the services
stay, the order and triggering become the model's choice. Until then both paths
exist and the flag picks one.

## 5. Phases

| Phase | Scope | Exit criteria |
|---|---|---|
| 0 | This plan reviewed; CLAUDE.md amendments drafted (alternatives rule in 13.5/27.1, new loop section) | Approved by the user |
| 1 | Loop after a weak new search: tools `search_products`, `catalog_overview`, `build_combination`, `finish`; `unmet_detail`; flag off by default | Section 6 criteria met with the flag on |
| 2 | Loop owns typed search/refine/detail/compare decisions; decision schema shrinks (those fields move into tools) | Suite parity; decision prompt materially shorter |
| 3 | Rooms (`plan_room`, `ask_designer`) as tools | Room cases parity |
| 4 | Delete superseded coordinator branches | Coordinator materially smaller; suite parity |

## 6. How we know it worked (phase 1)

- Full live suite with the flag **on** ≥ flag off (today 163/165).
- New eval cases, each run 3×, all pass: sofa for 6 → sets offered and
  disclosed; treadmill → honest + stocked alternative or a question; bunk bed →
  "we don't carry bunk beds" + beds; bean bag → disclosed alternative; beige sofa
  under 300 → honest + nearest real price offered; dining table for 12 →
  combination or honest; a good first result → **zero** extra model calls.
- Latency: p50 unchanged for good-first-result turns; weak turns p95 ≤ today's
  p95 + 4 s (measured on the suite).
- Zero number-check or grounding violations introduced.
- Unit tests: scripted fake-LLM moves cover every guard (locked bound refused,
  undisclosed category change refused, unknown result_ref refused, budget
  exhaustion, provider failure → today's result).

## 7. Risks

| Risk | Mitigation |
|---|---|
| Latency on weak turns | Only weak turns loop; move cap; time budget; measure before switching |
| Non-determinism | Guards in code; 3× repeats in evals; flag rollback |
| Loop re-learns rules badly | Rules stay in tools/guards, not prompt prose |
| Two paths drift while the flag is off | Short phase 1; delete the old chain as soon as the flag is on |
| Conflicts with parallel work in `turn_coordinator.py` | Loop lives in new modules (`app/agent_loop/`); coordinator gets one hook |
| Cost | Count model calls per turn in the suite report |

## 8. Decisions needed from the user

1. Hybrid scope (option B growing to A) - yes/no.
2. Alternatives rule: the agent may show a different stocked type when it says
   so plainly - yes/no.
3. Phase 1 slice = weak new searches only - yes/no.

## 9. Rough effort (phase 1)

Design sign-off 0.5 d; loop + tools + guards 2-3 d; `unmet_detail` 0.5 d;
unit tests 1 d; eval cases + live comparison 1 d. About one working week.

---

## 10. Revision 2 - after the code review (authoritative)

### Findings that changed the plan

| Finding (verified) | Consequence |
|---|---|
| The question card is shown before any search for most new needs (`_ask_or_search`, turn_coordinator ~4442-4451); live, "bunk bed" and "bean bags" got the card and ran no search | A post-search loop never fires for the headline cases → phase 1 moves **before the card** |
| `_run_search` is shared by card answers, paging, refinements and similar search, and its chain (`_another_type_seats_them`, `_maybe_compose_seating`) *is* the 27.1 spec | **Keep both deterministic.** Only `_offer_closest_type_instead` is replaced |
| `_run_search` commits state (active search, presented ids, sizes) | The loop needs an **execute-only** path; commit only what is presented |
| A move that restates "wishes, seats, price" can silently drop locked sizes / strict colour | Moves are **deltas on the turn's `ResolvedSearch`** (type change only); code carries the rest; measurements dropped on any type change (13.5) |
| Pydantic discriminated unions emit `oneOf`; strict structured output wants `anyOf` | Plain `Union` of models with `kind: Literal`, inside a wrapper |
| "we don't carry bunk beds" would be the model asserting a catalog fact (3.3) | A "not carried" claim needs a **catalog check** (name/semantic hit within the type), never the model's word alone |
| Free-text `unmet_detail` is unbounded, overlaps `semantic_text` / 12.3 | Replace with a closed `type_fit: exact \| broader_than_asked` + short `asked_kind_words` for the writer |
| The writer has no field for "detail not carried"; the number check would strip overview figures | Add `asked_kind` + a reason enum to `ResponseGroundingView`; allow overview figures |
| No Ask/chips move; treadmill gets nothing better | Phase 1 replies with **alternative chips** (stocked types); the loop gets an `Ask` move |
| Harness has no model-call count or latency percentiles | Add both before measuring |
| Card for an unstocked type with a feel-only card (e.g. chandelier) | Fixed by phase 1 (stock check before the card) |

### Revised phases

| Phase | Scope | Model calls added |
|---|---|---|
| 0 | CLAUDE.md amendments: disclosed alternatives (13.3/13.5, cross-category only when disclosed), the stock-and-fit check, the loop section. User sign-off | - |
| 1 | **Stock-and-fit check before the card** in `_ask_or_search`: (a) resolved type not stocked, or (b) `type_fit == broader_than_asked` and the catalog check finds no match → one constrained call over `catalog_overview` (extend `ClosestTypeResolver` to cross-category, disclosed) → reply with the disclosure + alternative chips; nothing searched. Fixes bunk bed, bean bag, treadmill, chandelier (issue 12) | ≤ 1, only on those turns |
| 2 | **The loop**, hooked after `_run_search` for customer-initiated searches (incl. `_answer_brief`): `_run_search` split into execute/commit; delta moves `SearchVariant \| Overview \| Combination(wraps _maybe_compose_seating) \| Ask \| Finish`; new `stage="agent_loop"` in the correction ladder; weak = zero exact results not already handled by `set_aside` or 27.1 | 1-3 moves, only on weak turns |
| 3 | Loop owns typed search/refine/detail/compare decisions; decision schema shrinks | - |
| 4 | Rooms as tools; delete superseded coordinator branches | - |

Harness work (`model_calls` per turn, p50/p95, card-first eval cases such as
"bunk bed" with `subcategory_not`/`mentions` checks) lands at the start of
phase 1, so every phase is measured the same way.

### Revised effort

Phase 0: 0.5 d. Phase 1: 3-4 d. Phase 2: 1.5-2 weeks. Total to the end of
phase 2: about **2-3 weeks**, not one.

### Phase 1 review outcome (2026-10-02)

Independent review: no blockers; eight should-fix items. Fixed: store-wide,
space-insensitive name lookup (S1); `asked_kind` limited to the customer's own
words plus a synonym rule (S2); disclosure kept on an empty substitute search
(S3); dropped sizes reported and not replaced by saved ones (S4); overview read
once per request (S5); prompt versions bumped (S6); one closest-type call, own
family first (S7); catch-all child (N7); typed answer to a substitute's card
(N8); debug header refused in prod (N5). **Deferred to phase 2 (S8):** a
not-carried type still runs a search that cannot succeed - the loop's weak-turn
trigger must recognise it rather than repeat the closest-type choice. Also for
phase 2: consolidate the three retype helpers (`as_type`, `_with_subcategory`,
the composer's `_retype`) and move disclosures to one `{reason, asked}` field.

### Phase 2 review outcome (2026-10-02)

Independent review: no blockers; seven should-fix items. Fixed: the chosen try
is committed from its own execution (execute/commit split, S6) on top of the
original search's state, so sizes are neither erased nor saved for the
alternative (S1; the same flaw in phase 1's substitution fixed with an explicit
`substitute` flag); only tries with exact matches can be presented, and the
writer no longer says "nothing was loosened" over a reported widening (S2); no
loop for a type the store does not stock (S3); a card shown for a substitute
remembers it (`PendingBrief.substituted_for`), and its answers stay with the
substitute and keep the disclosure (S4); siblings filtered by recorded seat
ceiling (S5) and by budget only in the store's own currency, min and max (N3).
A test found the card-answer path dropped every disclosure field from the turn
result - fixed. **Deferred to phase 3 (S7):** consolidate the three retype
helpers (`_with_subcategory` still keeps sizes for `_another_type_seats_them`
and the closest-type fallback), one `{reason, asked}` disclosure field instead
of four, and skipping the doomed search for a type not carried (phase 1 S8).

---

## 11. Phase 3 - plan (2026-10-02)

### 3a - done

- **One substitution helper** (`app/services/retype.py`): `as_type`,
  `composed_as_type` and `dropped_sizes`, used by the stock check, the agent
  loop, "another type seats them" and the closest-type fallback. The last two
  used `_with_subcategory`, which carried sizes across a type change (against
  13.5); it is deleted. The composer's `_retype` stays separate on purpose: it
  serves a type change the customer made, which may bring back their saved
  sizes.
- **Deferred, with reasons:** skipping the doomed search for a type not carried
  (phase 1 S8) - it saves one database search and no model call, and the loop
  already refuses unstocked types; one `{reason, asked}` disclosure field - to
  be shaped by 3b rather than reshaped twice.

### Evidence that re-scopes 3b

From the server logs of today's eval runs (5,181 decisions):

| Signal | Value |
|---|---|
| Decision corrective retries | 22 (0.4%) - all one rule: a refinement payload's shape |
| Turns that fell back to "please say it another way" | none observed |
| Actions | search 2,894 (56%), design_handoff 1,151, answer 625, refine 393, clarify 45, compare 33, show_selection 27, detail 13 |
| Decision latency | p50 7.1 s, p95 13.1 s |
| Search turns | decision (7 s) then query understanding (~4.7 s), in sequence |

Issue 11 ("one slip fails the whole turn") is now rare: the 21.1 ladder catches
the slips. The original phase 3 - the loop owning typed decisions as
multi-step moves - would add at least one model step (5-9 s) to most turns to
fix a failure seen in under 1% of decisions. The cost that every customer pays
is latency.

### Options for 3b

| Option | What | Effect | Risk |
|---|---|---|---|
| A. Original: loop owns decisions | Decide → tool → observe → decide, for search/refine/detail/compare | +5-9 s on most turns | High: rewrites the most tested path |
| **B. Speculative query understanding** | Start query understanding on the raw message at the same time as the decision; use it when the decision is a new search whose restated request is the message itself, otherwise discard it and interpret the restatement as today | about -4.7 s on most search turns | Low: same two calls, same validation; extra cost is a wasted call on non-search turns (~44%) |
| C. Faster decision | Measure `reasoning_effort=low` (or a smaller model) for the decision only, on the full suite | possibly -3-5 s on every turn | Medium: routing quality; switch only on suite parity |
| D. Merge decision + query understanding for new searches | One structured call emits the route and the full interpretation | -1 call on search turns | High: grows the schema the plan wants smaller |
| E. Tighten the one retry rule | Make the refinement payload shape unrepresentable (as the move schemas are) instead of corrected | removes most of the 0.4% retries | Low |

### Recommendation

3b = **B + E**, then **C measured**: the agent-like behaviour already lives where
it pays (phase 1 before the card, phase 2 after a weak search); 3b should make
every turn faster and the decision harder to get wrong, without adding steps.
Phase 4 (rooms as tools, delete superseded branches) is then re-assessed on the
same evidence.

Exit criteria for 3b: full suite parity (on vs off a flag), search-turn p50
latency down by at least 3 s, model-call mean per turn not above today's +0.5,
decision retries below 0.2%.

### 3b plan - revised after review (2026-10-02)

Review verdict: approve with changes. The order is now:

1. **Instrument first.** Log `has_search_request` on `customer_decision_completed`
   and add an `X-Turn-Action` debug header (refused in prod, like
   `X-Model-Calls`) so search-turn latency and B's hit rate are measured, not
   assumed.
2. **E as "normalise and ignore"** (21.1 step 1), not "unrepresentable" - strict
   structured output cannot express "one of these must be set". First confirm
   which rule the 22 retries broke from the logged violations; then an empty
   refinement delta becomes None, a refinement payload on a non-refine action is
   ignored and logged (`decision_payload_ignored`), and a refine with nothing to
   change stays refused.
3. **B behind `customer_agent.speculative_interpretation`** (default off), only if
   step 1 shows a useful hit rate. Reuse iff the interpreted input equals the
   message (whitespace-normalised) - which also covers `_refine_taxonomy`, a
   guaranteed hit. Reuse only a successful outcome; a speculative failure runs
   today's synchronous path, so error semantics and `_may_redecide` are
   unchanged. No speculation while a card, a room question or a seating
   question is pending (the model must restate). Cancel and await a discarded
   task; never surface its exception. Log `speculative`/`used` on
   `query_understanding_completed`.
4. **C measured:** `customer_agent.decision_reasoning_effort`, A/B on the full
   suite x3, switched only on parity.

Open decisions for the user: switching on 14.7 and 14.8 by default (issue 1 is
fixed in practice only then), and whether to pursue a slim router for the
decision (issue 11's overload and misrouting, which retry counts cannot
measure - option F).

### 3b results (2026-10-02, full suite, A/B on the same code)

| Step | Result |
|---|---|
| 1. Instrumentation | `X-Turn-Action` + `X-Model-Calls` (refused in prod); `search_request_restated` on the decision log. Hit rate: the decision restated only 6 of 206 search requests (97% reusable) |
| 2. E | A stray refinement payload on a non-refine action is ignored, not refused; decision retries 0.4% → 0.27% |
| 3. B (speculative interpretation) | search turns p50 21.4 s → 17.0 s (-4.4 s); all turns p50 18.4 → 15.7 s; model calls +0.08 per turn; parity (failures were staging errors and one flake present on both sides) |
| 4. C (decision `reasoning_effort=low`) | 189/190 vs 188/190 at medium; all turns p50 15.7 → 14.8 s, p95 34.4 → 29.8 s; design -2.2 s, refine -2.7 s; search unchanged |

Defaults: B and C on in local and stage with the phase 1/2 features; prod
unchanged; an explicit setting always wins. Router split (issue 11, option F):
decide on this data - the decision now costs ~7 s p50 at low effort with 0.27%
retries, so a split would have to beat that on both latency and routing.
Open: `seat_combo_strict_colour_honest` fails in every configuration since the
baseline - a pre-existing defect, investigated separately.

### Phase 3 review outcome (2026-10-02)

Independent review: approve with changes; one blocker. Fixed: config tests no
longer read a developer's `.env` (blocker - CI would have failed); a failed
speculative reading is handled once, never read again (it doubled query
understanding's corrective attempt and an outage's wait); a failure in a
discarded reading is logged (a defect at warning); speculative readings are
tagged on their own logs; `decision_payload_ignored` records what E forgave;
the startup summary shows the resolved features; 27.1 and the 18.1 placement
corrected; one `same_words` helper for the restatement metric and the reuse.
**Exit criterion not met, stated plainly:** decision retries are 0.27%, not
below 0.2% - the rest are a refinement with nothing to change, which is
genuinely unusable and stays refused. `decision_reasoning_effort` applies only
where `decision_model` is configured.

### Phase 3 testing outcome (2026-10-02)

Independent tester, full suite + 49 exploratory conversations, phase 3 on vs
off: speculation (B) ready - search turns p50 23.3 -> 17.3 s in the suite, no
stale or wrong interpretation, routing matched on 113/117 turns, +0.18 calls
per turn. Retype (3a) correct: sizes never carried to a substitute, the
dropped width disclosed. **C reverted as a default:** at `low` the decision
built "a room around my picks" at once, skipping its questions, in 4 of 8 runs
(0 of 9 at medium); its own gain was 1-4 s on refine and answer only. The
setting remains for a later measured attempt with that routing fixed first.
Pre-existing issues found on both sides are logged in docs/known-issues.md
(15-17).

---

## 12. Phase 4 - TO DO (parked 2026-10-05)

Not started. Before any code: a plan grounded in evidence (where room
conversations fail today, what each item would really fix, a latency budget),
reviewed by an agent, then the user's go-ahead - as for phases 2 and 3.

1. **Rooms as tools** - `plan_room` and `ask_designer` as tools the agent loop
   can call, instead of the fixed room flow in the coordinator.
2. **Delete superseded coordinator branches** - little is superseded yet,
   since phase 3 did not move typed decisions into the loop.
3. **Decide whether the agent drives more turns with tools** - the original
   "loop owns typed decisions" (search/refine/compare/detail), re-scoped in
   phase 3 for latency, plus the slim-router question for the decision
   (option F; baseline: decision ~7 s p50, 0.27% retries).
4. One disclosure field instead of four (`offered_instead_of`,
   `unstocked_type`, `kind_not_found`, `alternative_to`).
5. Skip the doomed search for a type not carried (phase 1 review S8).
6. Retry `decision_reasoning_effort=low` after fixing the room-around-picks
   routing it broke.
