import type { SupportPresentation } from '../api/types'
import { MailIcon, WhatsAppIcon } from './icons'
import { supportLinks } from '../lib/supportLinks'

const ICONS = {
  whatsapp: { Icon: WhatsAppIcon, tint: 'hover:border-sage hover:text-sage' },
  email: { Icon: MailIcon, tint: 'hover:border-clay hover:text-clay' },
} as const

/** The store's support team, one small icon per channel the store set:
 *  WhatsApp opens a chat with its number, Email a message to its address. */
export function SupportChannels({ support }: { support?: SupportPresentation | null }) {
  const links = supportLinks(support)
  if (links.length === 0) return null
  return (
    <div className="flex items-center gap-2" role="group" aria-label="Contact the store's support">
      {links.map(({ channel, label, url }) => {
        const { Icon, tint } = ICONS[channel]
        return (
          <a
            key={channel}
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            title={label}
            aria-label={label}
            className={`flex h-9 w-9 items-center justify-center rounded-full border border-line bg-surface text-ink shadow-card transition ${tint}`}
          >
            <Icon size={18} />
          </a>
        )
      })}
    </div>
  )
}
