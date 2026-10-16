// Every word the console draws around the conversation - a product card, the
// picks tray, the compare bar, a catalogue tile, a room package, the card of
// questions, the comparison table, a room picture, and the customer's own
// message after a tap - is the server's (`ChatResponse.labels`), in the
// reply's language, exactly as a store's widget receives them. The console
// keeps no copy of its own beyond English for a server that sent none.
//
// Display only: every tap still sends the same structured action, so a
// translated button can never change what it does.

import type { BundleStatus, CardLabels, RenderView } from '../api/types'
import type { Turn } from '../hooks/useChat'

/** What the server sends when it sends nothing else: English. */
export const ENGLISH: CardLabels = {
  select: 'Add to cart',
  selected: 'In cart',
  more_like_this: 'More like this',
  not_this_one: 'Not this one',
  compare: 'Compare',
  view_product: 'View product →',
  like: 'Like',
  liked: 'Liked',
  like_product: 'Like {name}',
  unlike_product: 'Unlike {name}',
  widened: 'widened',
  your_pick: 'Your pick',
  show_different_options: 'Show me different options',
  compare_same_kind: 'Compare it with products of the same kind',
  compare_limit: 'Up to {max} products at a time - uncheck one first',
  compare_title: 'Compare',
  compare_count: 'Compare {count}',
  compare_need_two: 'Check at least two products of the same kind',
  compare_one_more: 'Check one more of the same kind',
  stop_comparing: 'Stop comparing {name}',
  clear: 'Clear',
  your_picks: 'Your cart ({count})',
  goes_with: 'Goes with',
  what_goes_with: 'What goes with it',
  what_goes_with_product: 'What goes with {name}',
  remove_pick: 'Remove {name} from your cart',
  focused_pick: 'The pick "it" refers to',
  cart_total: 'Total {price}',
  cart_empty: 'Your cart is empty',
  quantity: 'Qty',
  increase_quantity: 'One more {name}',
  decrease_quantity: 'One fewer {name}',
  quantity_limit: 'Up to {max} of one product',
  add_all_to_cart: 'Add all to cart · {price}',
  go_to_cart: 'Go to cart',
  offer_add: 'Yes, add it',
  offer_not_now: 'Not now',
  add: 'Add',
  view: 'View',
  room_limit: 'A room can show up to {max} products',
  room_package: 'Room package',
  your_seating: 'Your seating',
  seating_option: 'Seating option {number}',
  choose_this: 'Choose this',
  not_this_option: 'Not this one',
  more_options: 'Show more options',
  status_complete: 'Complete',
  status_partial: 'Partial',
  status_infeasible: 'Infeasible',
  locked: 'locked',
  already_owned: 'already owned',
  swap: 'Swap',
  swap_piece: 'Swap the {name}',
  each: '{price} ea',
  new_spend: 'New spend',
  budget_up_to: 'Budget ≤ {price}',
  budget_under: 'Budget < {price}',
  within_budget: 'within budget',
  over_budget: 'over budget',
  total_mixed_units: 'Total unavailable: mixed price units',
  total_nothing_priced: 'Total unavailable: no priced lines',
  camera_view: 'Camera view',
  view_corner: 'Corner',
  view_eye_level: 'Eye-level',
  view_isometric: 'Isometric',
  view_top_down: 'Top-down',
  visualize: 'Visualize',
  upload_room_photo: 'Upload room photo',
  place_in_my_room: 'Place in my room',
  change_photo: 'Change photo',
  narrow_down: 'Narrow down',
  quick_taps: 'A few quick taps - skip any',
  narrow_these_down: 'Narrow these down',
  up_to: 'up to {count}',
  cancel: 'Cancel',
  anything_else: 'Anything else? A colour, a size, a fabric…',
  remove_chip: 'Remove {name}',
  choice_or: ' or ',
  comparison_heading: 'Comparison',
  comparison_field: 'Field',
  field_price: 'Price',
  field_commerce_subcategory: 'Commerce subcategory',
  field_seating_capacity: 'Seating capacity',
  field_main_color: 'Main color',
  field_styles: 'Styles',
  field_length: 'Length',
  field_overall_width: 'Overall width',
  field_depth: 'Depth',
  field_height: 'Height',
  comparison_different: 'different',
  comparison_same: 'same',
  listed_dimensions: 'Listed dimensions',
  listed_caveat: 'Shown as listed; length and width may be recorded in either order.',
  not_listed: 'Not listed',
  not_listed_title: 'Not listed for this product',
  quick_reply: 'Quick reply',
  goes_well_with_it: 'Goes well with it',
  also_goes_well_with_it: 'Also goes well with it',
  render_title: 'Your room',
  render_view: '{view} view',
  render_outdated: 'Package changed since this render.',
  full_screen: 'Full screen',
  open_full_screen: 'Open full screen',
  close: 'Close',
  download: 'Download',
  edit_selection: 'Edit selection',
  render_again: 'Render again',
  try_this_view: 'Try this view',
  render_alt_furnished: 'Your room, furnished',
  render_alt_view: 'Your room ({view} view)',
  room_visualisation: 'Room visualisation',
  say_like: 'I like the {name}',
  say_not_this_one: 'Not this one — the {name}',
  say_what_goes_with: 'What goes with the {name}?',
  say_more_like: 'More like the {name}',
  say_compare: 'Compare these products: {names}',
  say_without: 'Without {name}',
  say_take_option: "I'll take option {number}",
  say_added_to_cart: "I've added the {name} to my cart",
  say_add_all_to_cart: 'Add all of these to my cart',
  say_add_it_to_cart: 'Yes, add it to my cart',
  say_not_option: 'Not option {number}',
  say_more_seating: 'Show me more seating options',
  say_other_options: 'Show me other {name} options',
  say_use_option: 'Use option {number} for the {name}',
  say_visualize_room: 'Visualize my room — {view} view',
  say_place_room: 'Place my room package in my room',
  say_place_selection: 'Place my selection in my room — {pieces}',
  say_visualize_selection: 'Visualize my selection — {pieces} in a {size} m {style} {room}, {view} view',
  say_show_selection: 'Show my selection — {view} view',
  say_show_selection_in_room: 'Show my selection in my room again',
  pieces_one: '1 piece',
  pieces_many: '{count} pieces',
  list_separator: ', ',
  list_and: ' and ',
  catalog_title: 'Browse catalogue',
  close_catalogue: 'Close catalogue',
  step_pick: 'Pick products',
  step_room: 'Set up the room',
  try_again: 'Try again',
  loading_catalogue: 'Loading catalogue…',
  back_to_products: 'Products',
  nothing_picked: 'Nothing picked yet',
  products_one: '1 product',
  products_many: '{count} products',
  catalog_max_products: '(up to {max})',
  catalog_total: 'Total {price}',
  visualize_room: 'Visualize room',
  render_unavailable: 'Room visualisation is not configured',
  waiting_for_reply: 'Waiting for the current reply',
  sort_label: 'Sort',
  sort_featured: 'Featured',
  sort_price_asc: 'Price: low to high',
  sort_price_desc: 'Price: high to low',
  search_placeholder: 'Search by name…',
  search_products: 'Search products',
  clear_search: 'Clear search',
  filter_category: 'Category',
  filter_type: 'Type',
  filter_colour: 'Colour',
  filter_style: 'Style',
  all_categories: 'All categories',
  all_types: 'All types',
  any_colour: 'Any colour',
  any_style: 'Any style',
  min_price: 'Minimum price',
  max_price: 'Maximum price',
  min_short: 'Min',
  max_short: 'Max',
  clear_filters: 'Clear filters',
  no_matches: 'No products match these filters.',
  catalog_page: '{count} products · page {page} of {pages}',
  previous_page: 'Previous page',
  next_page: 'Next page',
  size_error: 'Enter sides between {min} and {max} m.',
  room_where: 'Where',
  room_where_aria: 'Where to place the pieces',
  room_setup: 'Set up a room',
  room_my_photo: 'My room photo',
  room_kind: 'Room',
  room_kind_aria: 'Room type',
  room_size: 'Size (metres)',
  room_size_optional: 'Size (metres) - optional, for the fit check',
  room_length: 'Room length in metres',
  room_width: 'Room width in metres',
  room_view: 'View',
  in_the_room: 'In the room',
  upload_room_photo_long: 'Upload a photo of your room',
  room_photo_note: 'We clear out its furniture and decor first, then place your pieces in it.',
  fit_enter_size: 'Enter the room size to check the pieces fit.',
  fit_over: 'Too much for this room',
  fit_crowded: 'This will feel crowded',
  fit_ok: 'Fits comfortably',
  fit_coverage: '{covered} of {floor} m² · {percent}%',
  fit_aim: 'Aim for under {percent}%',
  fit_crowded_note: 'Furniture covers {percent}% of the floor. Aim for under {aim}% so there\'s room to walk.',
  fit_over_note: 'These pieces need more floor than the room has.',
  fit_too_big: '{name} ({length} m) won\'t fit this room.',
  fit_uncounted_one: '1 piece has no recorded size, so it isn\'t counted.',
  fit_uncounted_many: '{count} pieces have no recorded size, so they aren\'t counted.',
  fit_not_counted: 'Rugs, lighting and decor aren\'t counted.',
  quantity_of: 'Quantity of {name}',
  one_fewer: 'One fewer {name}',
  one_more: 'One more {name}',
  at_most_per_product: 'At most {max} of one product',
}

