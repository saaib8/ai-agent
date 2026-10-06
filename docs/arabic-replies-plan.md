# Arabic replies - step 1 plan (draft for review)

Branch: `feat/arabic-replies` (from `main` @ fe3fb86). Status: plan only, no code.
Decided with the user (2026-10-05): one shared pipeline, business logic stays
English, only the presentation is Arabic; phased - this step is the replies and
questions the models write; cards, chips and labels stay English until a
reviewed strings catalog lands; behind a switch; Western digits.

---

## 1. Requirement

- A session whose customer writes Arabic gets its replies and the agent's own
  questions **written directly in Arabic** by the models - not translated.
- **The language is sticky:** set by the first message, kept until the customer
  explicitly asks to switch ("let's talk in English", "تكلم عربي"). An English
  word, a figure, a product name or a chip tap never flips it.
- **Arabizi counts as Arabic** ("abi kanaba", "3ayez kanaba b 3000 riyal").
- Decisions, query understanding, taxonomy, search, relaxation, rooms and the
  prompts' reasoning are unchanged and stay English.
- Western digits ("1,250 ريال") in Arabic replies; the number check accepts
  Arabic-Indic digits and Arabic separators too.
- Behind `customer_agent.arabic_replies`: on in local and stage, off in prod and
  test, an explicit setting always wins (the agent-loop policy).

Out of scope for this step: card questions/choices, chips, budget band labels,
room piece labels, `name_arabic` on cards, frontend RTL, Arabic `semantic_text`
for ranking, an Arabic name lookup.

## 2. Facts from the code (main)

| Place | Today |
|---|---|
| `app/prompts/customer_commerce/v1.py:755` | Decision prompt: "Reply in English." - its clarifications reach the customer as written (`DeterministicResponseKind.MODEL_CLARIFICATION`) |
| `app/prompts/customer_commerce/response_v1.py:784` | Writer prompt: "Reply in English." |
| `app/services/response_generator.py:93` `_ends_on_a_question` | Looks only for "?" - an Arabic "؟" ending gets an English question appended |
| `app/schemas/next_step.py` `QUESTIONS`, `ANY_NEXT_STEP` | The appended questions are English constants |
| `app/services/response_wording.py` `FALLBACK_WORDING` etc. | Fixed sentences for failures and fallbacks, English |
| `app/services/numeric_guard.py:51,60` | Tokens `\d[\d,.]*`; only the ASCII comma groups - "٣٬٠٠٠" reads as 3 and 0 |
| `app/core/numbers.py` | Rejects "٣٬٠٠٠", "٣٫٥" |
| `app/schemas/chat.py` `ChatRequest` | `session_id`, `store_id`, `message`, actions; `extra="forbid"` - no locale |
| `app/schemas/agent_state.py` `AgentStateV1` | `schema_version = "agent_state_v5"`; no language |
| `app/schemas/agent_turn.py` `DecisionInput` | message, conversation, state_view |

## 3. Design

### 3.1 The session language (state)

- `AgentStateV1.reply_language: ReplyLanguage | None = None` (`ReplyLanguage =
  "en" | "ar"`). None = not yet set. A default, so sessions stored before this
  load unchanged (confirm the state-versioning rule: bump to v6 only if the
  loader requires it).
- Set once, on the first typed message of a session, and changed only by an
  explicit request.

### 3.2 Who decides

Deterministic first, the model only for what needs language understanding:

1. **Arabic script in the message** (a share of Arabic letters above a
   threshold, after removing digits and punctuation) → `ar`. Code, no model.
2. **Arabizi** (Latin letters) cannot be read by script. The decision model
   already reads it; it gets one field, `customer_writes: "ar" | "en" | None`,
   set only when the message is in Arabic written in Latin letters (or to
   resolve a genuinely mixed message). Code uses it only when the session has
   no language yet.
3. **An explicit switch** ("English please", "كلمني بالعربي"): the decision gets
   `switch_reply_language: "ar" | "en" | None`. Code applies it at any turn.
