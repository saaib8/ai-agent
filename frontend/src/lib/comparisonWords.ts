// The comparison table's own words, in the language the conversation is
// answered in. The values in its cells come from the backend as they are.

import type { ReplyLanguage } from './turnWords'

interface ComparisonWords {
  title: string
  field: string
  viewProduct: string
  listedDimensions: string
  listedCaveat: string
  notListed: string
  notListedTitle: string
  fields: Record<string, string>
  status: Record<string, string>
}

const WORDS: Record<ReplyLanguage, ComparisonWords> = {
  en: {
    title: 'Comparison',
    field: 'Field',
    viewProduct: 'View product →',
    listedDimensions: 'Listed dimensions',
    listedCaveat: 'Shown as listed; length and width may be recorded in either order.',
    notListed: 'Not listed',
    notListedTitle: 'Not listed for this product',
    fields: {},
    status: { different: 'different', same: 'same' },
  },
  ar: {
    title: 'المقارنة',
    field: 'الخاصية',
    viewProduct: '← عرض المنتج',
    listedDimensions: 'المقاسات المسجّلة',
    listedCaveat: 'كما وردت في القائمة؛ قد يُسجَّل الطول والعرض بأي ترتيب.',
    notListed: 'غير مذكور',
    notListedTitle: 'غير مذكور لهذا المنتج',
    fields: {
      price: 'السعر',
      commerce_subcategory: 'النوع',
      seating_capacity: 'عدد المقاعد',
      main_color: 'اللون',
      styles: 'الطراز',
      length: 'الطول',
      overall_width: 'العرض',
      depth: 'العمق',
      height: 'الارتفاع',
    },
    status: { different: 'مختلف', same: 'متطابق' },
  },
}

export function comparisonWords(language: ReplyLanguage): ComparisonWords {
  return WORDS[language]
}
