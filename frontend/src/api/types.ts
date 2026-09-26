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
}

export interface GroundedBundleTotals {
  new_spend_total: string | null
  currency: string | null
  total_unavailable: string | null
  budget_max_amount: string | null
  budget_currency: string | null
  budget_max_exclusive: boolean
  within_budget: boolean | null
}

export interface GroundedBundlePresentation {
  status: BundleStatus
  items: GroundedBundleItem[]
  totals: GroundedBundleTotals
}

export interface ChatPresentation {
  products: GroundedProduct[]
  comparison: ProductComparisonResult | null
  room: GroundedBundlePresentation | null
  /** A picture of the room package, when the turn made one. */
  render?: RoomRenderPresentation | null
  /** Composed seating combinations, when no single piece met the seat count.
   *  Each renders as its own package; empty when none fit the budget. */
  seating_bundles: GroundedBundlePresentation[]
  /** Ready answers to the question just asked, built by the backend from real
   *  options (e.g. the seating shapes in stock, with their from prices). */
  choices?: { label: string; value: string }[]
  /** A room's pieces as chips to tick, when the room question asks for them. */
  piece_picker?: PiecePickerData | null
}

export interface PiecePickerData {
  pieces: { label: string; selected: boolean; essential: boolean }[]
  submit_label: string
  choose_for_me: { label: string; value: string }
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
  image_url: string
  width: number
  height: number
  view: RenderView
  view_label: string
  /** Exactly the pieces the render was asked to show. */
  items: RoomRenderItem[]
  source?: RenderSource
  /** The room a catalogue render was set up with. */
  room?: RenderRoomSpec | null
}

export interface VisualizeRequest {
  session_id: string
  store_id: number
  view: RenderView
  expected_session_revision?: number
}

export interface CustomerResponse {
  message: string
  referenced_grounding_refs: number[]
  follow_up_question: string | null
}

export interface ChatResponse {
  session_id: string
  session_revision: number
  response: CustomerResponse
  presentation: ChatPresentation | null
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

export type BundleAction = BundleAlternativesAction | BundleSwapAction

export interface MoreOptionsAction {
  kind: 'more_options'
}

export interface ExcludeProductAction {
  kind: 'exclude'
  /** Position of the product to drop, among those on screen (1-based). */
  ordinal: number
}

export type SearchAction = MoreOptionsAction | ExcludeProductAction

export interface ChatRequest {
  session_id: string
  store_id: number
  message: string
  expected_session_revision?: number
  bundle_action?: BundleAction
  search_action?: SearchAction
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
}

export interface CategoryFacet extends Facet {
  subcategories: Facet[]
}

export interface StudioOptions {
  room_types: { value: RoomType; label: string }[]
  styles: string[]
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
  price: { currency: string; min_amount: string; max_amount: string } | null
  studio: StudioOptions
}

/** What a catalogue render was made from: kept by the client that sent it,
 *  because chat presentations never carry catalog ids. */
export interface CatalogSelection {
  items: { product_id: number; quantity: number }[]
  room: RenderRoomSpec
}

export interface CatalogVisualizeRequest {
  session_id: string
  store_id: number
  items: { product_id: number; quantity: number }[]
  room: RenderRoomSpec
  view: RenderView
  expected_session_revision?: number
}
