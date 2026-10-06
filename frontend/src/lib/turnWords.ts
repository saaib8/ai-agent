// The words a screen button puts in the customer's own bubble, in the language
// the conversation is answered in.
//
// A tap sends a structured action, and the backend acts on that alone - these
// words are only what the customer sees as their side of the turn, and what is
// kept in the conversation record. So translating them can never change what a
// tap does.
//
// Arabic names no product or room piece: there are no Arabic product names
// yet, and the card or piece the tap was about is on screen beside the bubble.

import type { Turn } from '../hooks/useChat'

export type ReplyLanguage = 'en' | 'ar'

interface TurnWords {
  likeProduct: (name: string) => string
  notThisOne: (name: string) => string
  notThisOneLabel: string
  whatGoesWith: (name: string) => string
  differentOptions: string
  otherRoomOptions: (role: string) => string
  useRoomOption: (ordinal: number, role: string) => string
}

const WORDS: Record<ReplyLanguage, TurnWords> = {
  en: {
    likeProduct: (name) => `I like the ${name}`,
    notThisOne: (name) => `Not this one — the ${name}`,
    notThisOneLabel: 'Not this one',
    whatGoesWith: (name) => `What goes with the ${name}?`,
    differentOptions: 'Show me different options',
    otherRoomOptions: (role) => `Show me other ${role} options`,
    useRoomOption: (ordinal, role) => `Use option ${ordinal} for the ${role}`,
  },
  ar: {
    likeProduct: () => 'أعجبني هذا',
    notThisOne: () => 'لا أريد هذا',
    notThisOneLabel: 'لا أريد هذا',
    whatGoesWith: () => 'ما الذي يناسب هذا؟',
    differentOptions: 'أرني خيارات مختلفة',
    otherRoomOptions: () => 'أرني خيارات أخرى لهذه القطعة',
    useRoomOption: (ordinal) => `استخدم الخيار ${ordinal} لهذه القطعة`,
  },
}

/** The language the latest reply was written in; English until one says otherwise. */
export function conversationLanguage(turns: Turn[]): ReplyLanguage {
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    const turn = turns[i]
    if (turn.kind === 'assistant') return turn.data.reply_language === 'ar' ? 'ar' : 'en'
  }
  return 'en'
}

export function turnWords(language: ReplyLanguage): TurnWords {
  return WORDS[language]
}
