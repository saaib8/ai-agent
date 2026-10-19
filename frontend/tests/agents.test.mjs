import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import ts from 'typescript'

const source = readFileSync(new URL('../src/lib/agents.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
})
const { AGENTS, agentLook } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)

test('Each reply is shown under the assistant the server named', () => {
  assert.equal(agentLook('nora').name, 'Nora')
  assert.equal(agentLook('salman').name, 'Salman')
  assert.equal(agentLook('sara').name, 'Sara')
})

test('A reply with no assistant named is Sara, the session voice', () => {
  assert.equal(agentLook(undefined).name, 'Sara')
  assert.equal(agentLook('someone-else').name, 'Sara')
})

test('Sara and Salman share an initial, so their colours differ', () => {
  assert.equal(AGENTS.sara.initial, AGENTS.salman.initial)
  assert.notEqual(AGENTS.sara.avatar, AGENTS.salman.avatar)
})

test('Faysal, the store support assistant, has a look of his own', () => {
  assert.equal(agentLook('faysal').name, 'Faysal')
  assert.notEqual(AGENTS.faysal.avatar, AGENTS.sara.avatar)
})
