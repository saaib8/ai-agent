# Agentic discovery questions — sheet as a knowledge base, not a script

## Status — built & verified (2026‑10‑29)

**Done, green, staged (not committed):**
- `a_designer_might_ask` now carries the sheet's **questions (subjects) only — no
  answer options** (`shopping.py`). The YAML keeps its questions as the baseline;
  the agent **decides the options itself** from the catalog + conversation.
- New **`ask_about` tool** (Sara) — resolves the kind and returns its designer
  questions **with no search/products**, so the *first* question (pre‑search) is
  sheet‑grounded.
- Prompt: for a bare need, `ask_about` first, then ask one question in her own
  words and choose the options herself (a baseline, not a script).
- Unit tests (questions shape, graceful fallback for an uncovered kind, the
  `ask_about` handler) + live opening eval cases (recliner/sofa/rug). `ruff`/`mypy`
  clean; unit suites green.

**Live result** — bare openings, options now agentic: "I want a recliner" →
*"What matters most — the reclining mechanism, extra comfort, or a compact
shape?"*; "I need a sofa" → *"What look or shape do you prefer?"*; "I want a rug"
→ *"What shape and size do you need?"*. The right **subjects** (from the YAML
questions), with the agent's **own options**, in its own words, never leading
with budget. 6/6 live passes.

## The goal (yours)

The "ZORY Product Search" sheet (~60 subcategories, up to 3 questions each with
answer options) should be the agent's **knowledge base** of *what is worth
asking* per category. When a customer asks about a category, the agent looks up
that knowledge and **asks in its own words, varying by conversation** — never a
hardcoded question, never a fixed multiple‑choice card.

## What's actually true today (verified in code + one recorded run)

I traced the current flow before planning. Three things are **proven**, one is
**observed**, one is a **real gap**.

**Proven — the hardcoded card is already gone.** The old fixed `BriefCard`
(`ProductBrief` → `subcategory_questions_v1.yaml` rendered as a multiple‑choice
card) was **retired** when the agent team landed:
- `app/schemas/agent_state.py:85` lists `product_brief` in `_RETIRED_STATE_FIELDS`
  — it is stripped from any old session on read.
- `app/agents/screen.py` (`presentation()`) never sets `brief` / `narrow_down`
  / `brief_chips`, so the live `/v1/chat` response never carries a card.
- The frontend `BriefCard.tsx` still exists but only renders if `presentation.brief`
  is set — which it never is. **Dead UI.**

**Proven — the sheet already reaches the agent as knowledge.** On a search,
`app/agents/tools/shopping.py:555` (`_designer_questions`) attaches
`a_designer_might_ask` to the facts: the sheet's questions + their usual
answers, *"for the agent to choose from, if it asks anything at all."* The
prompt already says so (`app/prompts/agents/v2.py:254`): *"a_designer_might_ask
lists the questions this store's designers ask for that kind … choose from them
when you ask."*

**Observed — the agent does phrase in its own words.** From the recorded eval
run (`evals/conversations/last_run.json`), the same opening comes out differently
across runs — *"let's build a living room that feels right for you"* vs *"let's
shape a living room around how you want it to feel"* — and the chips are the
agent's own store‑grounded `choices` (budget bands from real prices, store
colours), not a fixed card. (These are room/Nora turns; I did **not** yet watch
a single‑product "I want a recliner" turn — see Step 0.)

**The real gap — the first question is *not* sheet‑grounded.** The sheet
knowledge is surfaced **only** through `search_products` (shopping.py:587). It
is **not** in the agent's per‑turn `SessionBrief`. So when a customer states a
bare need ("I want a sofa") and the agent asks *before* showing anything — which
the prompt explicitly invites — it does **not** have the sheet's topics at that
moment. The prompt's first question is generic ("the room, how many sit there,
the look"), not the category‑specific knowledge from the sheet. The sheet only
helps *after* a search has run.

