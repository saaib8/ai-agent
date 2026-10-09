// The card of questions' own words, in the language the conversation is
// answered in. Its questions and chips arrive worded by the backend.

import type { ReplyLanguage } from './turnWords'

interface BriefWords {
  narrowDown: string
  quickTaps: string
  narrowTheseDown: string
  upTo: (count: number) => string
  cancel: string
  anythingElse: string
  remove: (label: string) => string
}

const WORDS: Record<ReplyLanguage, BriefWords> = {
  en: {
    narrowDown: 'Narrow down',
    quickTaps: 'A few quick taps - skip any',
    narrowTheseDown: 'Narrow these down',
    upTo: (count) => `up to ${count}`,
    cancel: 'Cancel',
    anythingElse: 'Anything else? A colour, a size, a fabric…',
    remove: (label) => `Remove ${label}`,
  },
  ar: {
    narrowDown: 'تضييق الخيارات',
    quickTaps: 'اختيارات سريعة - تجاوز ما لا يهمك',
    narrowTheseDown: 'ضيّق هذه الخيارات',
    upTo: (count) => `حتى ${count}`,
    cancel: 'إلغاء',
    anythingElse: 'شيء آخر؟ لون، مقاس، خامة…',
    remove: (label) => `إزالة ${label}`,
  },
}

export function briefWords(language: ReplyLanguage): BriefWords {
  return WORDS[language]
}
