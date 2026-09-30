import type { BundleAction, ProductAction } from '../api/types'
import type { QuickReply } from '../lib/quickReplies'

interface QuickRepliesProps {
  replies: QuickReply[]
  /** The reply's words, and the action it runs when it carries one - a product
   *  action, or a room edit for the yes/no on an over-budget swap. */
  onPick: (value: string, action?: ProductAction | null, bundle?: BundleAction | null) => void
  disabled: boolean
  /** What the row is. Defaults to "Quick reply". */
  label?: string
}

export function QuickReplies({
  replies,
  onPick,
  disabled,
  label = 'Quick reply',
}: QuickRepliesProps) {
  if (replies.length === 0) return null
  return (
    <div className="flex animate-rise flex-wrap items-center gap-2 pt-0.5">
      <span className="text-[11px] font-medium uppercase tracking-wide text-muted">{label}</span>
      {replies.map((reply) => (
        <button
          key={reply.value}
          onClick={() => onPick(reply.value, reply.product_action, reply.bundle_action)}
          disabled={disabled}
          className="rounded-full border border-clay/30 bg-surface px-3.5 py-1.5 text-sm font-medium text-clay transition hover:border-clay hover:bg-clay hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {reply.label}
        </button>
      ))}
    </div>
  )
}
