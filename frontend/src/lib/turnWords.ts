// The language the conversation is answered in, for what the console asks the
// server in it (the catalogue). Every word it shows - including what a tapped
// button puts in the customer's own bubble - is the server's (`labels`).

import type { Turn } from '../hooks/useChat'

export type ReplyLanguage = 'en' | 'ar'

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