export interface CardWords {
  select: string
  selected: string
  moreLikeThis: string
  notThisOne: string
  compare: string
  viewProduct: string
  like: string
  liked: string
  likeName: (name: string) => string
  unlikeName: (name: string) => string
  widened: string
  yourPick: string
  differentOptions: string
  compareHintKind: string
  compareHintMax: (max: number) => string
  tray: {
    yourPicks: (count: number) => string
    goesWith: string
    goesWithTitle: string
    goesWithName: (name: string) => string
    removeName: (name: string) => string
    focused: string
    /** One line per currency: "Total 14,350 SAR". */
    total: (price: string) => string
    empty: string
    quantity: string
    increaseName: (name: string) => string
    decreaseName: (name: string) => string
    quantityLimit: (max: number) => string
    goToCart: string
  }
  compareBar: {
    title: string
    clear: string
    compare: (count: number | null) => string
    needTwo: string
    oneMore: string
    stopName: (name: string) => string
  }
  tile: {
    add: string
    view: string
    roomLimit: (max: number) => string
  }
  room: {
    title: string
    yourSeating: string
    seatingOption: (position: number) => string
    chooseThis: string
    /** "Add all to cart · 7,200 SAR"; without a price, just "Add all to cart". */
    addAllToCart: (price: string | null) => string
    notThisOption: string
    moreOptions: string
    status: Record<BundleStatus, string>
    locked: string
    alreadyOwned: string
    swap: string
    swapPiece: (kind: string) => string
    each: (price: string) => string
    newSpend: string
    budget: (price: string, exclusive: boolean) => string
    withinBudget: string
    overBudget: string
    totalUnavailable: (reason: string) => string
    cameraView: string
    views: Record<RenderView, string>
    visualize: string
    uploadRoomPhoto: string
    placeInMyRoom: string
    changePhoto: string
  }
  brief: {
    narrowDown: string
    quickTaps: string
    narrowTheseDown: string
    upTo: (count: number) => string
    cancel: string
    anythingElse: string
    remove: (label: string) => string
    or: string
  }
  comparison: {
    title: string
    field: string
    fieldName: (field: string) => string | null
    status: Record<string, string>
    viewProduct: string
    listedDimensions: string
    listedCaveat: string
    notListed: string
    notListedTitle: string
  }
  chips: {
    quickReply: string
    goesWellWithIt: string
    alsoGoesWellWithIt: string
  }
  picture: {
    title: string
    view: (view: string) => string
    outdated: string
    fullScreen: string
    openFullScreen: string
    close: string
    download: string
    editSelection: string
    renderAgain: string
    tryThisView: string
    altFurnished: string
    altView: (view: string) => string
    roomVisualisation: string
  }
  /** What a tapped button puts on the customer's side and sends as the message. */
  say: {
    like: (name: string) => string
    addedToCart: (name: string) => string
    addAllToCart: string
    notThisOne: (name: string) => string
    whatGoesWith: (name: string) => string
    moreLike: (name: string) => string
    compare: (names: string[]) => string
    differentOptions: string
    without: (label: string) => string
    takeOption: (number: number) => string
    notOption: (number: number) => string
    moreSeating: string
    otherOptions: (role: string) => string
    useOption: (number: number, role: string) => string
    visualizeRoom: (view: string) => string
    placeRoom: string
    placeSelection: (count: number) => string
    visualizeSelection: (count: number, size: string, style: string, room: string, view: string) => string
    showSelection: (view: string) => string
    showSelectionInRoom: string
  }
  /** Several values as one list: "a, b and c" / "a، b وc". */
  list: (values: string[]) => string
  /** Between values listed side by side: ", " / "، ". */
  separator: string
  catalog: {
    title: string
    close: string
    stepPick: string
    stepRoom: string
    tryAgain: string
    loading: string
    backToProducts: string
    nothingPicked: string
    picked: (products: number, pieces: number) => string
    maxProducts: (max: number) => string
    total: (price: string) => string
    visualizeRoom: string
    renderUnavailable: string
    waitingForReply: string
    sort: Record<'label' | 'featured' | 'price_asc' | 'price_desc', string>
    searchPlaceholder: string
    searchProducts: string
    clearSearch: string
    filter: Record<'category' | 'type' | 'colour' | 'style', string>
    allCategories: string
    allTypes: string
    anyColour: string
    anyStyle: string
    minPrice: string
    maxPrice: string
    minShort: string
    maxShort: string
    clearFilters: string
    noMatches: string
    page: (count: string, page: number, pages: number) => string
    previousPage: string
    nextPage: string
    sizeError: (min: number, max: number) => string
    where: string
    whereAria: string
    setUpRoom: string
    myRoomPhoto: string
    room: string
    roomAria: string
    size: string
    sizeOptional: string
    roomLength: string
    roomWidth: string
    view: string
    inTheRoom: string
    uploadPhoto: string
    photoNote: string
    fit: {
      enterSize: string
      over: string
      crowded: string
      ok: string
      coverage: (covered: string, floor: string, percent: number) => string
      aim: (percent: number) => string
      crowdedNote: (percent: number, aim: number) => string
      overNote: string
      tooBig: (name: string, length: number) => string
      uncounted: (count: number) => string
      notCounted: string
    }
    quantity: {
      of: (name: string) => string
      fewer: (name: string) => string
      more: (name: string) => string
      atMost: (max: number) => string
      remove: (name: string) => string
    }
  }
}

