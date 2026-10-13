import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import ts from 'typescript'

const source = ts.transpileModule(
  readFileSync(new URL('../src/lib/briefAnswer.ts', import.meta.url), 'utf8'),
  { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } },
).outputText
const { briefAnswer, questionKey } = await import(
  `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
)

const question = (kind, id, keys, max = 1) => ({
  kind,
  id,
  label: kind,
  choices: keys.map((key) => ({ key, label: key })),
  max_choices: max,
})

// A sub-category's card: a type, a colour, and two feels told apart by id.
const sofa = {
  card: 4,
  mode: 'ask',
  noun: 'sofas',
  questions: [
    question('type', 'q1', ['q1-1', 'q1-2']),
    question('colour', 'q2', ['Beige', 'Grey'], 3),
    question('feel', 'q3', ['q3-1', 'q3-2'], 3),
    question('feel', 'q4', ['q4-1', 'q4-2'], 3),
  ],
  submit_label: 'Show me sofas',
  skip_label: 'Just show me sofas',
}

test('a question is kept under its id, or its kind on a family card', () => {
  assert.equal(questionKey(sofa.questions[2]), 'q3')
  assert.equal(questionKey({ ...sofa.questions[0], id: null }), 'type')
})

test("every feel ticked across a sub-category's questions is sent", () => {
  const answer = briefAnswer(sofa, { q1: ['q1-2'], q2: ['Beige'], q3: ['q3-1'], q4: ['q4-1', 'q4-2'] })
  assert.equal(answer.piece, 'q1-2')
  assert.deepEqual(answer.colours, ['Beige'])
  assert.deepEqual(answer.feels, ['q3-1', 'q4-1', 'q4-2'])
  assert.equal(answer.feel, null)
})

test('a family card sends its one feel as before, and no feels', () => {
  const family = { ...sofa, questions: [question('feel', null, ['feel-1', 'feel-2'])] }
  const answer = briefAnswer(family, { feel: ['feel-2'] })
  assert.equal(answer.feel, 'feel-2')
  assert.equal('feels' in answer, false)
})

test("the sheet's words beside a colour question's colours are sent as feels", () => {
  const colourOrPattern = {
    kind: 'colour',
    id: 'q3',
    label: 'Which color or pattern do you prefer?',
    choices: [
      { key: 'Beige', label: 'Beige' },
      { key: 'Grey', label: 'Grey' },
      { key: 'q3-3', label: 'Striped', kind: 'feel' },
    ],
    max_choices: 3,
  }
  const rug = { ...sofa, questions: [question('feel', 'q2', ['q2-1', 'q2-2'], 3), colourOrPattern] }
  const answer = briefAnswer(rug, { q2: ['q2-1'], q3: ['Grey', 'q3-3'] })
  assert.deepEqual(answer.colours, ['Grey'])
  assert.deepEqual(answer.feels, ['q2-1', 'q3-3'])
})
