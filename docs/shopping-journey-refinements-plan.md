# Shopping‑journey refinements — questions, seating room‑size, Salman's room offer, Nora‑led cross‑sell

## Status (2026‑10‑29, staged, not committed)
- ✅ **Phase 1** — ask 2–3 questions before showing (incl. "show me"); verified live.
- ✅ **Phase 2** — seating room‑size question → Nora; verified live.
- ⏭️ **Phase 3** — cross‑sell through Nora: **built, then reverted and skipped.** The
  Salman→Nora→Salman relay ran ~47 s (4 hops, over the 40 s deadline) and fought
  `max_hops=3`. Nora's matching is great and *fast* when reached directly (~15 s), but
  the "shown by Salman" byline isn't worth the extra hop. Parked.
- ✅ **Phase 4** — Salman's bundled anchor offer (cross‑sell + "design the whole room"
  on a room‑anchor cart‑add, agent‑judged; side tables don't trigger it); verified live.

Four refinements, validated against the current prompt (`app/prompts/agents/v2.py`)
and code. All are **agentic** — code supplies facts, sums and guardrails; the
agent judges, asks and offers. Phase‑by‑phase, each green on
`ruff + mypy + pytest` and its own evals, **nothing committed** until reviewed.

## The four requirements (current → wanted)

| # | Requirement | Today | Change |
|---|---|---|---|
| 1 | **Ask 2–3 questions, agentic** — incl. on "show me sofas"; skip on enough‑info / "just show me" / size‑chosen kinds | asks **one** at a time; **"show me X" shows at once** | ask **2–3** up front, incl. on "show me"; ask the **gaps** on a half‑specified search |
| 2 | **Seating room‑size question → Nora** | **Sara** asks it (`v2.py:283`) | Nora asks it |
| 3 | **Salman's bundled offer** = cross‑sell products **+** "design the whole room" | two separate instructions; design‑offer gated on 2 pieces/met goal | one bundle; trigger: open goal → finish first; no goal → on a **room‑anchor** cart‑add (agent judges anchor‑worthiness) |
| 4 | **Cross‑sell matched by Nora, shown by Salman** | Salman's `what_goes_with`/`show_kind` runs its **own** basic look‑lean search (`sales.py:122`), contradicting §10.5 and his own prompt | Salman picks the kind → **Nora** searches it matched (complementary colours/styles/size) → Salman presents |

## Principles held throughout
- Agentic delivery, code = facts + guardrails (CLAUDE.md §3, §10, §17).
- One owner per job: matching/fit/design = Nora; cart/compare = Sara; value/cross‑sell offer = Salman.
- Keep `agent_state_v5`, old sessions readable; update CLAUDE.md where the contract changes.

---

## Phase 1 — Agentic 2–3 questions (incl. "show me")
**Goal:** the agent gathers a little before showing — 2–3 questions up front, in
its own words — unless they gave enough, said "just show me," or it's a
size‑chosen kind.

**Changes:** `app/prompts/agents/v2.py` (the "Show or ask first" block,
`:248‑257`) + CLAUDE.md §10.2. Prompt‑only.

**Approach:**
- For a bare need **or a "show me X" with little detail** → `ask_about`, then ask
  **up to 2–3** of its topics in your own words before showing.
- For a **half‑specified** search ("a beige sofa") → show if enough; otherwise ask
  the **remaining** topics (the gaps) first. The agent compares `searched_for`
  (what ran) against `a_designer_might_ask` (what's worth asking) — its judgment,
  no new code.
- Unchanged skips: "just show me"/decline → show; size‑chosen kinds → show.

**DECIDED: 1a — ask a few questions *before* showing, always** (for the
personalized journey), even on "show me sofas." Skips only: explicit "just show
me"/decline, a fully‑specified request (enough info), and size‑chosen kinds.

**⚠️ Known cost of 1a:** ~**96** eval cases use `show me …` / `now …` / `any …`
and expect **immediate products**. These get **re‑baselined** to the new
"asks first" behaviour as part of this phase — expected work, not a blocker.

*Chips:* default to asking the 2–3 questions **as natural text** (they type),
chips only on the primary question via existing `choices`. The richer
`QuestionGroup` (a tappable row per question) needs a frontend change
(dev‑doc Phase 1) — deferred unless you want it.

**Evals:** bare need → 2–3 topics asked, varied wording; "a 3‑seater beige sofa
under 4000" → shows (enough); "just show me" → shows; mattress → shows. Plus the
~96 "show me" cases re‑baselined to the chosen option.

---

## Phase 2 — Seating room‑size question → Nora
**Goal:** in the seating‑one‑piece flow, the room‑size question is Nora's (it
serves her fit/placement judgment), not Sara's.

