// The backend asks clarifications as free text ("Which currency…?", "How many
// people…?"). It returns no structured options, so we derive smart quick-reply
// chips from the question — the affordance a senior chat UI adds so the customer
// taps instead of typing. Matching is intentionally conservative: when nothing
// is confidently recognised we show no chips rather than guess wrong ones.

export interface QuickReply {
  label: string
  value: string
}

const rule = (test: RegExp, replies: QuickReply[]) => ({ test, replies })

const simple = (values: string[]): QuickReply[] => values.map((v) => ({ label: v, value: v }))

const RULES = [
  // Results on screen (a photo pick's offer): follow-ups the chat resolves
  // against the cards the customer is looking at.
  rule(/compare any of these|hear more about one/i, [
    { label: 'Compare the first two', value: 'Compare the first two' },
    { label: 'More about the first one', value: 'Tell me more about the first one' },
    { label: 'Anything cheaper?', value: 'Do you have anything cheaper?' },
  ]),

  // Currency — the store is Saudi, so SAR leads.
  rule(/currenc|which (currency|money)|sar|usd|riyal/i, simple(['SAR', 'USD', 'AED', 'EUR'])),

  // Seating capacity.
  rule(/how many (people|seats?|persons?)|seat(s|ing)?\b|people (does|do|will|is|are)/i, [
    { label: '2 people', value: 'For 2 people' },
    { label: '3 people', value: 'For 3 people' },
    { label: '4 people', value: 'For 4 people' },
    { label: '5 people', value: 'For 5 people' },
    { label: '6+ people', value: 'For 6 or more people' },
  ]),

  // Budget (only when it isn't really a currency question).
  rule(/budget|how much|price range|spend|maximum you|willing to pay/i, [
    { label: 'Under 3,000 SAR', value: 'Under 3000 SAR' },
    { label: 'Under 6,000 SAR', value: 'Under 6000 SAR' },
    { label: 'Under 12,000 SAR', value: 'Under 12000 SAR' },
    { label: 'No strict limit', value: 'No strict budget' },
  ]),

  // Style / look.
  rule(/style|which look|aesthetic|vibe/i, [
    { label: 'Modern', value: 'Modern style' },
    { label: 'Minimalist', value: 'Minimalist style' },
    { label: 'Scandinavian', value: 'Scandinavian style' },
    { label: 'Classic', value: 'Classic style' },
    { label: 'Rustic', value: 'Rustic style' },
  ]),

  // Room type.
  rule(/which room|what room|room type|room is this|for the (living|bed)/i, [
    { label: 'Living room', value: 'Living room' },
    { label: 'Bedroom', value: 'Bedroom' },
    { label: 'Dining room', value: 'Dining room' },
    { label: 'Office', value: 'Office' },
  ]),

  // Color.
  rule(/colou?r|which shade|tone/i, simple(['Beige', 'Grey', 'White', 'Black', 'Blue', 'Green'])),
]

const YES_NO_START = /^(do|does|did|would|should|is|are|can|could|shall|will|have|has|may)\b/i

// Strip the lead-in of a choice question so the two branches are left bare:
// "Would you prefer something compact…" -> "compact…".
const CHOICE_STEM =
  /^(would you (prefer|like|want|rather)|do you (prefer|want|like)|which (do you prefer|would you prefer|one)|are you (after|looking for))\b[:,]?\s*(something\s+)?/i

const capitalise = (s: string): string => s.charAt(0).toUpperCase() + s.slice(1)

// "A or B?" is a choice, not a yes/no. Pull out the two sides as chips.
function choiceReplies(question: string): QuickReply[] {
  const body = question.replace(/\?+\s*$/, '').replace(CHOICE_STEM, '').trim()
  const parts = body
    .split(/\s*,?\s+or\s+/i)
    .map((p) => p.trim().replace(/[.?]+$/, ''))
    .filter(Boolean)
  // Only when it splits cleanly into two short, sensible sides. Otherwise show
  // nothing rather than a lopsided guess.
  if (parts.length !== 2 || parts.some((p) => p.length < 2 || p.length > 40)) return []
  return parts.map((p) => ({ label: capitalise(p), value: capitalise(p) }))
}

export function deriveQuickReplies(message: string, followUp: string | null): QuickReply[] {
  const question = followUp ?? message
  // Only offer chips for a turn that is actually asking something.
  if (!question.includes('?')) return []

  // Match on the question itself, not the original request. "How many people
  // to seat?" asked after "…under 6000 SAR" must offer seat counts, not
  // currencies — the word in the request must not decide the answer options.
  for (const { test, replies } of RULES) {
    if (test.test(question)) return replies
  }

  // An either/or question ("compact… or broader…?") is a choice between two
  // things, never a yes/no — offering "Yes" to it is meaningless and stalls
  // the conversation. Offer the two sides; if we cannot split it cleanly, show
  // nothing rather than the wrong chip.
  if (/\bor\b/i.test(question)) return choiceReplies(question)

  // A plainly phrased yes/no question is the last, safest fallback.
  if (YES_NO_START.test(question.trim())) {
    return [
      { label: 'Yes', value: 'Yes' },
      { label: 'No', value: 'No' },
    ]
  }
  return []
}
