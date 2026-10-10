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
  moreLikeThis: (name: string) => string
  compareProducts: (names: string[]) => string
  differentOptions: string
  dropChip: (label: string) => string
  chooseCombination: (position: number) => string
  notCombination: (position: number) => string
  moreCombinations: string
  chooseLabel: string
  notThisLabel: string
  moreLabel: string
  seatingOption: (position: number) => string
  yourSeating: string
  otherRoomOptions: (role: string) => string
  useRoomOption: (ordinal: number, role: string) => string
}

const WORDS: Record<ReplyLanguage, TurnWords> = {
  en: {
    likeProduct: (name) => `I like the ${name}`,
    notThisOne: (name) => `Not this one — the ${name}`,
    notThisOneLabel: 'Not this one',
    whatGoesWith: (name) => `What goes with the ${name}?`,
    moreLikeThis: (name) => `More like the ${name}`,
    compareProducts: (names) =>
      `Compare these products: ${
        names.length > 2 ? `${names.slice(0, -1).join(', ')} and ${names.at(-1)}` : names.join(' and ')
      }`,
    differentOptions: 'Show me different options',
    dropChip: (label) => `Without ${label}`,
    chooseCombination: (position) => `I'll take option ${position}`,
    notCombination: (position) => `Not option ${position}`,
    moreCombinations: 'Show me more seating options',
    chooseLabel: 'Choose this',
    notThisLabel: 'Not this one',
    moreLabel: 'Show more options',
    seatingOption: (position) => `Seating option ${position}`,
    yourSeating: 'Your seating',
    otherRoomOptions: (role) => `Show me other ${role} options`,
    useRoomOption: (ordinal, role) => `Use option ${ordinal} for the ${role}`,
  },
  ar: {
    likeProduct: () => 'أعجبني هذا',
    notThisOne: () => 'لا أريد هذا',
    notThisOneLabel: 'لا أريد هذا',
    whatGoesWith: () => 'ما الذي يناسب هذا؟',
    moreLikeThis: () => 'أرني المزيد مثل هذا',
    compareProducts: () => 'قارن هذه المنتجات',
    differentOptions: 'أرني خيارات مختلفة',
    dropChip: (label) => `من دون ${label}`,
    chooseCombination: (position) => `سآخذ الخيار ${position}`,
    notCombination: (position) => `ليس الخيار ${position}`,
    moreCombinations: 'أرني خيارات جلوس أخرى',
    chooseLabel: 'اختر هذا',
    notThisLabel: 'ليس هذا',
    moreLabel: 'أرني المزيد',
    seatingOption: (position) => `خيار الجلوس ${position}`,
    yourSeating: 'جلستك',
    otherRoomOptions: () => 'أرني خيارات أخرى لهذه القطعة',
    useRoomOption: (ordinal) => `استخدم الخيار ${ordinal} لهذه القطعة`,
  },
}

/** The latest declared reply language. Photo and render replies without
 *  language metadata do not reset an Arabic conversation to English. */
export function conversationLanguage(turns: Turn[]): ReplyLanguage {
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    const turn = turns[i]
    if (turn.kind === 'assistant' && turn.data.reply_language != null) {
      // Only the two languages there are words for; anything else reads as English.
      return turn.data.reply_language === 'ar' ? 'ar' : 'en'
    }
  }
  return 'en'
}

export function turnWords(language: ReplyLanguage): TurnWords {
  return WORDS[language]
}
