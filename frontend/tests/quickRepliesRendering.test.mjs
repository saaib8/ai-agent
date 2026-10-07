import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'

const source = readFileSync(new URL('../src/components/QuickReplies.tsx', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React,
  },
})
const code = `import React from ${JSON.stringify(import.meta.resolve('react'))};\n${outputText}`
const { QuickReplies } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)
const render = (replies) => renderToStaticMarkup(createElement(QuickReplies, {
  replies, onPick: () => {}, disabled: false,
}))

test('No backend choices means no inferred buttons', () => {
  assert.equal(render([]), '')
})

for (const answers of [
  ['Burnt-orange colour', 'Curved headboard'],
  ['اللون البرتقالي', 'شكل اللوح المنحني'],
]) {
  test(`Render backend alternatives unchanged: ${answers.join(' / ')}`, () => {
    const html = render(answers.map((answer) => ({ label: answer, value: answer })))
    assert.equal((html.match(/<button/g) ?? []).length, answers.length)
    for (const answer of answers) assert.ok(html.includes(answer))
    assert.doesNotMatch(html, /Beige|Grey|White|بيج|رمادي/)
  })
}
