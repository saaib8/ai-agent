import type { AgentKey } from '../lib/agents'

// TypeScript mirror of the ZORY backend's public wire contract.
//
// These types match the customer-safe shapes the service returns — not its
// internal schemas. Note that every monetary / dimensional value arrives as a
// JSON *string*: Pydantic v2 serialises `Decimal` as a string, so we type them
// as `string` and parse only for display (see lib/format.ts).

export type DimensionStatus = 'normalised' | 'unknown_unit' | 'absent'

export interface NormalisedDimensions {
  length_cm: string | null
  width_cm: string | null
  height_cm: string | null
  unit: string | null
  status: DimensionStatus
}

export interface CommerceClassification {
  category: string | null
  subcategory: string | null
  seating_capacity: number | null
}

/** A product's card text, ready to show, in the reply's language - worded by
 *  the server, so a client (this console or a store's widget) decides nothing. */
/** A room piece's card text, ready to show, in the reply's language. */
export interface BundleItemDisplay {
  name: string
  kind: string | null
  seats: string | null
  /** One piece's price: "950 SAR" / "950 ريال". */
  price: string
  /** What the line adds to the bill; null for a piece already owned. */
  line_total: string | null
}

/** A room's totals, worded in the reply's language. */
export interface BundleTotalsDisplay {
  new_spend: string | null
  budget: string | null
}

export interface ProductDisplay {
  name: string
  kind: string | null
  color: string | null
  styles: string[]
  seats: string | null
  price: string | null
  size: string | null
}

/** Every word drawn around the conversation, in the reply's language - the
 *  server's, as a store's widget gets them. Placeholders (`{name}`, `{names}`,
 *  `{count}`, `{max}`, `{price}`, `{number}`, `{view}`, `{pieces}`, `{size}`,
 *  `{style}`, `{room}`) are filled by the client; an Arabic label may leave one
 *  out. */
export interface CardLabels {
  select: string
  selected: string
  more_like_this: string
  not_this_one: string
  compare: string
  view_product: string
  like: string
  liked: string
  like_product: string
  unlike_product: string
  widened: string
  your_pick: string
  show_different_options: string
  compare_same_kind: string
  compare_limit: string
  compare_title: string
  compare_count: string
  compare_need_two: string
  compare_one_more: string
  stop_comparing: string
  clear: string
  your_picks: string
  goes_with: string
  what_goes_with: string
  what_goes_with_product: string
  remove_pick: string
  focused_pick: string
  cart_total: string
  cart_empty: string
  quantity: string
  increase_quantity: string
  decrease_quantity: string
  quantity_limit: string
  add_all_to_cart: string
  go_to_cart: string
  offer_add: string
  offer_not_now: string
  add: string
  view: string
  room_limit: string
  room_package: string
  your_seating: string
  seating_option: string
  choose_this: string
  not_this_option: string
  more_options: string
  status_complete: string
  status_partial: string
  status_infeasible: string
  locked: string
  already_owned: string
  swap: string
  swap_piece: string
  remove: string
  remove_piece: string
  each: string
  new_spend: string
  budget_up_to: string
  budget_under: string
  within_budget: string
  over_budget: string
  total_mixed_units: string
  total_nothing_priced: string
  camera_view: string
  view_corner: string
  view_eye_level: string
  view_isometric: string
  view_top_down: string
  visualize: string
  upload_room_photo: string
  place_in_my_room: string
  change_photo: string
  narrow_down: string
  quick_taps: string
  narrow_these_down: string
  up_to: string
  cancel: string
  anything_else: string
  remove_chip: string
  choice_or: string
  comparison_heading: string
  comparison_field: string
  field_price: string
  field_commerce_subcategory: string
  field_seating_capacity: string
  field_main_color: string
  field_styles: string
  field_length: string
  field_overall_width: string
  field_depth: string
  field_height: string
  comparison_different: string
  comparison_same: string
  listed_dimensions: string
  listed_caveat: string
  not_listed: string
  not_listed_title: string
  quick_reply: string
  goes_well_with_it: string
  also_goes_well_with_it: string
  render_title: string
  render_view: string
  render_outdated: string
  full_screen: string
  open_full_screen: string
  close: string
  download: string
  edit_selection: string
  render_again: string
  try_this_view: string
  render_alt_furnished: string
  render_alt_view: string
  room_visualisation: string
  say_like: string
  say_not_this_one: string
  say_what_goes_with: string
  say_more_like: string
  say_compare: string
  say_without: string
  say_take_option: string
  say_added_to_cart: string
  say_add_all_to_cart: string
  say_add_it_to_cart: string
  say_not_option: string
  say_more_seating: string
  say_other_options: string
  say_remove_piece: string
  add_to_room: string
  say_add_to_room: string
  room_in_cart: string
  say_use_option: string
  say_visualize_room: string
  say_place_room: string
  say_place_selection: string
  say_visualize_selection: string
  say_show_selection: string
  say_show_selection_in_room: string
  pieces_one: string
  pieces_many: string
  list_separator: string
  list_and: string
  catalog_title: string
  close_catalogue: string
  step_pick: string
  step_room: string
  try_again: string
  loading_catalogue: string
  back_to_products: string
  nothing_picked: string
  products_one: string
  products_many: string
  catalog_max_products: string
  catalog_total: string
  visualize_room: string
  render_unavailable: string
  waiting_for_reply: string
  sort_label: string
  sort_featured: string
  sort_price_asc: string
  sort_price_desc: string
  search_placeholder: string
  search_products: string
  clear_search: string
  filter_category: string
  filter_type: string
  filter_colour: string
  filter_style: string
  all_categories: string
  all_types: string
  any_colour: string
  any_style: string
  min_price: string
  max_price: string
  min_short: string
  max_short: string
  clear_filters: string
  no_matches: string
  catalog_page: string
  previous_page: string
  next_page: string
  size_error: string
  room_where: string
  room_where_aria: string
  room_setup: string
  room_my_photo: string
  room_kind: string
  room_kind_aria: string
  room_size: string
  room_size_optional: string
  room_length: string
  room_width: string
  room_view: string
  in_the_room: string
  upload_room_photo_long: string
  room_photo_note: string
  fit_enter_size: string
  fit_over: string
  fit_crowded: string
  fit_ok: string
  fit_coverage: string
  fit_aim: string
  fit_crowded_note: string
  fit_over_note: string
  fit_too_big: string
  fit_uncounted_one: string
  fit_uncounted_many: string
  fit_not_counted: string
  quantity_of: string
  one_fewer: string
  one_more: string
  at_most_per_product: string
}

