# Room form: the room's questions asked together

Status: built 2026-11-19 (prompt agents/v2.117).

## The problem

When a shopper asks Nora to design a room, she asks one question per reply
before building anything: three in a bedroom, four in a dining room, up to five
in a living room (latest runs, 2026-11-18):

| Question | Living room | Bedroom | Dining room |
|---|---|---|---|
| Budget | yes | yes | yes |
| Room size | yes | yes | yes |
| Colours and look | yes | yes | yes |
| What the room is mostly for | yes | - | - |
| How many people | yes | - | yes |

Each one is a full turn (6-15 s on stage), and the shopper answers the same
kind of question again and again before seeing anything. Some runs also asked
the size twice, or "shall I design it now?" two or three times.

## What changes for the shopper

1. **The room is clear first.** "Design my room" with nothing to tell which
   room is one plain question first - "Which room shall we design?" - with the
   rooms this store can furnish as chips (`rooms_store_can_design`). A room
   that is already clear (they said it, or the piece in the cart belongs to
   one room) skips this.
2. **One small form: the three questions every room shares.**

   | Question | Choices | Free box |
   |---|---|---|
   | Your budget | fixed amounts, not ranges: 5,000 / 10,000 / 15,000 / 20,000 (in the store's currency) | a box beside them: "or type your amount" |
   | Room size | Small (about 3 x 3 m) / Medium (about 4 x 4 m) / Large (5 x 5 m or more) | two number boxes, length and width in metres, with up and down |
   | Colours | the main piece's stocked colours, several (up to 3) | none |
   | Style | the main piece's stocked styles, several (up to 2), plus "You decide" | "or describe the look" |

   One button: **Design it**. There is no "You decide everything" button.
   **The budget is required**; the size and the look may be left blank (the
   look keeps its own "You decide" chip). Tested on stage (2026-11-19): with no
   budget, Nora asked for it up to three times and then built with no limit
   (13,600-21,300 SAR rooms). A question already answered (they said
   the budget, gave the size, a bed in the cart sets the look) is left off the
   form; when all three are known, there is no form.
3. **Follow-ups, only where the room needs them: ordinary chat questions**,
   one at a time - Nora's reply with answer chips under it, as today, and the
   shopper taps a chip or types in the normal chat box. No form, no box of
   their own:

   | Room | Follow-ups, in order |
   |---|---|
   | Living room | 1. How many people usually sit there? (1-2 / 3-4 / 5-6 / 7+) 2. What is it mostly for? (Family & TV / Hosting / Reading & quiet / Children playing) |
   | Dining room | How many should it seat? (2 / 4 / 6 / 8 / 10+) |
   | Bedroom, office, others | none |

   Each is asked on its own, never combined. One already known (a sofa for 6
   in the cart, "for my family of five", "it's for hosting") is skipped.
4. **Then the room is built.** No "shall I design it now?".

So a bedroom is: form -> room. A dining room: form -> one follow-up -> room.
A living room: form -> people -> purpose -> room. Never more than the form plus
the room's own follow-ups, and anything already said is not asked again.

**Typing always works.** A shopper who ignores the form and types "8000, 4 by
5, warm beige" gets the same result; the form is a shortcut, never a gate.

## How it works

### Who decides what

- **Nora decides when.** A new tool, `ask_room_form`, which she calls when she
  starts a room and something on the form is still unknown. It takes no
  questions: she cannot add, remove or word them. The room's follow-ups are
  her ordinary questions with chips (`choices`), asked after the form.
- **Code decides what is on it** (`app/services/room_form.py`): the questions
  for that step, minus what is already known; the choices from the store's data
  in the reply's language; the labels.
  - Budget: fixed amounts, the same in every store and never worked out from
    its prices - a setting (`room_form.budget_amounts`, default 5,000 /
    10,000 / 15,000 / 20,000), shown in the store's currency. A tapped amount
    is the room's budget ceiling; a typed one is read by the number reader,
    and an amount without a currency is in the store's.
  - Looks from the shelf of the room's main kind (`store_shelf` styles and
    colours), the store's own values only (chips from trusted data).
