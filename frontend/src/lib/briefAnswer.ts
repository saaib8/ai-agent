import type { BriefAnswerAction, BriefQuestion, ProductBrief } from '../api/types'

/** What tapped chips are kept under: a sub-category's card may ask several
 *  questions of one kind (a material and a feature are both feels), so its
 *  questions carry an id; the family cards key by kind. */
export function questionKey(question: BriefQuestion): string {
  return question.id ?? question.kind
}

/** The card's answers as the keys it offered. Each tapped chip is answered as
 *  its own kind: a pattern beside a colour question's colours is a feel. */
export function briefAnswer(brief: ProductBrief, picked: Record<string, string[]>): BriefAnswerAction {
  const ticked = brief.questions.flatMap((question) =>
    (picked[questionKey(question)] ?? []).map((key) => ({
      key,
      kind: question.choices.find((choice) => choice.key === key)?.kind ?? question.kind,
      sheet: Boolean(question.id),
    })),
  )
  const ofKind = (kind: BriefQuestion['kind']) =>
    ticked.filter((chip) => chip.kind === kind).map((chip) => chip.key)
  const first = (kind: BriefQuestion['kind']) => ofKind(kind)[0] ?? null
  // Every feel ticked on a sub-category's card; the family cards ask one.
  const feelChips = ticked.filter((chip) => chip.kind === 'feel')
  const feels = feelChips.filter((chip) => chip.sheet).map((chip) => chip.key)
  const feel = feelChips.find((chip) => !chip.sheet)?.key ?? null
  return {
    kind: 'brief',
    card: brief.card,
    piece: first('type'),
    room: first('room'),
    people: first('people'),
    budget: first('budget'),
    space: first('space'),
    colours: ofKind('colour'),
    styles: ofKind('style'),
    feel,
    ...(feels.length ? { feels } : {}),
  }
}
