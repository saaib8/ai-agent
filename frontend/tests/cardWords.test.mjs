import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import ts from 'typescript'

const compile = (path) =>
  ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText
const dataUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const formatUrl = dataUrl(compile('../src/lib/format.ts'))
const { cardWords, latestLabels, ENGLISH } = await import(dataUrl(compile('../src/lib/cardWords.ts')))
const { productText, optionLabel } = await import(
  dataUrl(compile('../src/lib/productWords.ts').replace("'./format'", JSON.stringify(formatUrl)))
)

// What the server sends with an Arabic reply (`ChatResponse.labels`).
const serverArabic = {
  select: 'اختيار', selected: 'تم الاختيار', more_like_this: 'المزيد مثل هذا',
  not_this_one: 'لا أريد هذا', compare: 'مقارنة', view_product: '← عرض المنتج',
  like: 'إعجاب', liked: 'أعجبني', like_product: 'أعجبني {name}',
  unlike_product: 'إلغاء الإعجاب بـ {name}', widened: 'بحث موسّع', your_pick: 'اختيارك',
  show_different_options: 'أرني خيارات مختلفة', compare_same_kind: 'قارن بمنتجات من النوع نفسه',
  compare_limit: 'حتى {max} منتجات في كل مرة - ألغِ تحديد واحد أولاً', compare_title: 'مقارنة',
  compare_count: 'قارن {count}', compare_need_two: 'حدّد منتجين على الأقل من النوع نفسه',
  compare_one_more: 'حدّد منتجًا آخر من النوع نفسه', stop_comparing: 'إيقاف مقارنة {name}',
  clear: 'مسح', your_picks: 'اختياراتك ({count})', goes_with: 'ما يناسبه',
  what_goes_with: 'ما الذي يناسبه', what_goes_with_product: 'ما الذي يناسب {name}',
  remove_pick: 'إزالة {name} من اختياراتك', focused_pick: 'الاختيار الذي نتحدث عنه',
  add: 'إضافة', view: 'عرض', room_limit: 'يمكن أن تعرض الغرفة حتى {max} منتجات',
  room_package: 'باقة الغرفة', your_seating: 'جلستك', seating_option: 'خيار الجلوس {number}',
  choose_this: 'اختر هذا', not_this_option: 'ليس هذا', more_options: 'أرني المزيد',
  status_complete: 'مكتملة', status_partial: 'جزئية', status_infeasible: 'غير ممكنة',
  locked: 'مثبّتة', already_owned: 'لديك بالفعل', swap: 'تبديل', swap_piece: 'تبديل {name}',
  each: '{price} للقطعة', new_spend: 'إجمالي المشتريات', budget_up_to: 'الميزانية: حتى {price}',
  budget_under: 'الميزانية: أقل من {price}', within_budget: 'ضمن الميزانية',
  over_budget: 'تتجاوز الميزانية', total_mixed_units: 'لا يتوفر إجمالي: الأسعار بعملات مختلفة',
  total_nothing_priced: 'لا يتوفر إجمالي: لا توجد قطع للشراء', camera_view: 'زاوية الكاميرا',
  view_corner: 'زاوية', view_eye_level: 'مستوى النظر', view_isometric: 'أيزومتري',
  view_top_down: 'من الأعلى', visualize: 'تصوّر الغرفة', upload_room_photo: 'رفع صورة الغرفة',
  place_in_my_room: 'ضعها في غرفتي', change_photo: 'تغيير الصورة',
}

test("a card's words are the server's, placeholders filled", () => {
  const words = cardWords(serverArabic)
  assert.equal(words.select, 'اختيار')
  assert.equal(words.tray.yourPicks(2), 'اختياراتك (2)')
  assert.equal(words.tray.removeName('كنبة'), 'إزالة كنبة من اختياراتك')
  assert.equal(words.compareBar.compare(3), 'قارن 3')
  assert.equal(words.compareBar.compare(null), 'مقارنة')
  assert.equal(words.tile.roomLimit(8), 'يمكن أن تعرض الغرفة حتى 8 منتجات')
})

test('without labels from the server the words are English, as before', () => {
  const words = cardWords(undefined)
  assert.equal(words.select, 'Select')
  assert.equal(words.tray.yourPicks(1), 'Your picks (1)')
})

test("a room package's words are the server's, placeholders filled", () => {
  const room = cardWords(serverArabic).room
  assert.equal(room.title, 'باقة الغرفة')
  assert.equal(room.seatingOption(2), 'خيار الجلوس 2')
  assert.equal(room.status.partial, 'جزئية')
  assert.equal(room.each('950 ريال'), '950 ريال للقطعة')
  assert.equal(room.budget('12,000 ريال', false), 'الميزانية: حتى 12,000 ريال')
  assert.equal(room.budget('12,000 ريال', true), 'الميزانية: أقل من 12,000 ريال')
  assert.equal(room.swapPiece('سرير'), 'تبديل سرير')
  assert.equal(room.totalUnavailable('mixed_price_units'), 'لا يتوفر إجمالي: الأسعار بعملات مختلفة')
  assert.equal(room.views.top_down, 'من الأعلى')

  const english = cardWords(null).room
  assert.equal(english.title, 'Room package')
  assert.equal(english.each('950 SAR'), '950 SAR ea')
  assert.equal(english.budget('12,000 SAR', false), 'Budget ≤ 12,000 SAR')
  assert.equal(english.totalUnavailable('no_priced_lines'), 'Total unavailable: no priced lines')
})