4. **Storefront default:** optional `ChatRequest.locale: "ar" | "en" | None`,
   used only when nothing above decides (e.g. a first message "3000"). Not
   persisted over an explicit choice.

Precedence per turn: explicit switch > existing session language > script >
`customer_writes` > locale > English.

### 3.3 Telling the models

- Both prompts: "Reply in English." becomes the session's language, rendered
  from state (decision: in `DecisionInput`/state view; writer: on
  `ResponseGroundingView` as `reply_language`). Prompt versions bumped.
- Writer guidance for Arabic: write naturally (Gulf-friendly Modern Standard
  Arabic), Western digits, "ريال"/"SAR" for the currency, product names as given
  (English in this step) or described.
- The decision model's clarifications follow the same instruction.

### 3.4 Fixed text that is not written by a model

- `_ends_on_a_question`: accept "؟" as a question.
- The appended next-step questions (`QUESTIONS`, `ANY_NEXT_STEP`) and the
  fallback / failure sentences (`response_wording.py`): **translated in this
  step** (decided with the user 2026-10-05) - about 40 short sentences, kept in
  a versioned Arabic strings source beside the English, chosen by the session
  language, validated at startup so every English sentence has its Arabic, and
  reviewed by a native speaker before prod.

### 3.5 Numbers

- `numeric_guard`: tokens accept Arabic-Indic digits and the Arabic separators
  "٬" (thousands) and "٫" (decimal); normalise to Western before comparing.
- `app/core/numbers.py`: read "٣٬٠٠٠" and "٣٫٥" the same way. Ambiguity rules
  stay: anything unclear is refused, never guessed (CLAUDE.md 21.1).
- Writer instructed to use Western digits; the guard accepts both regardless.

### 3.6 Switch and observability

- `customer_agent.arabic_replies: bool`, defaulted by environment like the
  agent-loop features. Off: today's behaviour exactly (English replies).
- Log `reply_language` and how it was decided (`script`, `model`, `switch`,
  `locale`, `session`) per turn - never the message.
- `ChatResponse` returns the session language so the frontend can set `dir`.

## 4. Tests and evals

- Unit: script detection (Arabic, Latin, mixed, digits-only, emoji), the
  precedence table, stickiness across English words/figures/chip values, the
  explicit switch both ways, Arabizi via the model field, "؟" handling,
  separators in the guard and `numbers.py`, the switch off = unchanged,
  contract tests for new fields.
- Live evals (same suite): ~10 Arabic cases now - MSA, Gulf, Arabizi, a card
  answered in Arabic, a pick by ordinal, seating for 8, a room budget, a stock
  substitute, "English please" mid-conversation, a chip tap staying Arabic;
  checks that the reply is in Arabic script and the next step ends on "؟" or
  chips. Full suite on vs off for parity.

## 5. Risks

| Risk | Mitigation |
|---|---|
| Writer quality in Arabic (tone, salesmanship) | Native-speaker review of ~30 sample replies before turning it on in prod |
| Number check misfires on Arabic figures | 3.5, tested; writer told Western digits |
| Language flips unexpectedly | Sticky rule; only explicit switches change it; logged |
| Mixed Arabic reply + English cards/chips | Accepted for step 1, behind the switch |
| Latency/cost | No extra model call: two small fields on the existing decision schema |
| Prompt length | Language guidance kept to a few lines per prompt |

## 6. Effort

About 3-4 days including tests and live evals; plus native review of sample
replies (and of the ~40 fixed sentences if 3.4 is taken now).

---

## 7. Revision after review (2026-10-05)

Verdict: approve with changes. Folded in:

- **Where the language is settled:** in `CustomerTurnCoordinator.run()`, before
  the turn runs, a pure function decides from script (letters only; Arabic
  letters >= Latin and >= 2 -> `ar`; no letters -> undecided; mostly Latin with
  some Arabic -> left to the model) plus the explicit setting, and stamps
  `turn.state` - so screen actions, the redecide path and `_not_understood` keep
  it. The decision's `customer_writes` / `switch_reply_language` are applied
  after the decision. Both fields stay outside the action-payload validator.
