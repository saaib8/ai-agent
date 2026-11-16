# Photos in the chat and pictures of the cart - plan

Status: built 2026-11-16 on this service (branch photos-in-chat) and the
backend (branch chat-photos): D1-D8. Not built: the finder's outline screen in
the chat (D5 - Sara names the pieces found instead), and the console's and
widget's photo button sending the photo with the message.

## 1. The problems, in plain words

1. **"Visualize my cart" does not work.** A room picture draws only a room Nora
   designed, and only as it was designed:
   - a cart filled from searches (a sofa, a rug, a lamp) has nothing to draw;
   - a designed room put in the cart is frozen: a console bought for it later
     never appears, and one taken out of the cart still does. Asked to "show it
     with the console", Sara had no way to, the request bounced and the reply
     invented "pictures aren't available" (chat web-f2e496d82855).
2. **A photo always goes to the Furniture Finder.** The chat's photo button
   uploads straight to the finder (`POST /v1/furniture-finder/photos`), which
   outlines the furniture and waits for a tap. A shopper who wants to see their
   picks in a photo of their room has a different button on the room package
   (`POST /v1/room-photos`), and only there.
3. **The finder is outside the agents.** Its tap is its own endpoint
   (`/v1/furniture-finder/picks`) with a fixed reply; no agent decides anything,
   and typing "find this bed" with a photo is not possible.

## 2. The goal

- **"Visualize my cart" works any time** there is something in the cart: it
  draws what is in the cart now, room by room.
- **One photo button in the chat.** A photo sent on its own: Sara asks what
  they want - find furniture like what is in it, or see their cart in this room
  (only when there is something to draw). A photo sent with words goes straight
  to what the words ask ("find this bed", "put my cart in this room") - no
  question.
- **The finder is Sara's tool**, so its results are a turn like any search and
  "cheaper ones", "compare the first two" follow on naturally.

## 3. Decisions

### D1 - A picture draws the room as it is now