export interface GroundedProduct {
  grounding_ref: number
  presented_ordinal: number | null
  name_english: string
  price_amount: string
  price_unit: string
  image_url: string
  product_url: string
  commerce: CommerceClassification
  dimensions: NormalisedDimensions
  main_color: string | null
  styles: string[]
  /** 0 = matched the request exactly; >0 = surfaced only after widening. */
  relaxation_depth: number | null
  /** Its card text, ready to show in the reply's language. Display only:
   *  everything the console sends back uses the English values. */
  display?: ProductDisplay | null
}

export type ComparisonStatus = 'same' | 'different' | 'unknown'

export interface ComparisonCell {
  known: boolean
  value: string | null
}

export interface ComparisonRow {
  field: string
  cells: ComparisonCell[]
  status: ComparisonStatus
}

export interface ProductComparisonResult {
  products: GroundedProduct[]
  rows: ComparisonRow[]
}

// ── Comparing the checked cards in a pop-up ──────────────────────────────────

/** A card on screen: the result list it is on, and its position there. */
export interface CardRef {
  list_revision: number
  ordinal: number
}

/** Grouped types and the family they compare within. A type absent here
 *  compares only with itself. */
export interface CompareGroupsResponse {
  version: string
  groups: Record<string, string>
  /** How many products one comparison may cover. */
  max_products: number
}

export type BundleStatus = 'complete' | 'partial' | 'infeasible'
export type BundleAcquisition = 'to_buy' | 'already_owned'

export interface GroundedBundleItem {
  grounding_ref: number
  name_english: string
  image_url: string
  product_url: string
  commerce: CommerceClassification
  quantity: number
  acquisition: BundleAcquisition
  locked: boolean
  unit_price: string
  price_unit: string
  /** null when the line adds nothing to new spend (an already-owned piece). */
  new_spend_line_total: string | null
  /** The card's text in the reply's language - what the widget shows. */
  display?: BundleItemDisplay | null
}