- **Decision prompt:** the language applies to `clarification.question` only;
  `search_request`, `design_question`, `room_pieces` and every other field stay
  as today. Counter-examples so "3 seater", "L-shape" or an English style word
  are never read as Arabizi or a switch.
- **Prompts are built at startup:** two prebuilt instruction variants per
  prompt, chosen per turn by language. The writer reads the language from
  `result.state` inside `generate()`, which also covers `POST /v1/comparisons`
  and the design-handoff question. `reply_language` is on `DecisionInput`.
- **API:** `ChatRequest.locale: Literal["ar","en"] | None`, per turn, never
  stored; `ChatResponse.reply_language` (not on the writer's output schema).
- **State:** `reply_language` defaulted, version stays v5; nothing written while
  the switch is off. Rollback note: sessions written with the field cannot be
  read by the old code until they expire.
- **Fixed sentences:** all of `response_wording.py` plus next-step questions -
  about 55, in `dict[ReplyLanguage, ...]` tables with totality tests (every
  English sentence has its Arabic; Arabic digit-free).
- **Numbers:** guard tokens `\d[\d,.٫٬]*`, ٬ removed and ٫ -> "." when
  canonicalising (never the list comma "،"); `numbers.py` maps Unicode digits
  and ٬/٫ first, ambiguity rules unchanged; number words not added; figures the
  turn parsed are admitted; writer told prices and sizes in digits, never words.
- **Out of scope, stated:** non-chat routes (furniture finder, visualization,
  picks errors) and API errors - the frontend translates errors by `code`.
- **Tests/evals:** add `_not_understood` in Arabic, a screen action and a card
  comparison in an Arabic session, guard cases ١٢٬٥٠٠ / ٣٫٥, the precedence
  table, the decision keeping other fields English, prompt tests updated.
  `evals/conversations/run.py`: accept "؟", an `arabic_reply` check, fallback
  detected structurally, `mentions` able to take Arabic.
- **Effort:** 5-6 days with the sentences and eval changes, plus native review.

Decided with the user (2026-10-05):
- **Base stays `main`.** Merge conflicts with `feat/agent-loop` (prompts,
  coordinator, config, eval runner) are accepted and resolved when both land.
  Since `_agent_features_by_environment` is not on main, this branch adds its
  own environment default for `arabic_replies` (on in local/stage).
- **One-way implicit switch:** a substantial Arabic-script message (the same
  letter rule as 3.2) moves an English session to Arabic; Arabic to English
  still needs an explicit request.

## 8. Phase 1 as built (2026-10-05)

Deviations from sections 3 and 7, settled during phase 1 and its QA:

- **`writes_arabizi: bool` instead of `customer_writes: "ar" | "en" | None`.**
  Code already reads Arabic script; the model is needed only for Arabic in
  Latin letters, so one flag is enough. Consequences, accepted: a storefront
  `locale` of `ar` answers an English message in Arabic until the customer asks
  for English; a mostly-Latin message with one Arabic word stays English.
- **The model is asked exactly what it was asked before while the switch is
  off.** Both language fields are hidden from the response schema
  (`SkipJsonSchema`) and the prompt is unchanged (`customer_decision/v1`). On,
  `with_reply_language` shows them and a LANGUAGE section explains them with
  counter-examples (`customer_decision/v1+reply-language.1`). QA found that
  without that section an English follow-up in an Arabic session was read as a
  switch to English in 2 of 4 runs; with it, 48 of 48 language checks held.
- **Every field-by-field `AgentStateV1(...)` rebuild carries every field**, and
  a structural test enforces it: six rebuilds had silently dropped the new
  field.
- **Non-chat responses** (furniture finder, room visualization) return
  `reply_language: null`. The frontend should keep the session's last known
  direction rather than reset it on null.

## 9. Phase 2 as built (2026-10-05)

- **One writer prompt, two prebuilt versions.** `response_v1.build_instructions(language)`:
  English is exactly today's `INSTRUCTIONS`; Arabic swaps only the "Reply in
  English." line for the Arabic paragraph (MSA for Gulf readers, masculine
  form unless told otherwise, Western digits, prices as the chips show them,
  ريال, questions end with "؟"). Chosen per turn from `result.reply_language`,
  or the stored session language for the comparison pop-up; always English
  while the switch is off.