`visualize_room` (Sara's) draws one room, and what it draws is worked out in
code from the session, never chosen by a model:

| The room | What is drawn |
|---|---|
| Designed, not in the cart | the design's pieces (today) |
| Designed and in the cart | the room's cart lines now: its pieces, plus what was bought for it after, minus what was taken out |
| No designed room | the cart's products for that room |

"For that room" uses what the cart already knows about each product
(`pick_rooms`, `app/services/room_cart.py`): a room the customer named, a room
design that took it, or a kind that belongs to one room only (a bed is a
bedroom's). Example: a sofa, a rug and a lamp picked from searches, with
"living room" said earlier, are drawn as one living room.

For a cart with no designed room, Sara asks two things, one a reply, then
draws:

1. **Which room** - "Which room would you like to see: the bedroom or the
   living room?" - when the cart's products are for more than one room, or
   their room is not known. One room a picture.
2. **Which products** - the cart's products for that room, named, with "All of
   them" first: "Shall I show all of them - the sofa, the rug and the lamp - or
   just some?" They answer with the chip or in words ("just the sofa and the
   rug").

Then it is drawn. A product whose room was not known and that they choose for
this room is recorded as that room's (`set_pick_room`).

What they already said is not asked again: "visualize my living room" names
the room, "show the sofa and the rug together" names the products, and a cart
for one room with nothing to choose between asks neither. A designed room is
not asked about: its pieces are the room.
- **The room around the products**: its kind from the room; its size from
  `note_room` when the customer gave one, else a usual size for that kind (a
  setting); its look from the room's palette and styles, else from the
  products' own styles. Drawn through the same path Browse Catalogue uses
  (`render_selection`), which already places chosen products in a room.
- **Too many pieces for one picture**: the picture's existing piece limit
  applies; the reply names what was left out.
- Remembered like every picture (`last_render`, source `cart`), so "the lamp in
  that picture" works after it.

This is the fix for problem 1, and for the bounce: Sara always has something
to draw.

### D2 - The photo comes with the chat message

The photo is sent **with the message, to the chat endpoint** (decided
2026-11-16), so a photo with words or on its own is one request and one turn:

- **Widget -> backend**: the chat request carries the photo as a file, as the
  finder and room photo uploads do today (multipart, `image`, beside `message`
  and `session_id`). A photo with no words is allowed.
- **Backend -> agent**: the backend prepares the photo first, exactly as it does
  every photo today (`core/ai_agent/photo.py` `prepare`: upright, location and
  device metadata removed, re-encoded small enough to send), then sends it on
  the chat request as `photo` (base64 JPEG). The agent's chat request stays
  JSON, so its stream keeps its shape.

On that turn, before the agents run, code:

1. prepares it (upright, bounded in size, a real image) - an unusable photo is
   answered plainly, nothing else run;
2. runs the existing room check once (`RoomCheck`, the vision call room photos
   use today: one yes or no, nothing else read) and keeps the answer - a check
   that cannot be made counts as "not known", never as "not a room";
3. keeps the photo for the chat, with that answer. One photo a chat: a new one
   replaces it.

Later messages refer to the kept photo ("now find the lamp in it") and never
send it again. The expensive steps run only when a tool needs them, once per
photo:

- the finder's detection, when furniture is to be found in it;
- the room emptying, when their cart is to be drawn into it.

The room check (step 2 above) is the one cost every photo
has; the backend bills a photo turn accordingly (backend change 3).

### D3 - A photo with no words: Sara asks what they want

A message that is only a photo goes to Sara like any message, and she decides,
in her own words and the conversation's language. She offers only what can be
done:

- **Find furniture like this** - the finder (D5) - always.
- **See my cart in this room** - the photo as their room (D6) - only when the
  room check said it is a room and there is something to draw: products in the
  cart or a designed room. The brief says both (`photo_on_record`: a room or
  not; `photo_can_show`: the cart, the designed room, or nothing).

With nothing in the cart and no room designed there is only one thing to do,
so she does it - the finder runs on the photo - rather than asking a question
with one answer.

The two answers she offers are chips; a tap on one is the customer's answer
like any chip, and Sara runs the tool it names.

### D4 - A photo with words: straight to the tool, no question

The words decide, and Sara picks the tool - the same rule as every message:

| They send | What happens |
|---|---|
| photo + "find this bed" | `find_in_photo`, piece "bed" |
| photo + "find something like this" | `find_in_photo`, no piece named |
| photo + "put my cart in this room", "show my picks here" | `visualize_room` in their photo |
| photo + "how would this sofa look here" (a product on screen) | that product in their photo |

Only when the words fit neither does Sara ask, with the two choices.

### D5 - The finder is Sara's tool, `find_in_photo`

- **Detection** runs once on the chat's photo and is kept (as `FinderPhoto` is
  today). The pieces found are named by kind and place, never by id: "the bed",
  "the left lamp", "the second chair".
- **A piece named and found once** ("find this bed", one bed): its matches are
  the cards on screen - the same search, ranking, store check and commit as
  today's finder pick (`FinderTurnRuntime.pick`), so "anything cheaper?" refines
  it.
- **Several of that kind, or no piece named**: the photo is shown with its
  outlines (today's finder screen) and Sara asks which one; a tap on an outline
  is a fixed command (`find_in_photo` with that piece), and naming it in words
  works too.
- **The kind is not in the photo**: Sara says what was found in it instead and
  asks which.
- The finder still runs as the shopper (the platform's own finder): the chat
  request needs the shopper's widget session, as the finder endpoints get it
  today (backend change 2).

### D6 - Their photo as their room

"See my cart in this room" empties the chat's photo once (today's
`RoomPhotoService`: a vision check that it is a room, then the emptying) and
draws D1's room into it. "My room" in words, with no photo sent, still means the
designed room (decided 2026-11-16): a photo is used only when they send one.

### D7 - Who may use it: the backend decides, as for pictures

The gateway already decides who may draw (`visualization`, five values). The
finder gets the same: a `furniture_finder` field with the same five values,
worked out from its own check (`guard(tool=FINDER_TOOL)`) and overwriting
whatever the widget sent. `find_in_photo` refuses unless it is `allowed`, with
the same refusal wording; drawing still follows `visualization`. A missing
field follows the same setting as pictures (draws locally and on stage, refused
in production).

Visitors get the finder and not Visualization today (`GUEST_AGENT_GROUPS`), so
a visitor's "See my cart in this room" is answered with the sign-up wording,
and the chip stays (decided for picture buttons, 2026-11-15).

### D8 - Billing follows the work, as today

- A turn that ran the finder opens or joins the paid session under the
  finder's tool, as a finder photo and pick do today.
- Emptying their photo and drawing into it bill as today's room photo and
  render do (a design generation for the picture, `charge_picture`).

The reply says what ran (`presentation.finder` / `presentation.render`), and the
gateway charges from that - nothing is charged for an upload alone.

### D9 - The old doors stay for now

`/v1/furniture-finder/photos`, `/picks` and `/v1/room-photos` keep working
until the console and widget send photos through the chat, then are retired.
Browse Catalogue keeps its own room photo step.

## 4. Changes

### This service (`zory-agent`)

1. `ChatRequest.photo` on the chat and stream endpoints: prepared, room-checked
   and kept beside the session on the turn it arrives (D2).
2. A photo-only message goes to Sara; the brief says whether the photo can show
   anything (`photo_can_show`); her instructions say what to offer (D3).
3. `find_in_photo` (Sara): detection once per photo, pieces named by kind and
   place, the pick as today's finder turn, an outline tap as a fixed command
   (D5). Shopper credentials passed on from the chat request.
4. `visualize_room` draws D1's room: the cart's lines per room, the room around
   them, one room per turn; for a plain cart, the room and the products chosen
   (named by their place in the cart, never by id); their photo when the chat's
   photo was sent for it (D1, D6).
5. The `furniture_finder` field and its refusal (D7).
6. Sara's instructions: photos with words go straight to the tool; the finder
   and the cart picture.
7. Docs: CLAUDE.md (10, 17, 25), `docs/widget-display-contract.md`.

### The backend (`zory/backend`, `core/ai_agent/`)

Its chat proxy (`ai_agent_views.py`) builds the agent's request from an
allowlist (`PASS_THROUGH`) and refuses an empty message, and every photo today
goes through `photo.prepare` before it is sent on. So:

1. **The chat views take a photo**: the chat and stream views accept multipart
   as well as JSON (`MultiPartParser`, as `finder_views.py` does); `image` is
   read and checked as `finder_service.read_photo` does (size, then `prepare`),
   refused with the same `photo_refused` wording; an empty message is allowed
   when a photo is sent.
2. **Send it on**: `photo` (the prepared JPEG, base64) added to the payload -
   never passed through as the widget sent it.
3. **Bill the photo turn as a photo search**: a chat message with a photo opens
   or joins the paid session under the finder's tool, with the anonymous
   network cap, as a finder photo does today (`conversation.search_turn`,
   `ip_limit`) - the room check runs on it whatever is chosen next. Drawing
   into it still costs a design generation (`charge_picture`, D8).
4. **The shopper's widget session** on chat requests (`shopper_headers`), as
   the finder routes send it, so `find_in_photo` runs as the shopper.
5. **`furniture_finder`** on every chat request, worked out like
   `visualization` (D7).
6. The chat archive records a photo turn as it does a finder photo
   (`chat_archive.PHOTO_SEARCH`).

### The console and the widget

1. The chat's photo button sends the photo with the message, with or without
   words.
2. The finder's outline screen, when a reply carries it, sends an outline tap
   to the chat.

## 5. Tests

- D1: each kind of room drawn as it is now - design only; in the cart with a
  piece added after and one removed; a plain cart for one room; two rooms
  (asks the room, then the products); "all of them" and a few named; the room
  or products already named (nothing asked); a product with no room chosen for
  this one (recorded); too many pieces.
- D2: a photo with a message and on its own, in one request; an unusable photo;
  a room and a product shot each checked once and kept; a check that fails
  counts as not known; a later message uses the kept photo; a new photo
  replaces it.
- D3: a photo alone with a cart or a designed room - Sara asks, offering both;
  with neither - the finder runs, no question; each answer runs its tool.
- D4: photo + "find this bed" runs the finder on the bed with no question;
  photo + "put my cart here" draws into it.
- D5: one match, several (outlines and a question), none (what was found);
  "cheaper ones" after refines the finder's results.
- D7: each `furniture_finder` value; a missing field per setting.
- Live, local and on stage: each flow by typing and by tapping, Arabic and
  English, an anonymous visitor and a member; chat web-f2e496d82855's console
  case.

## 6. Order of work

1. D1, the cart picture, on its own - no new upload, and it fixes the console
   case.
2. D2-D5: the chat photo, the two choices and the finder as Sara's tool.
3. D6: their photo as their room through the chat.
4. Backend: shopper session on chat, `furniture_finder`, billing (D7, D8).
5. Console, then the widget's change; then retire the old doors (D9).

## 7. Open questions

1. **A usual room size per kind** for a cart picture with no size given: a
   setting per room kind (for example a living room 5 x 4 m), or ask the size
   first?
Settled (2026-11-16): a photo is room-checked when it arrives (D2), and "See my
cart in this room" is offered only for a room.
2. **Sara seeing the photo herself** (decided 2026-11-16: not now). She decides
   from the customer's words and the finder's detected pieces only; letting her
   model see the image is a later step.
