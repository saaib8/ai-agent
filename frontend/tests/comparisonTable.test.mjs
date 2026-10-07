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
const component = compile('../src/components/presentation/ComparisonTable.tsx')
  .replace("'../../lib/format'", JSON.stringify(formatUrl))
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
  assert.doesNotMatch(html, /overall width/)
})

test('Known semantic dimensions are preserved alongside listed sizes', () => {
  const html = render([
    product(1, size('220', '95', '85')),
    product(2, size('200', '90', '80')),
  ], [{ field: 'overall_width', status: 'different', cells: [
    { known: true, value: '220 cm' }, { known: true, value: '200 cm' },
  ] }])
  assert.match(html, /L 220 · W 95 · H 85 cm/)
  assert.match(html, /overall width/)
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