- **Decision model:** only `clarification.question` is written in the session
  language. When the customer writes Arabic or Arabizi, the decision always
  restates the request in English (`search_request`, `design_question`), so
  query understanding, ranking text and the design specialist keep reading
  English. Found by QA: Arabizi reached query understanding raw and caused a
  needless currency question; Arabic `semantic_text` was ranked against English
  product text.
- **Carried to phase 4 (QA finding):** counts the customer spelled out
  ("لستة أشخاص", and on main already "three seater") are not in the number
  check's allowance, so a reply quoting them as digits can fall back. Phase 4's
  "figures the turn parsed are admitted" must cover seats and budgets on the
  zero-result path, with tests.
- **Carried to phase 5:** the eval runner's question check accepts "؟".

## 10. Phase 3 as built (2026-10-05)

- **A message catalog instead of per-language tables.** `app/services/arabic_wording.py`
  holds the Arabic of every fixed sentence, keyed by the English sentence. The
  English tables (`response_wording.py`, `next_step.QUESTIONS`, `ANY_NEXT_STEP`)
  are unchanged and remain the source; the reply writer translates a fixed
  sentence where it uses one (`in_language`). A test scans those modules -
  every constant, any table shape - and fails if a sentence has no Arabic, if an
  Arabic entry has lost its English, or if a constant holds a shape the scan
  cannot read. Arabic entries are digit-free, contain no Latin letters, address
  the customer in the masculine, and ask a question exactly where the English
  does.
- **QA review folded in:** one meaning fix (alternatives that do not *work*
  with the room, not that do not *match* it), three nuance fixes (no promise of
  further refinements; "what the room needs", not "what you need"; priced on a
  different basis, not higher) and eight naturalness fixes. The " - " dashes are
  left for the native-speaker review.
- **Recorded for later (outside this step):**
  - `POST /v1/comparisons` raises four different refusals under one error
    code (`comparison_refused`), so a frontend translating by code cannot tell
    them apart. Give each its own code, or expose the reason.
  - On main too: a "show me more" / "not this one" screen action with no
    search on record answers "I wasn't able to run that search just now",
    which misdescribes what happened.

## 11. Phase 4 as built (2026-10-05)

- **The number check reads Arabic figures.** Tokens take any script's digits
  and Arabic's thousands (U+066C) and decimal (U+066B) separators; the Arabic
  comma (U+060C) is never part of a figure. `app/core/numbers.py` maps Arabic
  digits, separators and the percent sign (U+066A) to ASCII first, and its
  ambiguity rules then apply unchanged ("5.000" is refused in either script).
- **What the customer said in words may be said back in digits.** A new,
  narrow source for the number check, `CustomerTurnResult.stated_figures`:
  - a new search: the budget bounds and seat counts query understanding read
    from their words this turn (`stated_figures`);
  - a refinement: the absolute budget amounts and seat counts the decision
    read (`refinement_figures`) - never a relative price, whose bound is
    computed from a product's price.
  This turn's only, never the saved search (one room path stores a computed
  remaining budget there), and never a size ("2 m" said as "200 cm" is a
  conversion). It fixes replies quoting "3" for "three seater" or "6" for
  "لستة أشخاص" being refused and replaced by the fallback - on main too, in
  English.
- **Accepted risk (QA, low):** in an Arabic session the figures come from the
  decision's English restatement, so a model that broke "never add a detail
  they did not give" could restate a computed budget. The search already runs
  with whatever the restatement says; the new exposure is only the prose
  repeating it. Revisit if evals show it.
- **Fails safe (QA, informational):** a comma used as a thousands separator in
  Arabic ("3،000") reads as two numbers, so such a reply would be refused,
  never wrongly passed.

