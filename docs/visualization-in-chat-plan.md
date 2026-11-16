# Room pictures through the chat - plan

Status: built on this service (2026-11-15) - D1, D2, D3 (the button as a
chat tap), D4 and the endpoint checks of D5. The backend's part (sending
`visualization`, billing chat pictures) and the widget's move of the package
buttons to the chat are still to do. Needs changes in this service and in the
backend's gateway (`zory/backend`, `core/ai_agent/`).

## 1. The problems, in plain words

We draw three kinds of room picture:

| Picture | How it starts today | Code |
|---|---|---|
| One product in a room ("See it in a room") | A chat message or the card's button - Sara's tool `visualize_product` | `render_single` |
| The room package / shopping list | The package's "Visualize" button - its own endpoint `POST /v1/visualizations` | `VisualizationTurnRuntime`, `render` |
| Products picked in Browse Catalogue | The catalogue's room step - its own endpoint `POST /v1/catalog/visualizations` | `CatalogVisualizationRuntime`, `render_selection` |

1. **Anyone can get free pictures through the chat.** The backend's gateway
   locks the two picture endpoints: the shopper must be signed in
   (`require_sign_up`), their role must open Visualization
   (`visualization_refused`), and each picture costs one design generation,
   refunded if it fails (`core/ai_agent/render_views.py`). "See it in a room"
   is a chat message, and chat is open to every role, anonymous included: no
   sign-in check, no Visualization check, no design generation charged - at
   most a five-minute session, which may already be running
   (`core/ai_agent/conversation.py`).
2. **Typing cannot draw the room.** "Show me the second one in a room" works;
   "show me my living room", "show it from above" or "put it in my photo"
   does not - only the package's button can.
3. **The agents half-forget pictures.** The two endpoints save a stand-in line
   to the history ("[Visualised 4 pieces picked from the catalogue in a 4 x 5 m
   modern living room, corner view]") and the reply, but not which products were
   drawn; nothing about any picture is in the brief the agents read each turn
   (`app/agents/views.py`), and the history line is folded away as the chat
   grows. "Swap the sofa in that picture" has nothing to go on.

The root of all three: the pictures do not share one path. Two are drawn
outside the agents; the third is drawn inside the chat without the buttons'
checks.

## 2. The goal

- Every room picture can be asked for **by typing**, and the buttons keep
  working exactly as they do.
- **One rule for who may draw**, decided by the backend and respected by every
  picture, whichever way it is asked for; every picture **billed** the same way.
- Every picture **remembered**: what was drawn - its pieces, the room, the view
  - in the shared memory every agent reads, so they can follow up on it.

## 3. Decisions

### D1 - The backend decides who may draw; we respect it

The gateway already knows whether the shopper is signed in, whether their role
opens Visualization, and whether a design generation is left. On every chat
request it sends us the answer as one field:

```json
"visualization": "allowed" | "sign_up_required" | "limit_reached"
               | "not_included" | "store_unavailable"
```

**Five fixed values** (decided 2026-11-15). The gateway maps its own refusal
codes onto them, so its codes can change without this service:

| Value | When | The gateway's codes that map to it |
|---|---|---|
| `allowed` | may draw | - |
| `sign_up_required` | an anonymous shopper | `sign_up_required` |
| `limit_reached` | their own allowance is used up for now | `visitor_daily_limit`, `member_feature_cap_reached`, `member_cap_reached`, `feature_limit_reached` |
| `not_included` | their role or access does not include it | `tool_not_allowed`, `feature_not_allowed`, `no_role`, `view_only`, `tool_not_on_key`, `feature_not_on_key` |
| `store_unavailable` | the store does not offer it right now | `design_generations_exhausted`, `store_budget_reached`, `pool_exhausted`, `no_pool`, `feature_allocation_spent`, `feature_not_in_plan`, `feature_not_entitled`, `feature_not_in_widget`, `feature_off_here`, `unit_budget_reached`, `portal_limit`, `key_revoked`, `key_limit_reached`, and any code not listed |

**Never a reason to refuse the message.** Case and spaces are ignored; any other
value - a new code, a typo, the wrong type - is read as no picture, worded like
`store_unavailable`, and logged (`visualization_answer_unrecognised`).

The gateway works it out itself and **overwrites** whatever the widget sent -
the widget runs in the shopper's browser and could send anything. It is worked
out fresh on every request, so a shopper who signs in mid-chat can draw on their
next message. The same field is sent to the two picture endpoints while they
stay (D5).

**Missing field** (the internal console, direct calls): a setting,
`visualization.allowed_without_gateway` - true wherever the console runs (local
and stage, where it has no gateway in front of it), and **false in production**:
no field, no picture (decided 2026-11-15). A request that carries the field
always follows it, so the widget on stage is checked like production.

### D2 - Every chat picture is a tool, and the tool enforces D1

- `visualize_product` (one product, exists) and a new **`visualize_room`** (the
  room package: a view, or "in their photo" - the last room photo they
  uploaded) both refuse before any image is generated when the field is not
  `allowed`, carrying the backend's code.
- On a refusal the agent says what the value means, in one line in its own
  words and the conversation's language - `sign_up_required`: signing in or up
  lets them see rooms drawn; `limit_reached`: their room pictures are used for
  now; `not_included`: not part of their access; `store_unavailable`: not
  available here right now - and carries on helping. It never promises a
  picture.
- Owner: **Sara alone**, as today for every picture (CLAUDE.md 25). Nora hands
  over when a picture of the room is asked for.
