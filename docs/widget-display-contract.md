# Showing products in the customer's language - contract for a widget

For: the store widget (front-end team) and the Zory backend gateway (`core/ai_agent/`, Ans).
Status: built in the ai-agent service (uncommitted on `multi-agent-response`, 2026-10-13;
room package and every remaining console word added 2026-10-14).

The ai-agent service now **words everything a client shows around products** in the
reply's language, so a widget shows it as it is - no translation, no language logic.
The language is decided by the service (`reply_language`): Arabic once the customer
writes Arabic (sticky), English otherwise, and always English while the service's
`customer_agent.arabic_replies` setting is off.

## 1. Every product carries `display`

On `POST /v1/chat` and the photo search's pick (`presentation.products[]`, `presentation.focus`,
`presentation.comparison.products[]`), the picks (`picks[]`, `liked[]` on chat and
`POST /v1/picks`) and Browse Catalogue (`GET /v1/catalog/products` → `items[]`):

```json
"display": {
  "name": "طقم أريكة زاوية قماش 4 مقاعد - رمادي فاتح",
  "kind": "طقم كنب",
  "color": "رمادي",
  "styles": ["عصري", "بسيط"],
  "seats": "4 مقاعد",
  "price": "6,950 ريال",
  "size": "الطول 139 · العرض 139 · الارتفاع 76 سم"
}
```

Show these instead of `name_english`, `main_color`, `styles`, the price and the
dimensions. `seats` and `size` are `null` when unknown - show nothing then. In
English the same block reads `"4-seat"`, `"6,950 SAR"`, `"L 139 · W 139 · H 76 cm"`.
The Arabic name is the store's `name_arabic` exactly as stored (a store that saved
English there shows English). Keep sending the **English values and ids** back in
actions, as today - `display` is for reading only.

## 2. Every reply carries `labels`

`ChatResponse.labels` - **every word a widget draws around the conversation**, in
the reply's language (about 120 strings; the full list with English and Arabic is
`app/services/card_labels.py`, or call `GET /v1/labels?language=ar`). Chat replies,
photo-search replies and room-picture replies all carry it. Grouped:

