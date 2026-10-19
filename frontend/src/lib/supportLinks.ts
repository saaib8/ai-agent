// The store's support channels a reply may be drawn with. The server builds
// each link from the contacts the store's owner set; this keeps only links of
// the shape their channel promises, so a malformed one is never opened.

import type { SupportChannelView, SupportPresentation } from '../api/types'

const SCHEMES: Record<string, string> = {
  whatsapp: 'https://wa.me/',
  email: 'mailto:',
}

/** The channels to draw, in the server's order; none when nothing is safe. */
export function supportLinks(support: SupportPresentation | null | undefined): SupportChannelView[] {
  return (support?.channels ?? []).filter((c) => {
    const scheme = SCHEMES[c.channel]
    return !!scheme && c.url.startsWith(scheme) && c.url.length > scheme.length
  })
}