/** A label with its `{name}`, `{count}` or `{max}` filled in. */
function fill(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (whole, key: string) =>
    key in values ? String(values[key]) : whole,
  )
}

/** A label with its `{price}` taken out, and the separator before it:
 *  "Add all to cart · {price}" → "Add all to cart". */
function withoutPrice(template: string): string {
  return template.replace(/[\s·:\-–—]*\{price\}/g, '').trim()
}

/** The words for a reply - the server's labels, or English without them. */
export function cardWords(labels: CardLabels | null | undefined): CardWords {
  const l = labels ?? ENGLISH
  const list = (values: string[]) =>
    values.length > 1
      ? `${values.slice(0, -1).join(l.list_separator)}${l.list_and}${values.at(-1)}`
      : values.join('')
  const pieces = (count: number) =>
    count === 1 ? l.pieces_one : fill(l.pieces_many, { count })
  return {
    select: l.select,
    selected: l.selected,
    moreLikeThis: l.more_like_this,
    notThisOne: l.not_this_one,
    compare: l.compare,
    viewProduct: l.view_product,
    like: l.like,
    liked: l.liked,
    likeName: (name) => fill(l.like_product, { name }),
    unlikeName: (name) => fill(l.unlike_product, { name }),
    widened: l.widened,
    yourPick: l.your_pick,
    differentOptions: l.show_different_options,
    compareHintKind: l.compare_same_kind,
    compareHintMax: (max) => fill(l.compare_limit, { max }),
    tray: {
      yourPicks: (count) => fill(l.your_picks, { count }),
      goesWith: l.goes_with,
      goesWithTitle: l.what_goes_with,
      goesWithName: (name) => fill(l.what_goes_with_product, { name }),
      removeName: (name) => fill(l.remove_pick, { name }),
      focused: l.focused_pick,
      total: (price) => fill(l.cart_total, { price }),
      empty: l.cart_empty,
      quantity: l.quantity,
      increaseName: (name) => fill(l.increase_quantity, { name }),
      decreaseName: (name) => fill(l.decrease_quantity, { name }),
      quantityLimit: (max) => fill(l.quantity_limit, { max }),
      goToCart: l.go_to_cart,
    },
    compareBar: {
      title: l.compare_title,
      clear: l.clear,
      compare: (count) => (count === null ? l.compare : fill(l.compare_count, { count })),
      needTwo: l.compare_need_two,
      oneMore: l.compare_one_more,
      stopName: (name) => fill(l.stop_comparing, { name }),
    },
    tile: {
      add: l.add,
      view: l.view,
      roomLimit: (max) => fill(l.room_limit, { max }),
    },
    room: {
      title: l.room_package,
      yourSeating: l.your_seating,
      seatingOption: (number) => fill(l.seating_option, { number }),
      chooseThis: l.choose_this,
      addAllToCart: (price) =>
        price != null ? fill(l.add_all_to_cart, { price }) : withoutPrice(l.add_all_to_cart),
      notThisOption: l.not_this_option,
      moreOptions: l.more_options,
      status: {
        complete: l.status_complete,
        partial: l.status_partial,
        infeasible: l.status_infeasible,
      },
      locked: l.locked,
      alreadyOwned: l.already_owned,
      swap: l.swap,
      swapPiece: (kind) => fill(l.swap_piece, { name: kind }),
      each: (price) => fill(l.each, { price }),
      newSpend: l.new_spend,
      budget: (price, exclusive) => fill(exclusive ? l.budget_under : l.budget_up_to, { price }),
      withinBudget: l.within_budget,
      overBudget: l.over_budget,
      totalUnavailable: (reason) =>
        reason === 'mixed_price_units' ? l.total_mixed_units : l.total_nothing_priced,
      cameraView: l.camera_view,
      views: {
        corner: l.view_corner,
        eye_level: l.view_eye_level,
        isometric: l.view_isometric,
        top_down: l.view_top_down,
      },
      visualize: l.visualize,
      uploadRoomPhoto: l.upload_room_photo,
      placeInMyRoom: l.place_in_my_room,
      changePhoto: l.change_photo,
    },
    brief: {
      narrowDown: l.narrow_down,
      quickTaps: l.quick_taps,
      narrowTheseDown: l.narrow_these_down,
      upTo: (count) => fill(l.up_to, { count }),
      cancel: l.cancel,
      anythingElse: l.anything_else,
      remove: (label) => fill(l.remove_chip, { name: label }),
      or: l.choice_or,
    },
    comparison: {
      title: l.comparison_heading,
      field: l.comparison_field,
      fieldName: (field) => (l as unknown as Record<string, string | undefined>)[`field_${field}`] ?? null,
      status: { different: l.comparison_different, same: l.comparison_same },
      viewProduct: l.view_product,
      listedDimensions: l.listed_dimensions,
      listedCaveat: l.listed_caveat,
      notListed: l.not_listed,
      notListedTitle: l.not_listed_title,
    },
    chips: {
      quickReply: l.quick_reply,
      goesWellWithIt: l.goes_well_with_it,
      alsoGoesWellWithIt: l.also_goes_well_with_it,
    },
    picture: {
      title: l.render_title,
      view: (view) => fill(l.render_view, { view }),
      outdated: l.render_outdated,
      fullScreen: l.full_screen,
      openFullScreen: l.open_full_screen,
      close: l.close,
      download: l.download,
      editSelection: l.edit_selection,
      renderAgain: l.render_again,
      tryThisView: l.try_this_view,
      altFurnished: l.render_alt_furnished,
      altView: (view) => fill(l.render_alt_view, { view }),
      roomVisualisation: l.room_visualisation,
    },
    say: {
      like: (name) => fill(l.say_like, { name }),
      addedToCart: (name) => fill(l.say_added_to_cart, { name }),
      addAllToCart: l.say_add_all_to_cart,
      notThisOne: (name) => fill(l.say_not_this_one, { name }),
      whatGoesWith: (name) => fill(l.say_what_goes_with, { name }),
      moreLike: (name) => fill(l.say_more_like, { name }),
      compare: (names) => fill(l.say_compare, { names: list(names) }),
      differentOptions: l.show_different_options,
      without: (label) => fill(l.say_without, { name: label }),
      takeOption: (number) => fill(l.say_take_option, { number }),
      notOption: (number) => fill(l.say_not_option, { number }),
      moreSeating: l.say_more_seating,
      otherOptions: (role) => fill(l.say_other_options, { name: role }),
      useOption: (number, role) => fill(l.say_use_option, { number, name: role }),
      visualizeRoom: (view) => fill(l.say_visualize_room, { view }),
      placeRoom: l.say_place_room,
      placeSelection: (count) => fill(l.say_place_selection, { pieces: pieces(count) }),
      visualizeSelection: (count, size, style, room, view) =>
        fill(l.say_visualize_selection, { pieces: pieces(count), size, style, room, view }),
      showSelection: (view) => fill(l.say_show_selection, { view }),
      showSelectionInRoom: l.say_show_selection_in_room,
    },
    list,
    separator: l.list_separator,
    catalog: {
      title: l.catalog_title,
      close: l.close_catalogue,
      stepPick: l.step_pick,
      stepRoom: l.step_room,
      tryAgain: l.try_again,
      loading: l.loading_catalogue,
      backToProducts: l.back_to_products,
      nothingPicked: l.nothing_picked,
      picked: (products, units) =>
        `${products === 1 ? l.products_one : fill(l.products_many, { count: products })} · ${pieces(units)}`,
      maxProducts: (max) => fill(l.catalog_max_products, { max }),
      total: (price) => fill(l.catalog_total, { price }),
      visualizeRoom: l.visualize_room,
      renderUnavailable: l.render_unavailable,
      waitingForReply: l.waiting_for_reply,
      sort: {
        label: l.sort_label,
        featured: l.sort_featured,
        price_asc: l.sort_price_asc,
        price_desc: l.sort_price_desc,
      },
      searchPlaceholder: l.search_placeholder,
      searchProducts: l.search_products,
      clearSearch: l.clear_search,
      filter: {
        category: l.filter_category,
        type: l.filter_type,
        colour: l.filter_colour,
        style: l.filter_style,
      },
      allCategories: l.all_categories,
      allTypes: l.all_types,
      anyColour: l.any_colour,
      anyStyle: l.any_style,
      minPrice: l.min_price,
      maxPrice: l.max_price,
      minShort: l.min_short,
      maxShort: l.max_short,
      clearFilters: l.clear_filters,
      noMatches: l.no_matches,
      page: (count, page, pages) => fill(l.catalog_page, { count, page, pages }),
      previousPage: l.previous_page,
      nextPage: l.next_page,
      sizeError: (min, max) => fill(l.size_error, { min, max }),
      where: l.room_where,
      whereAria: l.room_where_aria,
      setUpRoom: l.room_setup,
      myRoomPhoto: l.room_my_photo,
      room: l.room_kind,
      roomAria: l.room_kind_aria,
      size: l.room_size,
      sizeOptional: l.room_size_optional,
      roomLength: l.room_length,
      roomWidth: l.room_width,
      view: l.room_view,
      inTheRoom: l.in_the_room,
      uploadPhoto: l.upload_room_photo_long,
      photoNote: l.room_photo_note,
      fit: {
        enterSize: l.fit_enter_size,
        over: l.fit_over,
        crowded: l.fit_crowded,
        ok: l.fit_ok,
        coverage: (covered, floor, percent) => fill(l.fit_coverage, { covered, floor, percent }),
        aim: (percent) => fill(l.fit_aim, { percent }),
        crowdedNote: (percent, aim) => fill(l.fit_crowded_note, { percent, aim }),
        overNote: l.fit_over_note,
        tooBig: (name, length) => fill(l.fit_too_big, { name, length }),
        uncounted: (count) =>
          count === 1 ? l.fit_uncounted_one : fill(l.fit_uncounted_many, { count }),
        notCounted: l.fit_not_counted,
      },
      quantity: {
        of: (name) => fill(l.quantity_of, { name }),
        fewer: (name) => fill(l.one_fewer, { name }),
        more: (name) => fill(l.one_more, { name }),
        atMost: (max) => fill(l.at_most_per_product, { max }),
        remove: (name) => fill(l.remove_chip, { name }),
      },
    },
  }
}

/** The labels of the latest reply that carried any - the conversation's
 *  words for what is drawn outside a reply (the tray, the compare bar, the
 *  catalogue). English before the first reply. */
export function latestLabels(turns: Turn[]): CardLabels | null {
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    const turn = turns[i]
    if (turn.kind === 'assistant' && turn.data.labels) return turn.data.labels
  }
  return null
}