## 12. Phase 5 as built (2026-10-05)

- **The eval runner reads Arabic.** "؟" is a question mark in its dead-end and
  next-step checks; the "please say it another way" fallback is recognised in
  either language, read from the application's own tables; new checks
  `arabic_reply` (the response says `ar` and the prose is Arabic script),
  `english_reply`, and `arabic_text` for the comparison pop-up; a typed turn
  may carry the storefront `locale`. The checks have unit tests
  (`tests/unit/test_eval_runner_language.py`).
- **15 Arabic cases** (`ar_*` in `evals/conversations/cases.yaml`): MSA, Gulf
  and Arabizi searches; a card tapped and a card answered in words; a typed
  pick; seating for 8; a room's budget question; an English follow-up staying
  Arabic; "English please" switching and staying; a screen tap keeping the
  language; the comparison pop-up; a storefront locale; figures said in words
  on a zero result; an English session staying English. All 15 pass, 3 runs
  each.
- **Before production:** a native speaker reviews the Arabic catalog
  (`app/services/arabic_wording.py`) and about 30 sample replies; the " - "
  dashes and the dialect balance are theirs to settle. The switch stays off in
  prod until then.

## 13. Final QA (2026-10-05)

Whole-feature review. With the switch off, every model call was proved
byte-identical to main (decision and writer instructions, response schemas,
strict schemas, inputs). Full suite: 180/180 with the switch on, 165/165 with
it off (network-drop failures re-run). Fixed from the review:

- **No settings error echoes its input** (`hide_input_in_errors`): a
  half-configured Azure pair printed the key - and any missing setting already
  printed `llm.api_key` on main.
- **Blank `AZURE_OPENAI_*` values mean "not set"**, so `.env.example` copied
  as shipped starts on OpenAI.
- **A session that never settled a language is saved without the field**
  (`exclude_if`), so sessions written while the switch is off stay readable by
  main - section 7's "nothing written while the switch is off" now holds.
  Sessions that did settle a language still carry it; code without the field
  cannot read those until they expire.

Open for the user: after an explicit "English please", an Arabic message is
still answered in English (section 7: Arabic to English needs an explicit
request, and so does going back). Confirm or change.

## 14. Step 2a: chips, the question card and the piece picker (2026-10-06)

Everything tappable beside a reply is worded in the turn's language; product
cards, product names, the comparison table and the picks tray are not (a later
step). Which chips are offered, the keys a tap sends back and the action a chip
performs never depend on the language.

- **Chip text table** (`app/services/chip_wording.py`): every chip the
  application writes - next steps, room questions, the over-budget swap,
  seating shapes, "no thanks" - with its label and value in English and Arabic
  side by side, templates for the ones with figures, and the card's question
  titles, budget bands and button. English is exactly what it was. A tapped
  Arabic chip sends Arabic words, read by the decision model like typed Arabic.
- **Reviewed data gets Arabic names**, one `arabic:` section per registry,
  checked total at startup (`app/taxonomy/arabic.py`): colours and styles,
  companion kinds, room pieces, and the card's kinds, feels and nouns.
- **Where no Arabic product-type name exists yet** - "What goes with the sofa
  set", "Show me sectional sofas" - the Arabic chip names no type rather than
  inventing one.
- **The piece picker** still sends its labels as words; with Arabic replies on,
  the decision prompt's room list shows each piece's Arabic name beside its key
  (`customer_decision/v1+reply-language.4`).

**For the frontend team:**
- Chip and card labels arrive in the session's language; nothing to translate.
- `deriveQuickReplies` (frontend/src/lib/quickReplies.ts) invents chips by
  matching English words in the reply. It finds nothing in Arabic replies and
  should be switched off when `reply_language` is `ar` - the backend now sends
  chips on almost every turn.
- The piece picker's submit sentence ("I'd like these pieces: ...") is built
  by the frontend in English; better still, send piece keys rather than words.
- Fixed UI text ("Quick reply", "· up to 3", buttons, the start screen) and
  right-to-left layout remain the frontend's.
