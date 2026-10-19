import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'

const react = `import React from ${JSON.stringify(import.meta.resolve('react'))};\n`

function load(path, imports = {}) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8')
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React,
    },
  })
  for (const [from, to] of Object.entries(imports)) {
    outputText = outputText.replace(`from '${from}'`, `from ${JSON.stringify(to)}`)
  }
  return `data:text/javascript;base64,${Buffer.from(react + outputText).toString('base64')}`
}

const icons = load('../src/components/icons.tsx')
const links = load('../src/lib/supportLinks.ts')
const { SupportChannels } = await import(
  load('../src/components/SupportChannels.tsx', { './icons': icons, '../lib/supportLinks': links })
)
const { supportLinks } = await import(links)
const render = (support) => renderToStaticMarkup(createElement(SupportChannels, { support }))

const WHATSAPP = { channel: 'whatsapp', label: 'WhatsApp', url: 'https://wa.me/966500000000' }
const EMAIL = { channel: 'email', label: 'Email', url: 'mailto:help@store.example' }

test('No support on the reply draws nothing', () => {
  assert.equal(render(undefined), '')
  assert.equal(render(null), '')
})

test('Each channel the store set is one icon opening its own link', () => {
  const html = render({ channels: [WHATSAPP, EMAIL] })
  assert.equal((html.match(/<a /g) ?? []).length, 2)
  assert.ok(html.includes('href="https://wa.me/966500000000"'))
  assert.ok(html.includes('href="mailto:help@store.example"'))
  assert.ok(html.indexOf('wa.me') < html.indexOf('mailto:'))
})

test('A store with only an email shows only the email icon', () => {
  const html = render({ channels: [EMAIL] })
  assert.equal((html.match(/<a /g) ?? []).length, 1)
  assert.doesNotMatch(html, /wa\.me/)
})

test('Links open apart from the chat, without handing it the page', () => {
  const html = render({ channels: [WHATSAPP] })
  assert.ok(html.includes('target="_blank"'))
  assert.ok(html.includes('rel="noopener noreferrer"'))
})

test('Each icon is named in the reply language for screen readers and tooltips', () => {
  const html = render({ channels: [{ ...WHATSAPP, label: 'واتساب' }] })
  assert.ok(html.includes('aria-label="واتساب"'))
  assert.ok(html.includes('title="واتساب"'))
})

test('A link not of its channel\'s shape is never drawn', () => {
  const bad = [
    { channel: 'whatsapp', label: 'WhatsApp', url: 'javascript:alert(1)' },
    { channel: 'email', label: 'Email', url: 'https://evil.example' },
    { channel: 'whatsapp', label: 'WhatsApp', url: 'https://wa.me/' },
    { channel: 'sms', label: 'SMS', url: 'sms:123' },
  ]
  assert.deepEqual(supportLinks({ channels: bad }), [])
  assert.equal(render({ channels: bad }), '')
})