- Typed requests reach the tools like any message: "show me my room from
  above" - `visualize_room(view=top_down)`.

### D3 - Buttons become fixed commands

The package's "Visualize" and view buttons, "Place in my room" and the
render's "Try this view" / "Render again" go to chat as taps that name the tool
they run (`Tap.runs`, as the kind chips already do): the turn starts with that
tool's owner, no model chooses, and a tap still always draws. Their words stay
the customer's side of the conversation (`say_visualize_room`,
`say_place_room`, already in the labels).

### D4 - Every picture is remembered

A new field in the shared state, `last_render`, written by every picture,
whichever path drew it:

- where it came from - one product, the room package, or Browse Catalogue;
- its pieces, each with its quantity;
- the room (kind, style, size) or "their own photo";
- the view.

Every agent's brief shows it as **the picture on screen**, its pieces named by
place ("the picture's second piece") and by name, and the history line says
which products were drawn, not only how many. The pieces of the newest picture
become the list "the second one" refers to, as cards shown last do.

### D5 - Browse Catalogue keeps its endpoint for now

The catalogue sends exact product ids and a room set up in a form; our rules
keep ids out of agent tools. So in this plan its endpoint stays, and gains:

- D1's field, checked before drawing (the gateway's own checks stay too);
- D4's memory, so "swap the sofa in that picture" works in the chat after it.

Moving the catalogue into the chat as well (its picks saved to the session as a
"studio selection" an agent can refer to) is a later step, decided after this
one is in use.

The room package's endpoint `POST /v1/visualizations` stays working until the
widget sends the package's buttons through the chat (D3), then is retired.

### D6 - The backend bills a chat picture as a picture

A chat reply carrying a room picture (`presentation.render`) is charged one
design generation, as the picture endpoints are, with the same duplicate window
(120 s) and refund. Because `allowed` already includes "a generation is left",
the charge after the reply can only fail in a race; then the reply is still
returned and the shortfall logged, as a chat session's is today.

## 4. Changes

### This service (`zory-agent`)

1. `ChatRequest` (and the stream): the optional `visualization` field; the
   setting for a missing one (D1).
2. `visualize_product`: refuse unless allowed; the reply code and facts for
   each refusal (D2).
3. New `visualize_room` tool: the room on record, a view, or the last room
   photo; refuses unless allowed; fails cleanly when there is no room or no
   photo (D2).
4. Fixed-command taps for the package and render buttons (D3).
5. `last_render` in `AgentStateV1` (an optional field, so stored sessions still
   read), written by all three pictures; the brief's "picture on screen"; the
   history line naming the pieces (D4).
6. The card's "See it in a room" button and the package's picture buttons are
   only offered when allowed, so the widget never shows a button that will be
   refused.
7. Sara's and Nora's instructions: when to use `visualize_room`, and what to
   say on a refusal.
8. Docs: CLAUDE.md (17, 25), `docs/widget-display-contract.md`.

### The backend (`zory/backend`, `core/ai_agent/`)

1. Work out `visualization` from the same checks the picture endpoints use
   (`require_sign_up`, `visualization_refused`, the design generations) and add
   it to every chat request and to the two picture endpoints, overwriting the
   widget's.
2. Charge one design generation when a chat reply carries a picture (D6).
3. Once the widget sends the package buttons through chat, retire the
   `/v1/visualizations` door.

### The widget

Probably nothing for D1-D2: it shows the buttons our replies offer. D3 needs
the package's and the render's buttons sent as chat taps instead of calls to
`/v1/visualizations`, one change for the frontend developer.

## 5. Tests

Unit, on our side:

- each picture refused for `sign_up` and `off`, with no image generated, and
  drawn for `allowed`;
- a missing field follows the setting;
- typed requests reach `visualize_room` (from the conversation tests), each
  view and "in my photo"; no room or no photo refused cleanly;
- each fixed-command tap runs its tool with no model choosing;
- `last_render` written by all three paths; the brief shows it; "the second
  one" after a picture is its second piece; an old stored session still reads;
- buttons offered only when allowed.

Live, local and staging: each picture by button and by typing, for an allowed
shopper; refused and worded for `sign_up` and `off`; "swap the sofa in that
picture" after each kind of picture; the chat regression set.

## 6. Order of work

1. D1 and D2 on our side, with the missing-field setting - closes the free
   pictures as soon as the backend sends the field.
2. The backend: the field (D1) and the billing (D6).
3. D4 memory, for all three pictures.
4. `visualize_room` and the typed room pictures (D2).
5. D3 fixed-command buttons, then the widget's change, then retiring
   `/v1/visualizations`.

## 7. Open questions

1. **This service can be reached without the gateway.** The staging host
   answers `/v1/...` directly (it answered our test chats with no widget
   session), so anyone can call the picture endpoints or set the field
   themselves. The gateway's checks only hold if the service accepts requests
   from the gateway alone - a shared secret header, or the network. This is
   needed for D1 to mean anything, and is worth doing on its own.
   **Deferred (2026-11-15):** the address is not published anywhere, so
   locking it is left for later; until then a direct caller could set the field
   themselves - an accepted risk.
Decided (2026-11-15): Sara alone owns `visualize_room`; the gateway sends one of
five fixed values and an unknown one never rejects the message; a missing field
refuses in production; picture buttons stay visible (a refusal is explained),
so `can_visualize` is optional for the widget.