export interface GroundedBundleTotals {
  new_spend_total: string | null
  currency: string | null
  total_unavailable: string | null
  budget_max_amount: string | null
  budget_currency: string | null
  budget_max_exclusive: boolean
  within_budget: boolean | null
  /** The totals worded in the reply's language. */
  display?: BundleTotalsDisplay | null
}

export interface GroundedBundlePresentation {
  status: BundleStatus
  items: GroundedBundleItem[]
  totals: GroundedBundleTotals
  /** The room went into their cart: a record of how it was designed, with no
   *  Swap, Remove or Add all. */
  in_cart?: boolean
}

export interface ChatPresentation {
  products: GroundedProduct[]
  /** Which kind of list `products` is. Only search results are the list that
   *  ticks and "Not this one" count into; picks and a single product are
   *  cards with no position there. */
  product_source?: 'search' | 'selection' | 'liked' | 'detail' | null
  /** Which result list these cards are, for ticks. Set on search results. */
  list_revision?: number | null
  /** The first card is the closest to what they described. */
  best_match?: boolean
  /** A card of questions for a product search: asked first, or folded beside
   *  results to narrow them. */
  brief?: ProductBrief | null
  /** Every option for the results on screen, opened showing what is used. */
  narrow_down?: ProductBrief | null
  /** What the search on screen is using, each removable. */
  brief_chips?: BriefChip[]
  /** The pick the customer just chose, drawn above what goes with it. */
  focus?: GroundedProduct | null
  comparison: ProductComparisonResult | null
  room: GroundedBundlePresentation | null
  /** A picture of the room package, when the turn made one. */
  render?: RoomRenderPresentation | null
  /** Composed seating combinations, when no single piece met the seat count.
   *  Each renders as its own package; empty when none fit the budget. */
  seating_bundles: GroundedBundlePresentation[]
  /** The seating combination the customer just chose, drawn as one package. */
  chosen_seating?: GroundedBundlePresentation | null
  /** Ready answers to the question just asked, built by the backend from real
   *  options (e.g. the seating shapes in stock, with their from prices). */
  choices?: ReplyChoice[]
  /** A room's pieces as chips to tick, when the room question asks for them. */
  piece_picker?: PiecePickerData | null
  /** The room piece a shown list of alternatives is for. Set when the customer
   *  asked for cheaper options for an over-budget swap, so selecting one sends a
   *  `swap` for that piece rather than picking a fresh product — which is what
   *  keeps a room edit from cross-selling a piece it already holds. */
  swap_context?: RoomSwapContext | null
  /** The finishing touch these cards are a page of, for the room still being
   *  designed: each card's button adds it to the room (`add_to_room`). */
  room_addition?: string | null
  /** The room on record is in their cart: every room package drawn before
   *  this reply is a record, no longer edited. */
  room_in_cart?: boolean
  /** The store's own support channels beneath a reply about the store. */
  support?: SupportPresentation | null
}

export type SupportChannel = 'whatsapp' | 'email'

/** One way to reach the store's support team, built by the server from the
 *  contacts the store's owner set - never by a model. */
export interface SupportChannelView {
  channel: SupportChannel
  /** The channel's name in the reply's language. */
  label: string
  /** `https://wa.me/<number>` or `mailto:<address>`. */
  url: string
  /** Whether the platform can send the shopper's request on this channel. */
  handover?: boolean
}

export interface SupportPresentation {
  channels: SupportChannelView[]
}

export interface RoomSwapContext {
  bundle_ordinal: number
  role: string
}

// ── The card of questions for a product search ───────────────────────────────

export type BriefQuestionKind =
  | 'type'
  | 'room'
  | 'people'
  | 'budget'
  | 'space'
  | 'colour'
  | 'feel'
  | 'style'

export interface BriefQuestion {
  kind: BriefQuestionKind
  /** A short title, or a sub-category's own question as a sentence. */
  label: string
  /** Which question on a sub-category's card, where several are feels. */
  id?: string | null
  /** Every choice leads to real products; `key` is what tapping sends back.
   *  `kind`, when set, is the chip's own kind on a question of another: the
   *  sheet's pattern words (feels) beside a colour question's colours. */
  choices: { key: string; label: string; kind?: BriefQuestionKind | null }[]
  /** One for a single answer; more where several may be ticked together. */
  max_choices: number
  /** Keys already true of the search on screen: Narrow down opens showing them. */
  selected?: string[]
}

