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

const react = JSON.stringify(import.meta.resolve('react'))
const dataUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const withReact = (source) => `import React from ${react};\n${source}`
const wordsUrl = dataUrl(compile('../src/lib/briefWords.ts'))
const iconsUrl = dataUrl(withReact(compile('../src/components/icons.tsx')))
const component = compile('../src/components/BriefCard.tsx')
  .replace("'../lib/briefWords'", JSON.stringify(wordsUrl))
  .replace("'./icons'", JSON.stringify(iconsUrl))
  .replace("from 'react'", `from ${react}`)
const { BriefCard } = await import(dataUrl(withReact(component)))

const brief = (mode) => ({
  card: 6,
  mode,
  noun: 'sofas',
  questions: [
    {
      kind: 'type',
      label: 'What kind?',
      choices: [
        { key: 'sofa:3', label: '3-seater' },
        { key: 'sofa-set', label: 'Sofa set' },
      ],
      max_choices: 1,
      selected: ['sofa:3'],
    },
  ],
  submit_label: 'Show me sofas',
  skip_label: 'Just show me sofas',
})

const render = (props) =>
  renderToStaticMarkup(
    createElement(BriefCard, {
      language: 'en',
      active: true,
      busy: false,
      onSubmit: () => {},
      ...props,
    }),
  )

test('Narrow down they asked for opens with its questions showing', () => {
  const html = render({ brief: brief('narrow'), opened: true })
  assert.ok(html.includes('What kind?'))
  assert.ok(html.includes('3-seater'))
  assert.ok(html.includes('Narrow these down'))
})

test('Narrow down beside results stays folded', () => {
  const html = render({ brief: brief('narrow') })
  assert.ok(html.includes('Narrow down'))
  assert.ok(!html.includes('What kind?'))
})

test('An opened card no longer latest is a read-only record', () => {
  const html = render({ brief: brief('narrow'), opened: true, active: false })
  assert.ok(html.includes('What kind?'))
  const buttons = html.match(/<button[^>]*>/g) ?? []
  assert.ok(buttons.length > 0)
  assert.ok(buttons.every((button) => button.includes('disabled')))
})

test('A card of questions opens as before', () => {
  assert.ok(render({ brief: brief('ask') }).includes('What kind?'))
})