| Group | Labels |
|---|---|
| Product card | `select`, `selected`, `more_like_this`, `not_this_one`, `compare`, `view_product`, `like`, `liked`, `like_product`, `unlike_product`, `widened`, `your_pick`, `show_different_options` |
| Compare bar | `compare_same_kind`, `compare_limit`, `compare_title`, `compare_count`, `compare_need_two`, `compare_one_more`, `stop_comparing`, `clear` |
| Picks tray | `your_picks`, `goes_with`, `what_goes_with`, `what_goes_with_product`, `remove_pick`, `focused_pick` |
| Catalogue tile | `add`, `view`, `room_limit` |
| Room package (section 4) | `room_package`, `your_seating`, `seating_option`, `choose_this`, `not_this_option`, `more_options`, `status_*`, `locked`, `already_owned`, `swap`, `swap_piece`, `remove`, `remove_piece`, `add_to_room`, `room_in_cart`, `each`, `new_spend`, `budget_up_to`, `budget_under`, `within_budget`, `over_budget`, `total_mixed_units`, `total_nothing_priced`, `camera_view`, `view_corner`, `view_eye_level`, `view_isometric`, `view_top_down`, `visualize`, `upload_room_photo`, `place_in_my_room`, `change_photo` |
| Card of questions | `narrow_down`, `quick_taps`, `narrow_these_down`, `up_to`, `cancel`, `anything_else`, `remove_chip`, `choice_or` (between several chosen chips in the message it sends) |
| Comparison table | `comparison_heading`, `comparison_field`, `field_<row.field>` (one per comparison field: `field_price`, `field_main_color`, ...), `comparison_different`, `comparison_same`, `listed_dimensions`, `listed_caveat`, `not_listed`, `not_listed_title` |
| Reply chips | `quick_reply`, `goes_well_with_it`, `also_goes_well_with_it` |
| Room picture | `render_title`, `render_view`, `render_outdated`, `full_screen`, `open_full_screen`, `close`, `download`, `edit_selection`, `render_again`, `try_this_view`, `render_alt_furnished`, `render_alt_view`, `room_visualisation` - name the picture's camera with `view_<render.view>`, not the English `view_label` |
| The customer's own message after a tap | `say_like`, `say_not_this_one`, `say_what_goes_with`, `say_more_like`, `say_compare`, `show_different_options`, `say_without`, `say_take_option`, `say_not_option`, `say_more_seating`, `say_other_options`, `say_remove_piece`, `say_add_to_room`, `say_use_option`, `say_visualize_room`, `say_place_room`, `say_place_selection`, `say_visualize_selection`, `say_show_selection`, `say_show_selection_in_room`, `pieces_one`, `pieces_many` - send it as the `message` beside the structured action, as the console does |
| Lists | `list_separator`, `list_and` - "a, b and c" / "a، b وc" |
| Browse Catalogue | `catalog_title`, `close_catalogue`, `step_pick`, `step_room`, `try_again`, `loading_catalogue`, `back_to_products`, `nothing_picked`, `products_one`/`products_many`, `catalog_max_products`, `catalog_total`, `visualize_room`, `render_unavailable`, `waiting_for_reply`, `sort_*`, `search_*`, `clear_search`, `filter_*`, `all_categories`, `all_types`, `any_colour`, `any_style`, `min_price`, `max_price`, `min_short`, `max_short`, `clear_filters`, `no_matches`, `catalog_page`, `previous_page`, `next_page`, `size_error`, `room_*` (the room step's fields), `in_the_room`, `upload_room_photo_long`, `room_photo_note`, `fit_*` (the fit check), `quantity_of`, `one_fewer`, `one_more`, `at_most_per_product` |

Placeholders to fill: `{name}` (a product's `display.name`, a room piece's kind, a
chip's label), `{names}` (several names joined with `list_separator` and `list_and`),
`{count}`, `{max}`, `{price}` (a worded price from `display`), `{number}` (an option's
position), `{view}` (the camera's `view_*` name), `{pieces}` (`pieces_one` or
`pieces_many`), `{size}`, `{style}`, `{room}`, `{min}`, `{page}`, `{pages}`, and the fit check's `{covered}`,
`{floor}`, `{percent}`, `{aim}`, `{length}`. **An Arabic label may leave a
placeholder out** - the customer's Arabic message names no product, as the card beside
it already does - so fill whatever is there and ignore what is not. Use the **latest
reply's** labels for things outside a reply (tray, compare bar, catalogue).

Before the first reply: `GET /v1/labels?language=ar` (or `en`) returns the same object.

**Which assistant a reply is from:** `agent` is `sara` (shopping, and the greeting on
the first reply), `salman` (sales) or `nora` (interior design). `sara` was `sarah`
until 2026-10-16 - map `sara` to "Sara" / "سارة" and its photo, and treat `sarah` the
same while an old reply may still be on screen. The reply's own words introduce it
by that name. `faysal` (store support) is a fourth.

**Who worked on the turn (2026-10-21):** `workflow` is `{owner, handoffs}` -
every agent that worked on the turn, in order, ending with the one who replied
(`agent`). Two or more handoffs can be shown as "Sara → Salman".

**Live progress:** `POST /v1/chat/stream` takes the same body as `/v1/chat` and
answers with server-sent events: `event: progress` (`{"agent", "phase"}`, phase
one of `routing`, `working`, `searching`, `designing`, `updating_cart`,
`checking_store`, `writing`) while the agents work, then exactly one
`event: reply` whose data is the committed chat response, or one `event: error`
(`{"code", "message"}`). If the stream ends without a reply, re-read the session
(`GET /v1/cart`, or the next turn with its revision) - never resend the message.

## 3. Browse Catalogue takes `language`

`GET /v1/catalog/products?...&language=ar` → tiles' `display` in Arabic.
`GET /v1/catalog/facets?store_id=..&language=ar` → each category, sub-category,
colour and style option carries `label` (show it; send its `value` back as the
filter), and `studio.style_labels` maps each room style value to its name.
Pass the conversation's latest `reply_language`. The facets' `studio.room_types` labels
and `price.currency_label` ("ريال", for a total the widget adds up) are in that
language too.

## 4. A room package reads in the reply's language too

`presentation.room`, `presentation.seating_bundles[]` and `presentation.chosen_seating`
(the whole-room package and the seating combinations) carry the same kind of text:

```json
"items": [{ "...": "...", "display": {
  "name": "مرآة زجاجية بخزانة ليلية ذات Drawer 2 - مرآة فضية",
  "kind": "كومودينة",
  "seats": null,
  "price": "1,360 ريال",
  "line_total": "2,720 ريال"
}}],
"totals": { "...": "...", "display": { "new_spend": "10,570 ريال", "budget": "12,000 ريال" } }
```

`line_total` is `null` for a piece the customer already owns (show "—"). Build the
card's words from `labels`: the heading `room_package` (or `seating_option` /
`your_seating`), the badge `status_complete|partial|infeasible` from `status`,
`each` with `{price}` = `display.price`, `budget_up_to` (or `budget_under` when
`budget_max_exclusive`) with `{price}` = `totals.display.budget`,
`within_budget`/`over_budget` from `within_budget`, `total_mixed_units` /
`total_nothing_priced` from `total_unavailable`, and the camera views and buttons
(`view_*`, `visualize`, `upload_room_photo`, `place_in_my_room`, `change_photo`).
Keep sending the view's **value** (`corner`, `eye_level`, ...) and the piece's
position back, as today.

The pieces card (`presentation.piece_picker`) sends `submit_message` when its
button is tapped: fill `{pieces}` with the ticked pieces' `label`s joined by
`piece_separator` ("أريد هذه القطع: سرير، كومودينات، سجادة") and send it as the
customer's message. `submit_label` and `choose_for_me` are already worded.

Arabic lines that start with a number ("950 ريال للقطعة") need right-to-left
direction to read in order - render text with `dir="auto"` (or `rtl` in an Arabic
layout).

### 4.1 A room piece's options: "Use this", never "Add to cart"

When the cards are the alternatives for one piece of the room, the
presentation says so: `product_source: "room_options"` and `options_for_piece`
(the piece, by its card's place in the room package). Draw each card with one
action, the `swap` label ("Use this"): it sends `bundle_action`
`{"kind": "swap", "bundle_ordinal": options_for_piece, "alternative_ordinal": <the
card's place>}` with `say_use_option` as the message. Do not offer Select / Add to
cart or Like on these cards: the option replaces the piece in the room, and
`POST /v1/picks` refuses a tick on them (`pick_unavailable`, reason
`room_option`).

A room just designed is drawn only once the agent has checked it - a reply
may describe a room before its package appears on a later reply.

## 5. Furniture Finder runs as the shopper

`POST /v1/furniture-finder/photos` and `POST /v1/furniture-finder/picks` keep their
bodies and answers, but are now answered by **Zory's own finder** (the photo is
uploaded and detected with `process_furniture_finder`, a picked piece matched with
`get-recommendations`), **run as the shopper**. The gateway must forward the widget
session it already verified, on both calls:

| Header | Value |
|---|---|
| `X-Zory-Widget-Token` | the shopper's widget access token (member, or anonymous visitor) |
| `X-Zory-Widget-Key` | the widget key the token was issued for (`ai-agent`) |
| `X-Zory-Widget-Store-Id` | the store's public id (uuid, or Salla id) |

- **Billing:** unchanged - the photo is a search in the ai-agent session, as the
  gateway bills it today. The ai-agent marks the detection as an inner step
  (`step_type`), so Zory does **not** charge the finder again.
- **Without the headers**, the ai-agent refuses with `403 finder_access_refused` -
  unless it runs as local (`ZORY_ENVIRONMENT=local`) with
  `ZORY_FURNITURE_FINDER__LOCAL_WIDGET_KEY` set, when it signs in an anonymous
  Product Discovery visitor of its own instead of the shopper. `docker-compose.yml`
  sets `ZORY_ENVIRONMENT=local`, so keep `LOCAL_WIDGET_KEY` out of any server real
  shoppers reach.
- **A refused token is never retried** as anyone else: a forwarded token Zory
  rejects (expired, most often) is answered `403 finder_access_refused`, and the
  widget refreshes its session and tries again.
- **Errors** (our wording, Arabic for an Arabic session): `finder_access_refused`
  (403 - token missing, expired or not for this store), `detection_unavailable`
  (503), `visual_search_unavailable` (503), plus the existing `image_rejected` and
  `finder_image_not_found`.
- A malformed header is refused with 422 before anything runs.


## 6. The cart - "Add to cart" and the store's own cart

**Select now means Add to cart.** The picks tray is the cart (CLAUDE.md 36): the
card button (`select` = "Add to cart", `selected` = "In cart"), a typed "add the
7th one", a seating combination's button and a room package's "Add all to cart"
all put products in the same list, with how many of each. The ai-agent keeps the
cart in the conversation; the widget copies every change into the store's own cart.

**Where the cart comes from.** Every chat reply and every `POST /v1/picks` answer
carries `cart` (absent = unchanged, as for `picks`); `GET /v1/cart?session_id=&store_id=`
reads it without a turn (an unstarted session is an empty cart at revision 0).

```json
"cart": {
  "lines": [{"pick": 1, "quantity": 2, "name_english": "...", "image_url": "...",
             "unit_price": "3600.00", "price_unit": "SAR", "line_total": "7200.00",
             "kind": "armchair", "display": {"name": "...", "price": "3,600 SAR"},
             "line_total_display": "7,200 SAR",
             "product": {"uuid": "...", "salla_id": "1660240108", "url": "https://..."}}],
  "item_count": 3,
  "totals": [{"price_unit": "SAR", "amount": "11200.00", "display": "11,200 SAR"}],
  "unavailable": 0,
  "changes": [{"product": {"uuid": "...", "salla_id": "1660240108", "url": "..."},
               "name": "...", "quantity_before": 0, "quantity_after": 2}]
}
```

- Draw the tray from `cart.lines`: `display.name`, `display.price` (each),
  `line_total_display`, and a − / + stepper. Totals: `cart_total` ("Total {price}")
  with each `totals[].display` - one per currency, never added together. A line
  chosen for a designed room carries `room` ("Living room"): show it as a tag. A
  room package with `in_cart` is a record - tag it `room_in_cart` and draw no
  Swap, Remove or Add all.
- **The agent asks Salla first.** On a Salla store, before a product enters our cart
  (or its quantity rises) the agent checks Salla's storefront: a product that is not
  this store's, out of stock, or past one order's limit is never put in our cart -
  the reply says why, and a tap gets `409 pick_unavailable` with words to show
  ("That product can't be added to this store's cart.", "That product is out of
  stock.", "The store takes up to 3 of this product per order."). So Salla refusing
  at `addItem` is now rare (a colour or size skipped, stock that moved) - still undo it.
- **The cart holds 10 products** however they are added: a tap past it is refused
  (`pick_limit`); a typed add or an offer's yes gets the reply "Your cart is full -
  remove a product from it to add another." and changes nothing.
- **Changing it:** `POST /v1/picks` with `{"kind":"quantity","pick":N,"quantity":Q}`
  (1..`cart_max_quantity`, default 10 - else 422 `cart_quantity`), `{"kind":"deselect","pick":N}`
  to remove, `{"kind":"select","ordinal":N,"list_revision":R}` to add a card (cart
  full - default 10 products - is 422 `pick_limit`). After a new pick the widget
  opens the turn as before (`goes_with`), now with `say_added_to_cart` as the message.
- **"I like the fifth one" asks first.** The reply offers it - its price and what the
  cart would then come to - with two chips in `presentation.choices`: Yes
  (`offer_add`, carrying `product_action: {"kind":"add_to_cart","card":{...}}`) and
  Not now (`offer_not_now`, words only). Send a chip's `product_action` with its
  `value` as the message, as for any chip; the yes adds it and answers like a tap
  (the pick and what goes with it). `card` may be null: the piece in focus.
- **A piece the store refused offers similar ones.** When the store's cart would not
  take the one piece they asked for and the turn did nothing else, the reply ends on
  "Would you like to see similar ones instead?" with two chips: Show similar ones
  (carrying `product_action: {"kind":"more_like_this","card":{...}}`, or none - then
  the piece in focus) and Keep browsing (words only). Like the offer's Yes / Not now,
  draw them together beside the question - never under "also goes well with it".
- **Favourites are `liked` only.** `picks` are the cart now: draw them as "in cart",
  never as favourites.
- **Add all:** a room package's button is `add_all_to_cart` ("Add all to cart · {price}",
  `{price}` = the room's `totals.display.new_spend`) and sends a chat turn with
  `bundle_action: {"kind": "add_to_cart"}` and `say_add_all_to_cart` as the message -
  every piece being bought, with its quantity; pieces they already own stay out. A
  seating combination's button uses the same label with its own total and sends the
  existing `search_action` `combination` / `choose`.
- **Copying into the store's cart (Salla).** Only the storefront page can write the
  shopper's cart. Sync from the whole cart, not only from `changes`: a reply that
  never reached the page (Stop, a dropped connection, a closed tab) still changed our
  cart, and its `changes` are gone. Keep what you last put into Salla per product
  (`product.uuid` → quantity, in `sessionStorage`), and on every `cart` you receive
  make Salla match `cart.lines`; `changes` is then only a hint for messages. Each
  difference is handled like a change:
  1. quantity went up: `POST /api/widgets/cart/salla-item/` (Zory, widget session) with
     `{"product": change.product.uuid, "quantity": quantity_after - quantity_before}`
     (`"salla_product_id": change.product.salla_id` names it as well).
     It checks the product is this store's and in stock (409 `out_of_stock`), and
     returns `cart_item` plus any `options` the product needs.
     If `requires_options`, ask the shopper and fill `cart_item.options` as
     `{<option id>: <value id>}`. Then call `salla.cart.addItem(cart_item)`.
     Several changes at once ("Add all to cart") go in one call:
     `{"items": [{"product": ..., "quantity": ...}, ...]}` (up to 20). The answer has a
     result per item in the same order (`ok: true` with the fields above, or `ok: false`
     with `code`/`detail`/`detail_arabic`), `cart_items` - the ready ones, to hand
     `salla.cart.addItem` one by one - and `all_ready`. Take each `ok: false` item back
     out of our cart (step 3). The batch form is Zory commit `7c8f46a` ("mutiple-cart"),
     on `stage`; it reaches stageapi once that is on `release-branch`.
  2. quantity went down or to 0: update or remove that product's line in the Salla
     cart (Twilight's cart item calls, matched by `salla_id`).
  3. if Salla refuses (out of stock, a required option skipped), take it back out of
     our cart: `{"kind":"deselect","pick":N}` (or a `quantity` back down) - so the
     agent never believes in a line the store's cart does not have.
  A store that is not on Salla answers `cart_supported: false`: keep our cart as the
  shopper's list and link each line to `product.url`. `go_to_cart` labels a link to
  the store's own cart page (Salla: `/cart`), where checkout happens.
- The cart lives as long as the conversation (`ZORY_SESSION__TTL_S`); the store's
  own cart outlives it.

## 7. Gateway (Zory backend `core/ai_agent/`) - what to check

Read-only check on 2026-10-13 (branch `feat/agent-timed-sessions`):

| Path | Today | Needed |
|---|---|---|
| Chat (`ai_agent_views.py`) | returns `{**answer, ...}` - all new fields pass through | nothing |
| Picks (`picks_views.py`) | returns the answer as is | nothing |
| Photo search upload + pick (`finder_service`) | forwards only session, store and photo | forward the three `X-Zory-Widget-*` headers (section 5) - until then the ai-agent cannot act as the shopper: it answers `finder_access_refused` (which the gateway turns into `agent_unavailable`, refunded), or, on a server with `LOCAL_WIDGET_KEY`, runs as its own visitor instead |
| Room pictures (`render_service._render`) | returns `{**answer, ...}` | nothing |
| Catalogue products (`catalog_service.products`) | forwards only `CATALOG_FILTERS` | add `"language"` to `CATALOG_FILTERS` (`constants.py`) |
| Catalogue facets (`catalog_service.facets`) | forwards only `store_id` | forward `language` from the request |
| Labels | no route | optional: a proxy for `GET /v1/labels?language=` (or rely on chat replies' `labels`) |
| Cart read | no route | `GET /api/widgets/ai-agent/cart/?session_id=` → `GET /v1/cart` with the scoped session and the token's store (section 6). Cart changes need nothing: they go through picks and chat, which pass answers through as they are |

## 8. Product links carry Zory's click id

Every `product_url` the service returns now carries `zory_click_id=<the product's public
uuid>` - the same id, in the same field, as every other Zory product link (the main
backend's `Product.tracking_product_url`). The storefront snippet reads it and reports
views, add-to-carts and purchases to Zory's analytics (`POST /salla/analytics/events/`),
so a sale that started in the assistant is counted like any other.

Where: `presentation.products[]`, `presentation.focus`, every room package and seating
combination piece (`presentation.room.items[]`, `seating_bundles[].items[]`,
`chosen_seating.items[]`), a render's `presentation.render.items[]`, Browse Catalogue
`items[]`, and the cart's `lines[].product.url` / `changes[].product.url`.

- **A widget changes nothing to open it:** link to `product_url` (or `product.url`) as
  today. A page the catalog no longer has, or a reply whose lookup failed, comes back
  without the id.
- **Matching by page must drop the id first.** Zory's widget cart and favourites
  (`core/widget/widget_cart.py`, `widget_favorites.py`) look products up by the exact
  page: they strip `zory_click_id` before comparing (backend team). A client comparing
  links itself does the same (the console's room-vs-picture check does).

The id is attached once the reply is finished (`app/services/click_tracking.py`): inside
the service `product_url` stays the page as stored, no model sees the id, and nothing
ranked or grounded carries it.