test('the tray and the catalogue use the latest reply that carried labels', () => {
  const turns = [
    { kind: 'assistant', data: { labels: { select: 'Select' } } },
    { kind: 'user', text: 'كنبة' },
    { kind: 'assistant', data: { labels: serverArabic } },
    { kind: 'assistant', data: {} },
  ]
  assert.equal(latestLabels(turns).select, 'اختيار')
  assert.equal(latestLabels([]), null)
})

test("a product's text is the server's display, or English without one", () => {
  const product = {
    name_english: '3 Seater Sofa',
    commerce: { category: 'seating', subcategory: 'sofa', seating_capacity: 3 },
    main_color: 'Light Grey',
    styles: ['Modern_Classic'],
    price_amount: '3600',
    price_unit: 'SAR',
    dimensions: { length_cm: '220', width_cm: '95', height_cm: '85', status: 'normalised' },
  }
  const display = {
    name: 'كنبة ثلاثية', kind: 'كنبة', color: 'رمادي فاتح', styles: ['كلاسيكي عصري'],
    seats: '3 مقاعد', price: '3,600 ريال', size: 'الطول 220 · العرض 95 · الارتفاع 85 سم',
  }
  assert.deepEqual(productText({ ...product, display }), display)
  assert.deepEqual(productText(product), {
    name: '3 Seater Sofa',
    kind: 'sofa',
    color: 'Light Grey',
    styles: ['Modern Classic'],
    seats: '3-seat',
    price: '3,600 SAR',
    size: 'L 220 · W 95 · H 85 cm',
  })
})

test("a filter option reads as the server labelled it; its value is what is sent", () => {
  assert.equal(optionLabel({ value: 'Beige', label: 'بيج' }, (v) => v), 'بيج')
  assert.equal(optionLabel({ value: 'Beige' }, (v) => v.toUpperCase()), 'BEIGE')
})

test("the customer's own message after a tap is the server's, in its language", () => {
  const english = cardWords(null).say
  assert.equal(english.like('Oak Bed'), 'I like the Oak Bed')
  assert.equal(english.compare(['A', 'B', 'C']), 'Compare these products: A, B and C')
  assert.equal(english.compare(['A', 'B']), 'Compare these products: A and B')
  assert.equal(english.placeSelection(1), 'Place my selection in my room — 1 piece')
  assert.equal(
    english.visualizeSelection(3, '4 × 5', 'modern', 'bedroom', 'Corner'),
    'Visualize my selection — 3 pieces in a 4 × 5 m modern bedroom, Corner view',
  )

  // The server's Arabic leaves the product out: the card beside the bubble names it.
  const arabic = cardWords({
    ...ENGLISH,
    say_like: 'أعجبني هذا',
    say_without: 'من دون {name}',
    say_visualize_selection: 'تصوّر اختياري - {pieces} في غرفة {size} م، {view}',
    pieces_many: 'عدد القطع {count}',
    choice_or: ' أو ',
    render_view: '{view}',
    list_separator: '، ',
    list_and: ' و',
  })
  assert.equal(arabic.say.like('Oak Bed'), 'أعجبني هذا')
  assert.equal(arabic.say.without('رمادي'), 'من دون رمادي')
  assert.equal(
    arabic.say.visualizeSelection(3, '4 × 5', 'modern', 'bedroom', 'من الزاوية'),
    'تصوّر اختياري - عدد القطع 3 في غرفة 4 × 5 م، من الزاوية',
  )
  assert.equal(arabic.list(['سرير', 'سجادة', 'مرآة']), 'سرير، سجادة ومرآة')
  assert.equal(arabic.brief.or, ' أو ')
  assert.equal(arabic.picture.view('من الأعلى'), 'من الأعلى')
  assert.equal(arabic.comparison.fieldName('price'), 'Price')
  assert.equal(arabic.comparison.fieldName('unknown_field'), null)
})

test("Browse Catalogue's words are the server's, counts and sizes filled", () => {
  const english = cardWords(null).catalog
  assert.equal(english.picked(1, 3), '1 product · 3 pieces')
  assert.equal(english.page('1,204', 2, 51), '1,204 products · page 2 of 51')
  assert.equal(english.fit.uncounted(1), "1 piece has no recorded size, so it isn't counted.")
  assert.equal(english.fit.uncounted(2), "2 pieces have no recorded size, so they aren't counted.")
  assert.equal(english.sort.price_asc, 'Price: low to high')

  const arabic = cardWords({
    ...ENGLISH,
    products_many: 'عدد المنتجات {count}',
    pieces_many: 'عدد القطع {count}',
    catalog_total: 'الإجمالي {price}',
    fit_too_big: '{name} ({length} م) لن تتسع لها هذه الغرفة.',
    quantity_of: 'كمية {name}',
  }).catalog
  assert.equal(arabic.picked(2, 4), 'عدد المنتجات 2 · عدد القطع 4')
  assert.equal(arabic.total('8,450 ريال'), 'الإجمالي 8,450 ريال')
  assert.equal(arabic.fit.tooBig('كنبة', 3.2), 'كنبة (3.2 م) لن تتسع لها هذه الغرفة.')
  assert.equal(arabic.quantity.of('كنبة'), 'كمية كنبة')
})
