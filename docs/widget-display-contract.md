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
| Room package (section 4) | `room_package`, `your_seating`, `seating_option`, `choose_this`, `not_this_option`, `more_options`, `status_*`, `locked`, `already_owned`, `swap`, `swap_piece`, `each`, `new_spend`, `budget_up_to`, `budget_under`, `within_budget`, `over_budget`, `total_mixed_units`, `total_nothing_priced`, `camera_view`, `view_corner`, `view_eye_level`, `view_isometric`, `view_top_down`, `visualize`, `upload_room_photo`, `place_in_my_room`, `change_photo` |
| Card of questions | `narrow_down`, `quick_taps`, `narrow_these_down`, `up_to`, `cancel`, `anything_else`, `remove_chip`, `choice_or` (between several chosen chips in the message it sends) |
| Comparison table | `comparison_heading`, `comparison_field`, `field_<row.field>` (one per comparison field: `field_price`, `field_main_color`, ...), `comparison_different`, `comparison_same`, `listed_dimensions`, `listed_caveat`, `not_listed`, `not_listed_title` |
| Reply chips | `quick_reply`, `goes_well_with_it`, `also_goes_well_with_it` |
| Room picture | `render_title`, `render_view`, `render_outdated`, `full_screen`, `open_full_screen`, `close`, `download`, `edit_selection`, `render_again`, `try_this_view`, `render_alt_furnished`, `render_alt_view`, `room_visualisation` - name the picture's camera with `view_<render.view>`, not the English `view_label` |
| The customer's own message after a tap | `say_like`, `say_not_this_one`, `say_what_goes_with`, `say_more_like`, `say_compare`, `show_different_options`, `say_without`, `say_take_option`, `say_not_option`, `say_more_seating`, `say_other_options`, `say_use_option`, `say_visualize_room`, `say_place_room`, `say_place_selection`, `say_visualize_selection`, `say_show_selection`, `say_show_selection_in_room`, `pieces_one`, `pieces_many` - send it as the `message` beside the structured action, as the console does |
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

## 5. Gateway (Zory backend `core/ai_agent/`) - what to check

Read-only check on 2026-10-13 (branch `feat/agent-timed-sessions`):

| Path | Today | Needed |
|---|---|---|
| Chat (`ai_agent_views.py`) | returns `{**answer, ...}` - all new fields pass through | nothing |
| Picks (`picks_views.py`) | returns the answer as is | nothing |
| Photo search pick (`finder_service.pick_object`) | returns `{**answer, ...}` | nothing |
| Room pictures (`render_service._render`) | returns `{**answer, ...}` | nothing |
| Catalogue products (`catalog_service.products`) | forwards only `CATALOG_FILTERS` | add `"language"` to `CATALOG_FILTERS` (`constants.py`) |
| Catalogue facets (`catalog_service.facets`) | forwards only `store_id` | forward `language` from the request |
| Labels | no route | optional: a proxy for `GET /v1/labels?language=` (or rely on chat replies' `labels`) |
