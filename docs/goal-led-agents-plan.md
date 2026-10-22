# Goal-led agents: what the customer is finishing, decided by the agents

Agreed 2026-10-22. Replaces reflexes (cross-sell after every add, combinations
for a group, buttons that force a tool) with three architectural pieces.

## The failure this removes

"Seat 10 people, one sofa at a time" lived only in the chat history. After the
first sofa went into the cart, three fixed paths fired: the console sent "what
goes with it", code would not let the turn end until `what_goes_with` ran, and
Sara's prompt said "after an add, hand back for sales". Salman offered a rug
with 7 seats still missing. "More like this" lost the look they asked for, and
a room interview's answers lived only in history.

## A. Goals the agents own

- **State.** `AgentStateV1.goals`: what the customer is working toward, each
  `open` / `done` / `dropped`:
  - `seating`: seat N people (the room, the shape they want in their words);
  - `room`: furnish a room (the existing `room_project` is its engine);
  - `piece`: find one kind of thing for something ("a rug for my sofa").
  Each goal keeps its members - the cart products chosen for it.
- **Tools.** `update_goal` (Sara, Nora, Salman): open, revise, close or drop a
  goal, in the agent's judgement. `add_to_cart` takes `for_goal`, so a product
  joins the goal it was chosen for.
- **Progress is code's** (facts, never decisions), shown in the session brief
  for every open goal: for seating, seats covered by its members and seats
  left; members by cart place; budget used where one applies.
- **Iterative seating.** No more combinations from the agents: Sara opens a
  seating goal, asks how many the first sofa should seat, searches it, and
  after each add the brief says how many seats are left - the next question is
  the next sofa, matched to the ones already chosen (C). `seating_options`,
  `choose_seating` and `dismiss_seating` leave the agents' tools; the planner
  stays for the room engine only.

## B. Events, not forced flows

- Every button is an event the supervisor routes like a message. The console
  sends a pick as the customer's words ("I've added X to my cart"). Code no
  longer refuses to finish a turn until a given tool ran (`tap_handled`,
  `BUTTON_DONE` go); `tap_can_be_handled_by` stays as a hint.
- Prompt reflexes go: no "after an add, hand back for sales". Instead: read
  the open goals - continue the unfinished one, close the finished one, then
  offer what comes next.

## C. Nora's judgement drives matching

- **`search_products` takes `match_with`**: products already chosen (cart
  places, liked, a goal's members, the room). The design specialist gives one
  direction for the kind searched beside them (`DesignTask.MATCH`): colours
  and styles the store stocks for that kind, what to avoid, a size in
  proportion to the anchor, and - when the room's size is known - what suits
  it. Code turns it into a ranking lean (`RankingLean`); it orders, never
  filters. The facts carry the direction, so the reply says why.
- **More like this** records the look as their taste (`remember_taste`, the
  agent's call), so later searches lean the same way.
- **Room size when it matters**: Nora (or Sara, through the brief) asks for it
  once the sizes start to decide - a second sofa in a room - not up front.
- `design_advice` with `alongside` judges a piece against what they have.

## Phases

1. A (goals, iterative seating) - tests, QA, live eval.
2. B (events) - tests, QA, live eval.
3. C (matching) - tests, QA, live eval.
4. CLAUDE.md sections 10, 17, 27.1, 36 rewritten for goals and matching.