**Changes:** `v2.py` — remove the room‑size ask from **Sara's** seating block
(`:283‑284` *"Before the second piece … ask its size …"*); add it to **Nora's**
block (when handed a next seating piece and the room is unknown, ask size + TV/
window via `note_room`, then search matched). CLAUDE.md §27.1. Prompt‑only
(Nora already owns `note_room`).

**Risk:** low. One extra hop (Sara → Nora) to ask; acceptable since the next
piece already hands to Nora for matching.

**Evals:** "sofas for 8" → first sofa → the **room‑size** question comes from
**Nora** (`workflow`/`agent` = nora), not Sara.

---

## Phase 3 — Cross‑sell through Nora (dev‑doc Phase 6)
**Goal:** Salman chooses *which kind* goes with the pick; **Nora** searches it
with her matching (complementary colours/styles, `size_ratio`); Salman presents.
Fixes the basic look‑lean and the contradiction in Salman's own prompt.

**Changes (code + prompt):**
- `app/agents/tools/sales.py` — `what_goes_with` keeps the **listing** mode;
  `show_kind` → **`suggest_kind`**: verify the kind is stocked and not the pick's
  own, return `{chosen_kind, goes_with, hand_to: nora, need: needs_design}`,
  **run no search**. Retire the `CompanionSearchBuilder` path if unused.
- `app/services/refinement_composer.py` `seed_new_task` → add
  **`skip_default_families`** so Nora's complementary colours/styles aren't
  overridden by the pick's/room's saved colours (a beige sofa must not force
  beige‑only rugs).
- Prompt: Salman picks the kind → hands to Nora; Nora searches with `match_with`
  the pick + her `direction`; the reply offers "more of this kind" + "design the
  whole room." CLAUDE.md §10.3 + the §17 table.

**Risk:** highest — a tool contract change + a hand‑off, and it **adds an agent
step (~5–15 s) per cross‑sell** (the dev doc's noted cost). Fallback if too slow:
keep Salman's search, apply only the seeding fix.

**Evals:** "beige sofa, then a rug" → handled by **Nora** (`handoffs_include:
nora`), and **not** only beige rugs first; a half‑met 10‑seat goal → no
cross‑sell, next piece is seating.

---

## Phase 4 — Salman's bundled anchor offer
**Goal:** when a **room‑anchor** pick hits the cart and **no goal is open**,
Salman offers **one bundle** — the cross‑sell products (Nora‑matched, from
Phase 3) **+** "design the whole room around your picks." An **open seating goal
finishes first**, then the offer.

**Changes:** `v2.py` — Salman's section: combine the cross‑sell and the
`around_picks` design offer into one choice; trigger on a room‑anchor cart‑add
with **no open goal**; **the agent judges** anchor‑worthiness (a sale that could
become a room — a sofa/bed/dining set, not a lamp), reading the goal state + cart
from the brief. CLAUDE.md §10.3, §36. Prompt‑only (tools/facts exist:
`around_picks`, goals, cart).

**Depends on Phase 3** (the bundle's cross‑sell should already be Nora‑matched).

**Risk:** low–medium. Agent judgment of "anchor‑worthy" could over/under‑fire;
evals pin it.

**Evals:** add a **sofa** (no goal) → offer **both** cross‑sell products **and**
design‑whole‑room; add a **side table** alone → **no** design‑whole‑room offer;
**open** seating goal → **no** offer until the goal is met.

---

## Sequencing
1 (independent) → 2 (small) → **3 (code, Phase 6)** → 4 (bundles 3's output).
3 before 4. 1 can run any time.

## Decisions
- ✅ **Phase 1 — "show me" behaviour:** **1a** — ask a few questions before
  showing, always (personalized journey). ~96 "show me" cases re‑baselined.
- ✅ **Phase 1 — chips:** natural‑text questions now; `QuestionGroup` deferred.
- ⏳ **Phase 3 — latency:** accept the ~5–15 s per cross‑sell, or hold the
  seeding‑only fallback? (decide at Phase 3)

## Verification (every phase)
`ruff check app tests` · `mypy app` · `pytest tests/unit` · the phase's
`--only=` conversation evals against a local server. Nothing committed.
