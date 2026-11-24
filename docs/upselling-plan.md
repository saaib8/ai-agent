# Upselling plan: product upgrades and higher-value alternatives

Status: **phases 1 and 2 built** (2026-11-24); phase 3 later. Decisions in
section 6.

Changed while building: almost no product lists a rating or warranty (store 21:
123 rated of 12,750), so "better on rating or warranty" would almost never find
a step up. The rule became **different upward on a listed fact** (section 4):
another main material, a 10% longer side, more seats, a longer warranty, or a
rating from 3+ reviews (4 or more beside an unrated product). Step ups are listed
nearest price first, so the one mentioned is the one shown first. A step-up or
cheaper search's price is never read as the customer's budget, and only a budget
for the same kind caps a step up.

Checklist rows covered (section 7):

- Recommend Product Upgrades
- Recommend Higher-Value Alternatives

Also makes **Recommend Lower-Priced Alternatives** (section 3) reliable, since it is
the same search pointed the other way.

## 1. What we agreed

- **Upselling is part of choosing, so it is Sara's.** Salman comes in only after
  something is chosen ("never in the middle of one") and stays on cross-sell. A
  step-up is offered while the customer is still deciding, which is Sara's moment.
- **No button.** It works by words and through the cards the widget already
  draws. The widget has no "More like this" button, only the console does.
- **The ranking is never overridden.** The cards shown stay the best match for
  what was asked, in the order ranking gave them. A step-up is always extra and
  always said as such.
- **A step-up is the same type as what it upgrades.** Sofa to sofa, never sofa to
  sofa set: a set is dearer because it is more pieces, not because it is better.
- **Upgraded on facts, never on price alone.** A dearer piece with nothing better
  about it is not an upgrade, and is not offered.
- **Not on a plain, mixed browse.** "Show me sofas" (sofas, sets and sectionals
  taking turns, with no look given) has nothing to step up from. Upselling waits
  until the customer points at one product.

## 2. How it will feel

> Customer: "Tell me more about the second one." (a grey Modern 3-seater, 2,289 SAR)
>
> Sara: its details... "If you'd like something longer-lasting in the same look,
> there's a grey 3-seater rated 4.8 from 12 reviews with a 5-year warranty, about
> 700 SAR more. Want to see it?"
>
> Customer: "Yes." Sara shows step-ups of that sofa as cards.

> Customer: "Do you have something like the first one but more premium?"
>
> Sara shows grey Modern sofas priced above it, each with what makes it a step up.

> Customer: "Anything like this but cheaper?"
>
> Sara shows the same look priced below it.

## 3. When it happens

| Moment | What happens |
|---|---|
| Plain or mixed browse ("show me sofas") | Nothing |
| Interest in one product: its details, or "is this good quality?" | Sara mentions one step-up in one line, if one exists, and offers to show it |
| Comparing products | Sara mentions one step-up of a compared product, if one exists, and offers to show it |
| "Something more premium / better than this" | Sara shows step-ups of that product |
| "Something like this but cheaper" | Sara shows cheaper look-alikes |
| A specific search with their look ("grey modern 3-seaters") | Phase 3, optional: one step-up card after the 5 |

Never offered unasked when:

- they gave a strict budget the step-up would break;
- they sorted by cheapest;
- they said it is too expensive, or the budget is tight;
- it is a room piece's options or a what-goes-with grid (rooms already spend
  leftover budget on better pieces).

At most one mention per product.

## 4. What counts as a step-up (decided in code)

A product is a step-up from product P when **all** of these hold:

1. **The same type** as P (`commerce_subcategory`).
2. **The same colour:** P's colour is required; its styles only order the cards
   (a lamp with three styles is not left with no step up for want of all three).
   The colour is lifted only as the last resort, and said.
3. **Dearer, within reach:** at least `upsell.min_step_share` above P (10%) and
   at most `upsell.max_multiple` times P (2x). A budget the customer locked wins
   over the cap.
4. **Better on at least one fact** P does not match:
   - a higher **weighted rating**, resting on reviews (the same weighted rating
     "best rated" uses, so one 5-star review does not count);
   - a longer **warranty** (`warranty_months`);
   - **Material never counts** (decision 4). A different main material is said as
     a fact ("solid wood"), never as "better", so there is no list of materials
     to maintain.

