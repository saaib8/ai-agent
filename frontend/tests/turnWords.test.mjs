import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import ts from 'typescript'

const source = readFileSync(new URL('../src/lib/turnWords.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
})
const { conversationLanguage } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
const reply = (language) => ({ kind: 'assistant', data: { reply_language: language } })

test('Photo and rendering replies without metadata preserve Arabic', () => {
  assert.equal(conversationLanguage([reply('ar'), reply(null), reply(undefined)]), 'ar')
})

test('An explicit English reply changes the conversation language', () => {
  assert.equal(conversationLanguage([reply('ar'), reply(null), reply('en')]), 'en')
})

test('A fresh conversation defaults to English', () => {
  assert.equal(conversationLanguage([]), 'en')
  assert.equal(conversationLanguage([reply(null)]), 'en')
})