/** One thing the search on screen is using; ✕ sends its facet back. */
export interface BriefChip {
  facet: string
  label: string
}

export interface ProductBrief {
  /** Which card this is in the session; its answers name it. */
  card: number
  /** `ask`: nothing searched yet. `narrow`: beside results, folded. */
  mode: 'ask' | 'narrow'
  noun: string
  questions: BriefQuestion[]
  submit_label: string
  /** The same button with nothing tapped, in the reply's language. */
  skip_label: string
}

export interface PiecePickerData {
  pieces: { label: string; selected: boolean; essential: boolean }[]
  submit_label: string
  choose_for_me: { label: string; value: string }
  /** The message the submit button sends, in the reply's language: the ticked
   *  pieces' labels fill `{pieces}`, joined by `piece_separator`. */
  submit_message?: string
  piece_separator?: string
}

// ── Room visualisation ───────────────────────────────────────────────────────

export type RenderView = 'corner' | 'eye_level' | 'isometric' | 'top_down'

export const RENDER_VIEWS: { value: RenderView; label: string }[] = [
  { value: 'corner', label: 'Corner' },
  { value: 'eye_level', label: 'Eye-level' },
  { value: 'isometric', label: 'Isometric' },
  { value: 'top_down', label: 'Top-down' },
]

export interface RoomRenderItem {
  name_english: string
  image_url: string
  product_url: string
  quantity: number
}

/** A package render pictures the conversation's room; a catalogue render,
 *  pieces the customer picked. Only a package render can go out of date. */
export type RenderSource = 'package' | 'catalog'

export type RoomType =
  | 'living_room'
  | 'bedroom'
  | 'dining_room'
  | 'home_office'
  | 'kids_room'
  | 'majlis'
  | 'entryway'

export interface RenderRoomSpec {
  room_type: RoomType
  /** An approved style value, e.g. "Modern_Classic". */
  style: string
  length_m: number
  width_m: number
}

export interface RoomRenderPresentation {
  /** A `data:image/jpeg;base64,` URL: renders are not stored anywhere, so the
   *  picture lives only in this reply and on screen. */
  image_url: string
  width: number
  height: number
  /** Null when drawn in the customer's own room: their photo is the camera. */
  view: RenderView | null
  view_label: string
  /** Exactly the pieces the render was asked to show. */
  items: RoomRenderItem[]
  source?: RenderSource
  /** The room a catalogue render was set up with. */
  room?: RenderRoomSpec | null
  /** The customer's room photo it was drawn in, so it can be drawn there again. */
  room_photo_id?: string | null
}

export interface VisualizeRequest {
  session_id: string
  store_id: number
  view: RenderView
  /** Draw it in the customer's own room photo instead; `view` is then unused. */
  room_photo_id?: string
  expected_session_revision?: number
}

/** A room photo the server checked, emptied and keeps for the session. */
export interface RoomPhotoResponse {
  room_photo_id: string
  width: number
  height: number
}

export interface CustomerResponse {
  message: string
  referenced_grounding_refs: number[]
  follow_up_question: string | null
  choices?: { label: string; value: string }[]
}

export interface ChatResponse {
  session_id: string
  session_revision: number
  response: CustomerResponse
  presentation: ChatPresentation | null
  /** The picks after this turn; absent or null means unchanged. */
  picks?: PickView[] | null
  /** Their liked list after this turn; null when unchanged or the buttons are off. */
  liked?: LikedView[] | null
  /** Their cart after this turn; absent or null means unchanged, as for picks. */
  cart?: CartView | null
  /** The language this turn was answered in; null where Arabic replies are off. */
  reply_language?: 'en' | 'ar' | null
  /** Which assistant the reply is shown as - a label the server decides. */
  agent?: AgentKey
  /** The words drawn around the products, in this reply's language. */
  labels?: CardLabels | null
}

export interface BundleAlternativesAction {
  kind: 'list_alternatives'
  /** Position of the room piece to show alternatives for (1-based). */
  bundle_ordinal: number
}

export interface BundleSwapAction {
  kind: 'swap'
  /** Position of the room piece being replaced (1-based). */
  bundle_ordinal: number
  /** Position of the chosen alternative among the products on screen (1-based). */
  alternative_ordinal: number
}

/** "I'm done with this room": the room in their cart is finished. */
export interface RoomDoneAction {
  kind: 'room_done'
}