Among the products that qualify, the best match by look comes first, as ranking
orders it today. The price difference is worked out by code (the `calculate`
rules), never by the model.

The agent receives, per step-up:

- `price_more` (a figure key);
- `reasons`: the facts it beats P on (`rating`, `warranty`);
- `main_material`, as a plain fact beside them;
- nothing that reads as a score.

## 5. Build

### Phase 1 - the explicit request (about 1 day)

- `more_like_this` gets `price: "higher" | "lower"` (absent: as today).
  - **Higher:** floor = P x (1 + `min_step_share`), cap = P x `max_multiple`,
    within a locked budget. Then only products that are better on a fact (4.4)
    are kept, best look first.
  - **Lower:** cap = P x (1 - `min_step_share`). No "better" rule: cheaper
    look-alikes are a plain alternative.
- Each card's facts carry `vs_this` (`price_more` / `price_less`, `reasons`).
- When nothing qualifies, the facts say so (`none_dearer_and_better` /
  `none_cheaper`). The reply says this is already one of the best of its kind
  here, and offers the closest look instead. Never a dead end.
- Owner stays `SHOPPERS` (Sara and Salman, as today).

### Phase 2 - interest in one product (about 1 day)

- `product_details` facts get `step_up` when one exists for that product:
  `price_more` and `reasons`. No product reference is needed: on the customer's
  yes, Sara calls `more_like_this` with `price: "higher"`, which shows it as cards.
- Computed only when the rules of section 3 allow it (no locked budget it would
  break, no cheapest sort, not a room option).
- `compare_products` facts get the same `step_up` for each compared product that
  has one (decision 3). Sara mentions at most one, the step-up of the product
  they lean to, or of the best match when they lean to none.
- **Speed:** one extra search per product (for a comparison, run side by side).
  It runs beside the details or comparison read, ranked but not hydrated, and
  only the chosen product is read in full (for its warranty).
- **The cart** (decision 5): when the product stepped up from is in their cart
  and they choose the step-up, Sara offers to swap it ("Shall I swap it for the
  one in your cart?"). On yes, `remove_from_cart` takes the old one out and
  `add_to_cart` puts the new one in, with its quantity. Nothing is removed
  without their yes.

### Phase 3 - optional: a specific search (about 1 day)

- The pipeline keeps the next part of the ranked list (say the next 20) with depth
  and look score, instead of dropping it after the 5 are chosen.
- One step-up card after the 5, only for a single-type search with a look given.
  It must be at the same depth, with the same preference match as the shown cards,
  and within `agents.share_upgrade_similarity_margin` of the 5th card's look
  score. It is marked for the agent (`step_up`) and described as one more option.

### Instructions (Sara)

- One line, one fact, and an offer to show it. Never pushy, never with urgency.
- "Something more premium" means `more_like_this` with higher; "cheaper" means
  lower. Show at once, with no question first.
- Say why each card is a step up, only from `reasons`.
- The routing line moves "better value while choosing" from Salman to Sara.

### Settings

`upsell.min_step_share` (0.10), `upsell.max_multiple` (2.0) and `upsell.enabled`.

### Tests

- **Unit:**
  - floor, cap and a locked budget;
  - the same type only (a sofa set never steps up a sofa);
  - "better on a fact" (a rating with too few reviews does not count);
  - none qualifies;
  - lower;
  - no step-up on a cheapest sort or a room option;
  - Phase 3 margin and depth.
- **Live** (`evals/conversations`, each case run twice):
  - "more premium like the first one";
  - "cheaper like this";
  - details of a product that has a step-up (mentioned once);
  - details after "too expensive" (not mentioned);
  - "show me sofas, just show me" (no step-up).

### Docs

- CLAUDE.md 10.2 (Sara) and 17 (who owns what).
- `docs/known-issues.md` if anything is left out.

## 6. Decisions (2026-11-24)

1. **The range:** at least 10% dearer and at most 2x (settings). For a 2,289 SAR
   sofa, that is about 2,520 to 4,580 SAR.
2. **Phase 3:** later. Phases 1 and 2 first; decide on phase 3 once they are live.
3. **Moments:** product details, quality questions, and **comparisons**. Not after
   a like (♡): a like is a quiet signal, and an upgrade right after it feels pushy.
4. **Material:** a fact only, never "better". Only rating and warranty make a
   step-up.
5. **The cart:** offer to swap. The old one comes out only on their yes.
