"""The words a client draws around products, in the reply's language.

A store's widget, like the console, shows these instead of its own English:
the buttons on a product card, the picks tray, the compare bar, a catalogue
tile, Browse Catalogue's screens, a room package, the card of questions, the
comparison table, a picture of the room, and the customer's own message after a
tap. Display only - a tap still
sends the same structured action, so a translated button can never change what
it does.

A label naming something carries a placeholder the client fills: `{name}` (a
product's display name, a room piece's kind or a chip's label), `{names}` (several
names joined with `list_separator` and `list_and`), `{count}` (how many), `{max}`
(a limit), `{price}` (a worded price from `display`), `{number}` (an option's
position), `{view}` (a camera view's `view_*` name), `{pieces}` (`pieces_one` or
`pieces_many`), `{size}`, `{style}` and `{room}` (a room's size, style and type),
`{min}`, `{page}`, `{pages}`, and the fit check's `{covered}`, `{floor}`,
`{percent}`, `{aim}` and `{length}`.
An Arabic label may leave out a placeholder its English has - the customer's own
Arabic message names no product, as the card beside it already does - but never
adds one.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class CardLabels(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # ── a product card ──────────────────────────────────────────────────────
    select: str
    selected: str
    more_like_this: str
    not_this_one: str
    compare: str
    view_product: str
    like: str
    liked: str
    like_product: str
    """"Like {name}", for a screen reader."""
    unlike_product: str
    widened: str
    """The badge on a card found only after the search was widened."""
    your_pick: str
    """The heading on the product just picked, above what goes with it."""
    show_different_options: str

    # ── comparing ───────────────────────────────────────────────────────────
    compare_same_kind: str
    """Why a card of another kind cannot be checked."""
    compare_limit: str
    """"Up to {max} products at a time - uncheck one first"."""
    compare_title: str
    compare_count: str
    """"Compare {count}"."""
    compare_need_two: str
    compare_one_more: str
    stop_comparing: str
    """"Stop comparing {name}"."""
    clear: str

    # ── the cart (the picks tray, CLAUDE.md 36) ─────────────────────────────
    your_picks: str
    """"Your cart ({count})"."""
    goes_with: str
    what_goes_with: str
    what_goes_with_product: str
    """"What goes with {name}"."""
    remove_pick: str
    """"Remove {name} from your cart"."""
    focused_pick: str
    cart_total: str
    """"Total {price}" - what the cart comes to, from `cart.totals`."""
    cart_empty: str
    quantity: str
    increase_quantity: str
    """"One more {name}", for a screen reader."""
    decrease_quantity: str
    """"One fewer {name}", for a screen reader."""
    quantity_limit: str
    """"Up to {max} of one product"."""
    add_all_to_cart: str
    """"Add all to cart · {price}" - a room package's or a seating
    combination's button, with what the pieces being bought come to."""
    go_to_cart: str
    """Opens the store's own cart, where checkout happens."""
    offer_add: str
    """The yes chip under "Shall I add it to your cart?"."""
    offer_not_now: str

    # ── a catalogue tile ────────────────────────────────────────────────────
    add: str
    view: str
    room_limit: str
    """"A room can show up to {max} products"."""

    # ── a room package ──────────────────────────────────────────────────────
    room_package: str
    your_seating: str
    """The heading on the seating combination the customer chose."""
    seating_option: str
    """"Seating option {number}"."""
    choose_this: str
    not_this_option: str
    more_options: str
    status_complete: str
    status_partial: str
    status_infeasible: str
    locked: str
    already_owned: str
    swap: str
    swap_piece: str
    """"Swap the {name}" - the piece's kind, for a screen reader."""
    each: str
    """"{price} ea" - one piece's price beside the line's."""
    new_spend: str
    budget_up_to: str
    """"Budget ≤ {price}"."""
    budget_under: str
    """"Budget < {price}" - a ceiling the total must stay below."""
    within_budget: str
    over_budget: str
    total_mixed_units: str
    """No total: the pieces are priced in more than one currency."""
    total_nothing_priced: str
    """No total: nothing in the room is being bought."""
    camera_view: str
    view_corner: str
    view_eye_level: str
    view_isometric: str
    view_top_down: str
    visualize: str
    upload_room_photo: str
    place_in_my_room: str
    change_photo: str

    # ── the card of questions ───────────────────────────────────────────────
    narrow_down: str
    quick_taps: str
    narrow_these_down: str
    up_to: str
    """"up to {count}" - how many choices a question takes."""
    cancel: str
    anything_else: str
    remove_chip: str
    """"Remove {name}" - a chip in the brief bar, for a screen reader."""
    choice_or: str
    """" or " - between a question's choices in the message the card sends."""

    # ── the comparison table ────────────────────────────────────────────────
    comparison_heading: str
    comparison_field: str
    field_price: str
    field_commerce_subcategory: str
    field_seating_capacity: str
    field_main_color: str
    field_styles: str
    field_length: str
    field_overall_width: str
    field_depth: str
    field_height: str
    """A row's name is `field_` + its `field`, for every comparison field."""
    comparison_different: str
    comparison_same: str
    listed_dimensions: str
    listed_caveat: str
    not_listed: str
    not_listed_title: str

    # ── reply chips ─────────────────────────────────────────────────────────
    quick_reply: str
    goes_well_with_it: str
    also_goes_well_with_it: str

    # ── a picture of the room ───────────────────────────────────────────────
    render_title: str
    render_view: str
    """"{view} view" - the camera the picture was taken from."""
    render_outdated: str
    full_screen: str
    open_full_screen: str
    close: str
    download: str
    edit_selection: str
    render_again: str
    try_this_view: str
    render_alt_furnished: str
    render_alt_view: str
    room_visualisation: str

    # ── the customer's own message after a tap ──────────────────────────────
    # What a button puts on the customer's side of the conversation and sends
    # as the message, beside the structured action the backend acts on.
    say_like: str
    say_not_this_one: str
    say_what_goes_with: str
    say_more_like: str
    say_compare: str
    say_without: str
    say_take_option: str
    say_added_to_cart: str
    """"I've added the {name} to my cart" - the message after "Add to cart"."""
    say_add_all_to_cart: str
    say_add_it_to_cart: str
    """The message the yes chip under an offer to add a piece records."""
    say_not_option: str
    say_more_seating: str
    say_other_options: str
    say_use_option: str
    say_visualize_room: str
    say_place_room: str
    say_place_selection: str
    say_visualize_selection: str
    say_show_selection: str
    say_show_selection_in_room: str
    pieces_one: str
    pieces_many: str

    # ── lists ───────────────────────────────────────────────────────────────
    list_separator: str
    list_and: str

    # ── Browse Catalogue ────────────────────────────────────────────────────
    catalog_title: str
    close_catalogue: str
    step_pick: str
    step_room: str
    try_again: str
    loading_catalogue: str
    back_to_products: str
    nothing_picked: str
    products_one: str
    products_many: str
    catalog_max_products: str
    catalog_total: str
    """"Total {price}" - the selection's sum with `price.currency_label`."""
    visualize_room: str
    render_unavailable: str
    waiting_for_reply: str
    sort_label: str
    sort_featured: str
    sort_price_asc: str
    sort_price_desc: str
    search_placeholder: str
    search_products: str
    clear_search: str
    filter_category: str
    filter_type: str
    filter_colour: str
    filter_style: str
    all_categories: str
    all_types: str
    any_colour: str
    any_style: str
    min_price: str
    max_price: str
    min_short: str
    max_short: str
    clear_filters: str
    no_matches: str
    catalog_page: str
    previous_page: str
    next_page: str
    size_error: str
    room_where: str
    room_where_aria: str
    room_setup: str
    room_my_photo: str
    room_kind: str
    room_kind_aria: str
    room_size: str
    room_size_optional: str
    room_length: str
    room_width: str
    room_view: str
    in_the_room: str
    upload_room_photo_long: str
    room_photo_note: str
    fit_enter_size: str
    fit_over: str
    fit_crowded: str
    fit_ok: str
    fit_coverage: str
    fit_aim: str
    fit_crowded_note: str
    fit_over_note: str
    fit_too_big: str
    fit_uncounted_one: str
    fit_uncounted_many: str
    fit_not_counted: str
    quantity_of: str
    one_fewer: str
    one_more: str
    at_most_per_product: str