> So this is not a from‑scratch build. The agentic channel exists and the card
> is already gone. The work is: **make the sheet knowledge available at the
> moment the agent decides to ask (including before a search), and shape it as
> topics to rephrase rather than a verbatim question to recite.**

## Plan

Small, phased, each gate‑green (`ruff` + `mypy` + `pytest`), nothing committed
until you review.

### Step 0 — Observe, before any code (~15 min, no code)
Run 2–3 single‑product openings live (`evals/conversations/run.py` against a
local server): "I want a recliner", "I need a sofa", "something comfy to read
in". Read what Sara *actually says*. This decides how much of Step 2 is needed:
- if she already rephrases and picks sensible topics → Step 2 is light prompt
  wording;
- if she recites the sheet's questions verbatim → Step 2 is the real work.
If a live run isn't available I'll say so and proceed on the recorded‑room
evidence (which already shows rephrasing).

### Step 1 — Put the sheet knowledge where the agent decides to ask
Make the "what's worth asking" available **before** a search, so the first
agentic question is sheet‑grounded.
- **Contract (Pydantic/typed):** a compact `DesignerAsks` view — per category /
  subcategory, the **topics** worth asking (type, people, colour, budget,
  material, feature, shape…) with a few example values as *vocabulary*, derived
  from the existing `SubcategoryQuestions` loader. No new YAML; reuse
  `subcategory_questions_v1.yaml`.
- **Surface it in `SessionBrief`** (`app/schemas/agent_brief.py`, built in
  `app/agents/views.py`): when the latest message implies a category/subcategory
  (the agent's forming interpretation), include its `designer_asks`. Keyed off
  the taxonomy, catalog‑grounded where the catalog can answer (colours/budget),
  exactly as `a_designer_might_ask` is today — just available pre‑search.
- Keep the existing `a_designer_might_ask` on search facts (it stays useful for
  narrowing); Step 1 removes the "only after a search" limitation.

### Step 2 — Shape it as knowledge, instruct rephrasing
- The agent receives **topics + example vocabulary**, not a verbatim question
  string, so there is nothing to recite.
- Prompt (`app/prompts/agents/v2.py`, Sara's opening section): "these are the
  topics this store's designers ask about for this kind — ask **in your own
  words**, vary it, at most one at a time, skip anything already clear; never
  read them out as a list."
- This is the whole behavioural change; everything else is plumbing to feed it.

### Step 3 — Evals (prove it, catch regressions)
Add single‑product opening cases in `evals/conversations/cases.yaml`:
- "I want a recliner" → asks **one** question, on a **sheet topic** for recliners
  (type/features/budget), **not fallback**, **no fixed card**;
- repeated runs → **wording varies** (not identical verbatim);
- "a 3‑seater beige sofa under 4000" → asks **nothing already stated**.

### Step 4 — Optional, separate cleanup (not in the headline)
Retire the dead `BriefCard.tsx` / `product_brief.py` card‑rendering path and the
unused `SheetOption` card machinery, *if* confirmed unused in the team flow.
Its own PR — it's hygiene, not this feature, and you just reverted a batch, so
I'll keep it out of the main change unless you ask.

## How I'll implement it
1. Step 0 observation → confirm the real size of Step 2.
2. `DesignerAsks` typed view + a small builder over the existing loader (unit
   tested against the real YAML).
3. Wire it into `SessionBrief` / `views.py`, keyed off the forming
   category/subcategory; catalog‑grounded like today.
4. Prompt edit for rephrasing + one‑question discipline.
5. Eval cases; run the gate after each step; nothing committed.

## Open questions for you
1. **Topics vs. freedom** — should the sheet *bound* which topics Sara asks per
   category (my default: yes, it's the reviewed guardrail), or is it loose
   inspiration she can go beyond?
2. **Chips** — keep the agent's own tappable `choices` chips (store‑grounded,
   agent‑worded) alongside the question, or pure conversational text?
3. **Legacy cleanup (Step 4)** — do it now in a separate PR, or leave it?