/** A finishing touch's "Add to room": that card joins the room package. */
export interface AddToRoomAction {
  kind: 'add_to_room'
  /** The card's place in the list on screen (1-based). */
  ordinal: number
}

/** A room piece's Remove button: that piece leaves the room, exactly as typed
 *  "remove the second one" would take it out. */
export interface BundleRemoveAction {
  kind: 'remove'
  /** Position of the room piece to remove (1-based). */
  bundle_ordinal: number
}

/** The yes/no answers to an over-budget swap, each an action with no argument:
 *  they answer the held offer the server remembers, not a piece on screen. */
export interface SwapConfirmAction {
  kind: 'swap_confirm'
}
export interface SwapDeclineAction {
  kind: 'swap_decline'
}
export interface SwapAlternativesAction {
  kind: 'swap_alternatives'
}
export interface SwapDismissAction {
  kind: 'swap_dismiss'
}
/** A finishing touch for a finished room: the registry's piece key, or null for
 *  "you choose", which lets the designer pick. */
export interface AddPieceAction {
  kind: 'add_piece'
  piece: string | null
}
/** A room's "Add a finishing touch" chip: shows the pieces it could still take. */
export interface FinishingTouchesAction {
  kind: 'finishing_touches'
}
/** A room package's "Add all to cart": every piece being bought goes in the
 *  cart with its quantity. Names nothing - the room on record is the one on
 *  screen, which `expected_session_revision` makes sure of. */
export interface AddRoomToCartAction {
  kind: 'add_to_cart'
}

export type BundleAction =
  | BundleAlternativesAction
  | BundleSwapAction
  | BundleRemoveAction
  | AddToRoomAction
  | RoomDoneAction
  | SwapConfirmAction
  | SwapDeclineAction
  | SwapAlternativesAction
  | SwapDismissAction
  | AddPieceAction
  | FinishingTouchesAction
  | AddRoomToCartAction

export interface MoreOptionsAction {
  kind: 'more_options'
}

export interface ExcludeProductAction {
  kind: 'exclude'
  /** Position of the product to drop, among those on screen (1-based). */
  ordinal: number
}

/** The answers tapped on a card of questions, as the keys it offered. */
export interface BriefAnswerAction {
  kind: 'brief'
  card: number
  piece?: string | null
  room?: string | null
  people?: string | null
  budget?: string | null
  /** A chosen along-wall width ceiling: how wide a space the piece must fit. */
  space?: string | null
  colours?: string[]
  styles?: string[]
  feel?: string | null
  /** Every feel ticked on a sub-category's card - materials, features, shapes. */
  feels?: string[]
}

/** ✕ on a chip: the search on screen again, without that one thing. */
export interface DropFacetAction {
  kind: 'drop'
  facet: string
}

/** A tap on the seating combinations on screen: choose one, turn one down,
 *  or see more. `position` is the combination's place on screen (1-based). */
export interface CombinationAction {
  kind: 'combination'
  op: 'choose' | 'dismiss' | 'more'
  position?: number | null
}

/** A tapped answer to the taste question on screen (phase 5). */
export interface TasteAnswerAction {
  kind: 'taste'
  question: number
  answer: string
}

export type SearchAction =
  | MoreOptionsAction
  | ExcludeProductAction
  | BriefAnswerAction
  | DropFacetAction
  | CombinationAction
  | TasteAnswerAction

// ── Picks: ticked products, asking about one, comparing two ─────────────────

/** An action on the customer's picks. Picks are named by their position in
 *  the picks, never by a product id. */
export type ProductAction =
  | { kind: 'goes_with'; pick: number }
  | { kind: 'compare'; picks: [number, number] }
  | { kind: 'compare_cards'; cards: { list_revision: number; ordinal: number }[] }
  | { kind: 'companion'; category: string; subcategory: string }
  | { kind: 'more_like_this'; card: { list_revision: number; ordinal: number } }
  | { kind: 'more_like_this'; liked: number }
  /** "Yes, add it" under an offer to add a piece they liked: its card, or the
   *  piece in focus when the card is null. */
  | { kind: 'add_to_cart'; card?: { list_revision: number; ordinal: number } | null }