- **Which follow-up a room has is reviewed domain data** in
  `room_pieces_v1.yaml`: `asks_seats` exists already (living room true,
  bedroom false); add `asks_purpose` (living room true) and a dining room
  entry with `asks_seats: true`. No room is special-cased in code.

### The answers

- **One tap for the whole form**: a new `room_form` action on the chat request
  (alongside `bundle_action` and the others): which form it answers (its
  number, so a stale form cannot answer a newer one), each question's chosen
  key, and each question's free text.
- **Chosen keys are read back through the form the session remembers**
  (`AgentStateV1.room_form`), like the old question card: a client can name
  a choice, never invent one.
- **Code saves what it can read exactly, before any model runs**: a budget
  chip, a size chip, a look chip, a people chip, a purpose chip, and a typed
  amount or "4 x 5" read by the one number reader (`app/core/numbers.py`).
  These go straight into the room record (budget, geometry, palette or styles,
  head count, purpose), as `note_room` would.
- **Free text that is not a plain figure goes to Nora** ("something cosy, a
  bit of green"), with what was saved. She interprets it as she does a typed
  message.
- Then the turn goes to Nora (the form's tap names `ask_room_form`'s owner,
  CLAUDE.md 17.3), who asks the room's next follow-up in chat, if one is left,
  or builds. Which follow-ups are left is in her brief (from the reviewed room
  data, minus what is known), so she never asks one already answered.

### What the screen shows

- A new `presentation.room_form` (the first step only): its number and its
  questions - each with a kind, a label, choices
  (key + label), whether several may be chosen (looks: up to 2), and a
  placeholder for the free box. Plus the two button labels.
- Drawn in the console by extending `BriefCard.tsx` (it already has chips, a
  free-text box and Submit/Skip); the free box becomes one per question.
- **The store widget has to draw it too**: added to
  docs/widget-display-contract.md, with the words in `labels`. Until the
  widget draws it, a widget that ignores the field still works: the reply asks
  the same questions in one message, and typed answers are read as today.

### The budget is required

Held in three places; only the last is a guarantee.

1. **The form**: **Design it** sends only with a budget chosen or typed;
   otherwise a short note under the budget ("Choose or type a budget"). The
   console card and the store widget each check it (widget display contract).
2. **The server**: a form answer with no budget is sent back - the same form,
   the budget marked; nothing is saved or built.
3. **`design_room`**: refuses to build a room without a budget
   (`budget_needed`), whether or not a form was used, so no room is ever built
   without a limit again. Nora asks for the budget **once**, in one line, and
   never asks again in other words; until they give one, she says she will
   design it as soon as they do.

## Nora's instructions

- Replace "asking first, one question a turn" for rooms with: make the room
  clear, then `ask_room_form` for the room, then its follow-up if any, then
  build. Never ask a question the form asks, never "shall I design it now?".
- The reply beside a form is one short line ("A few quick things so I can
  design it around you"), with no question of its own.
- After the form, at most one more question, and only for something the room
  cannot be designed without.

## Decisions for review

1. ~~Budget options~~ - decided: fixed amounts (not ranges, not from the
   store's prices), with a box beside them to type an amount.
2. ~~Living room follow-up as one form or two questions~~ - decided: two
   separate questions, people first, then purpose.
3. ~~Bedroom "for one or two?"~~ - decided: not asked; that question was
   removed and stays removed. The bedroom has no follow-up.
4. **Request field name**: a new top-level `room_form_action`, or a new kind
   on `bundle_action`. Proposed: new top-level, since no room exists yet when
   it is answered.

## Tests

- Unit: the form's questions per room and what is known; a typed budget read
  as an amount; reading chips and free text back (a key the form did not
  offer is refused); typed amounts and sizes through the number reader;
  the follow-up from `room_pieces_v1.yaml`; a stale form number refused.
- Tool rules: `ask_room_form` is Nora's alone; its tap routes to her;
  `design_room` without a budget is refused (`budget_needed`); a form answer
  without a budget is sent back.
- Conversation runs (stage): living, bedroom and dining rooms in four stores,
  answering by form and by typing - count turns
  and time to the room, and check nothing already said is asked again.

## Out of scope

- Search questions (Sara's) stay as they are.
- The room's design rules, budget handling and product choice are unchanged.
