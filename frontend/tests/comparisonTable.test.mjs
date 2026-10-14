import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'

function compile(path) {
  return ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.React,
    },
  }).outputText
}

const dataUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const formatUrl = dataUrl(compile('../src/lib/format.ts'))
const wordsUrl = dataUrl(compile('../src/lib/cardWords.ts'))
const productWordsUrl = dataUrl(
  compile('../src/lib/productWords.ts').replace("'./format'", JSON.stringify(formatUrl)),
)
const component = compile('../src/components/presentation/ComparisonTable.tsx')
  .replace("'../../lib/format'", JSON.stringify(formatUrl))
  .replace("'../../lib/cardWords'", JSON.stringify(wordsUrl))
  .replace("'../../lib/productWords'", JSON.stringify(productWordsUrl))
const { ENGLISH } = await import(wordsUrl)
const { ComparisonTable } = await import(dataUrl(
  `import React from ${JSON.stringify(import.meta.resolve('react'))};\n${component}`,
))

const product = (id, dimensions) => ({
  grounding_ref: id,
  name_english: `Product ${id}`,
  price_amount: '1000',
  price_unit: 'SAR',
  dimensions,
})
const size = (length, width, height, status = 'normalised') => ({
  length_cm: length, width_cm: width, height_cm: height, status,
})
const render = (products, rows = []) => renderToStaticMarkup(
  createElement(ComparisonTable, { comparison: { products, rows } }),
)

test('Catalog sizes remain visible when semantic measurement rows are unknown', () => {
  const html = render([
    product(1, size('220', '180', '90')),
    product(2, size('200', '160', '85')),
  ], [{ field: 'overall_width', status: 'unknown', cells: [{ known: false }, { known: false }] }])
  assert.match(html, /Listed dimensions/)
  assert.match(html, /L 220 · W 180 · H 90 cm/)
  assert.match(html, /L 200 · W 160 · H 85 cm/)
  assert.match(html, /length and width may be recorded in either order/)
  assert.doesNotMatch(html, /overall width/i)
})

test('Known measurement rows replace the listed sizes rather than sit beside them', () => {
  // A sofa's stored length is its width: "L 220" beside "overall width 220"
  // would contradict the row the backend read correctly (CLAUDE.md 15.1).
  const html = render([
    product(1, size('220', '95', '85')),
    product(2, size('200', '90', '80')),
  ], [{ field: 'overall_width', status: 'different', cells: [
    { known: true, value: '220 cm' }, { known: true, value: '200 cm' },
  ] }])
  assert.doesNotMatch(html, /Listed dimensions/)
  assert.doesNotMatch(html, /L 220 · W 95 · H 85 cm/)
  assert.match(html, /overall width/i)
  assert.match(html, /different/)
})

test('Partial dimensions display only measured axes', () => {
  const html = render([product(1, size(null, '95', null)), product(2, size('200', null, '80'))])
  assert.match(html, /W 95 cm/)
  assert.match(html, /L 200 · H 80 cm/)
  assert.doesNotMatch(html, /null|undefined/)
})

test('Absent dimensions are explicitly marked rather than hidden', () => {
  const html = render([product(1, size(null, null, null, 'absent')), product(2, null)])
  assert.match(html, /Listed dimensions/)
  assert.equal((html.match(/Not listed/g) ?? []).length, 2)
})

test('Unknown units are never presented as centimetres', () => {
  const html = render([
    product(1, size('220', '95', '85', 'unknown_unit')),
    product(2, size(null, null, null, 'absent')),
  ])
  assert.equal((html.match(/Not listed/g) ?? []).length, 2)
  assert.doesNotMatch(html, /220|95|85/)
})

test('names, prices and the kind, colour and styles rows read as the server worded them', () => {
  const display = (name, price) => ({
    name, kind: 'كنبة', color: 'بيج', styles: ['عصري', 'بسيط'], seats: null, price, size: null,
  })
  const products = [
    { ...product(1), display: display('كنبة ثلاثية', '1,000 ريال') },
    { ...product(2), display: display('كنبة رباعية', '1,200 ريال') },
  ]
  const cell = (value) => ({ known: true, value })
  const rows = [
    { field: 'commerce_subcategory', status: 'same', cells: [cell('sofa'), cell('sofa')] },
    { field: 'main_color', status: 'same', cells: [cell('Beige'), cell('Beige')] },
    { field: 'styles', status: 'same', cells: [cell('Minimalist, Modern'), cell('Minimalist, Modern')] },
    { field: 'price', status: 'different', cells: [cell('1000 SAR'), cell('1200 SAR')] },
  ]
  // The server's Arabic words for the table (`ChatResponse.labels`).
  const labels = {
    ...ENGLISH, comparison_heading: 'المقارنة', field_styles: 'الطراز', list_separator: '، ',
  }
  const html = renderToStaticMarkup(
    createElement(ComparisonTable, { comparison: { products, rows }, labels }),
  )
  assert.match(html, /المقارنة/)
  assert.match(html, /الطراز/)
  assert.match(html, /كنبة ثلاثية/)
  assert.match(html, />بيج</)
  assert.match(html, /عصري، بسيط/)
  assert.match(html, /1000 SAR/)
  // The header's price reads in riyals.
  assert.match(html, /1,000 ريال/)
  assert.doesNotMatch(html, />Beige</)
  // Without the server's display text, the backend's words, in English.
  const bare = [product(1), product(2)]
  const english = renderToStaticMarkup(
    createElement(ComparisonTable, { comparison: { products: bare, rows } }),
  )
  assert.match(english, /Product 1/)
  assert.match(english, />Beige</)
  assert.match(english, /1,000 SAR/)
})