/** A ready answer to tap; when it carries an action, tapping runs it. */
export interface ReplyChoice {
  label: string
  value: string
  product_action?: ProductAction | null
  /** A room edit tapping it performs, for the yes/no on an over-budget swap. */
  bundle_action?: BundleAction | null
  /** An answer to the search's own question - a taste question's key. */
  search_action?: SearchAction | null
}

export interface PickView {
  /** Its position in the picks — the number the tray's actions use. */
  pick: number
  name_english: string
  image_url: string
  price_amount: string
  price_unit: string
  kind: string | null
  /** Its card number in the results on screen, when it is one of them. */
  presented_ordinal: number | null
  /** Every card showing it on the result lists still tickable. */
  positions?: { list_revision: number; ordinal: number }[]
  /** The product the conversation is about. */
  focused: boolean
  /** Its card text, ready to show in the reply's language. Display only:
   *  everything the console sends back uses the English values. */
  display?: ProductDisplay | null
}

/** One liked product, as the tray draws it. */
export interface LikedView {
  /** Its position in the liked list — the number the tray's actions use. */
  liked: number
  name_english: string
  image_url: string
  price_amount: string
  price_unit: string
  kind: string | null
  /** Every card showing it on the result lists still on screen. */
  positions?: { list_revision: number; ordinal: number }[]
  /** It is among their picks as well. */
  picked: boolean
  /** Its card text, ready to show in the reply's language. Display only:
   *  everything the console sends back uses the English values. */
  display?: ProductDisplay | null
}

export type PickAction =
  | { kind: 'select'; ordinal: number; list_revision?: number | null }
  | { kind: 'deselect'; pick: number }
  | { kind: 'like'; ordinal: number; list_revision: number }
  | { kind: 'unlike'; liked: number }
  | { kind: 'unlike'; ordinal: number; list_revision: number }
  | { kind: 'select_liked'; liked: number }
  /** How many of the pick at this place in the cart, from now on (at least
   *  one; removing it is `deselect`). */
  | { kind: 'quantity'; pick: number; quantity: number }

export interface PicksRequest {
  session_id: string
  store_id: number
  action: PickAction
  expected_session_revision?: number
}

export interface PicksResponse {
  session_id: string
  session_revision: number
  picks: PickView[]
  /** Their liked list; null when the buttons are switched off. */
  liked?: LikedView[] | null
  /** Set when this tick picked the first of its kind: show what goes with it. */
  goes_with?: number | null
  /** The cart after this action; null when it could not be read - the client
   *  keeps the cart it has. */
  cart?: CartView | null
}

// ── The cart: the picks with how many of each ───────────────────────────────

/** The product as the store knows it, for copying it into the store's own
 *  cart. Never sent back: requests name a line by its place (`pick`). */
export interface CartProductRef {
  uuid: string
  salla_id?: string | null
  url: string
}

/** One product in the cart and how many, priced fresh from the catalog.
 *  Amounts are Decimals: a JSON string (or number), parsed only for display. */
export interface CartLineView {
  /** Its place in the cart - the same number as its pick, which the picks
   *  actions (quantity, deselect, goes_with) name. */
  pick: number
  quantity: number
  name_english: string
  image_url: string
  unit_price: string | number
  price_unit: string
  /** `unit_price` times `quantity`. */
  line_total: string | number
  kind?: string | null
  product: CartProductRef
  /** Its card text, ready to show in the conversation's language. */
  display?: ProductDisplay | null
  /** `line_total` worded in the conversation's language: "7,200 SAR". */
  line_total_display?: string | null
  /** The room it was chosen for ("Living room"), in the reply's language. */
  room?: string | null
}

/** What the cart comes to in one currency; never converted. */
export interface CartTotal {
  price_unit: string
  amount: string | number
  /** `amount` worded in the conversation's language: "14,350 SAR". */
  display?: string | null
}

/** One product whose quantity changed this turn, for the store's own cart. */
export interface CartChange {
  product: CartProductRef
  name: string
  quantity_before: number
  quantity_after: number
}

export interface CartView {
  lines: CartLineView[]
  /** How many items, counting quantities: two armchairs and a sofa is 3. */
  item_count: number
  /** One total per currency; empty when the cart is. */
  totals: CartTotal[]
  /** Products the catalog no longer returns - left out of lines and totals. */
  unavailable: number
  /** What this turn changed; empty on a plain read. */
  changes: CartChange[]
}

/** `GET /v1/cart`: the cart without a turn of the conversation. */
export interface CartResponse {
  session_id: string
  /** 0 for a conversation that has not started. */
  session_revision: number
  cart: CartView
}

export interface ChatRequest {
  session_id: string
  store_id: number
  message: string
  expected_session_revision?: number
  bundle_action?: BundleAction
  search_action?: SearchAction
  product_action?: ProductAction
}

export interface ErrorBody {
  code: string
  message: string
  trace_id: string | null
}

export interface ErrorResponse {
  error: ErrorBody
}

export interface HealthResponse {
  status: string
  service: string
  environment: string
  dependencies: Record<string, string>
}

// ── Furniture Finder ─────────────────────────────────────────────────────────

/** A box in the *stored* photo's pixel space (see FinderPhotoResponse.width/height). */
export interface ImageBox {
  x1: number
  y1: number
  x2: number
  y2: number
}

export interface FinderObject {
  object_id: number
  /** The detector's visual class, e.g. "side-table". */
  label: string
  /** How a person says it, e.g. "side table". */
  display_label: string
  confidence: number
  box: ImageBox
  /** Outline points [x, y] in the same space as `box`. */
  polygon: [number, number][]
}

export interface FinderPhotoResponse {
  image_id: string
  /** The size the backend stored the photo at; outlines are in this space. */
  width: number
  height: number
  /** Only objects this retailer's catalog can match. */
  objects: FinderObject[]
  unmatched_count: number
}

export interface FinderPickRequest {
  session_id: string
  store_id: number
  image_id: string
  object_id: number
  expected_session_revision?: number
}

// ── Browse Catalogue ─────────────────────────────────────────────────────────

export type CatalogSort = 'featured' | 'price_asc' | 'price_desc'

export interface CatalogQuery {
  q?: string
  category?: string
  subcategory?: string
  color?: string
  style?: string
  min_price?: string
  max_price?: string
  currency?: string
  sort?: CatalogSort
  page?: number
  page_size?: number
  /** The conversation's language: the tiles read in it. */
  language?: 'en' | 'ar'
}

export interface CatalogItem {
  product_id: number
  name_english: string
  name_arabic: string
  price_amount: string
  price_unit: string
  image_url: string
  product_url: string
  commerce: CommerceClassification
  dimensions: NormalisedDimensions
  main_color: string | null
  styles: string[]
  /** Furniture that takes up floor; rugs, lighting and decor do not. */
  stands_on_floor: boolean
  /** Floor area in cm², for floor-standing pieces of known size only. */
  footprint_cm2: number | null
  longest_side_cm: number | null
  /** Its card text, ready to show in the reply's language. Display only:
   *  everything the console sends back uses the English values. */
  display?: ProductDisplay | null
}

export interface CatalogPage {
  items: CatalogItem[]
  page: number
  page_size: number
  total_pages: number
  count: number
}

export interface Facet {
  value: string
  count: number
  /** How it reads, in the requested language; `value` is what a filter sends. */
  label?: string | null
}

export interface CategoryFacet extends Facet {
  subcategories: Facet[]
}

export interface StudioOptions {
  room_types: { value: RoomType; label: string }[]
  styles: string[]
  /** Each style's name in the requested language, by its value. */
  style_labels?: Record<string, string> | null
  max_products: number
  max_quantity: number
  min_room_side_m: number
  max_room_side_m: number
  crowded_floor_ratio: number
  render_available: boolean
}

export interface CatalogFacets {
  categories: CategoryFacet[]
  colors: Facet[]
  styles: Facet[]
  price: {
    currency: string
    min_amount: string
    max_amount: string
    /** How the currency reads in the requested language ("SAR" / "ريال"). */
    currency_label?: string
  } | null
  studio: StudioOptions
}

/** What a catalogue render was made from: kept by the client that sent it,
 *  because chat presentations never carry catalog ids. */
/** Picked pieces, and where they go: a room set up, or the customer's own
 *  room photo - one of the two. */
export interface CatalogSelection {
  items: { product_id: number; quantity: number }[]
  room: RenderRoomSpec | null
  room_photo_id?: string | null
}

export interface CatalogVisualizeRequest {
  session_id: string
  store_id: number
  items: { product_id: number; quantity: number }[]
  room: RenderRoomSpec | null
  room_photo_id?: string | null
  view: RenderView
  expected_session_revision?: number
}
