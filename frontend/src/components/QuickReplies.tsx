import type { QuickReply } from '../lib/quickReplies'

interface QuickRepliesProps {
  replies: QuickReply[]
  onPick: (value: string) => void
  disabled: boolean
}

export function QuickReplies({ replies, onPick, disabled }: QuickRepliesProps) {
  if (replies.length === 0) return null
  return (
    <div className="flex animate-rise flex-wrap items-center gap-2 pt-0.5">
      <span className="text-[11px] font-medium uppercase tracking-wide text-muted">Quick reply</span>
      {replies.map((reply) => (
        <button
          key={reply.value}
          onClick={() => onPick(reply.value)}
          disabled={disabled}
          className="rounded-full border border-clay/30 bg-surface px-3.5 py-1.5 text-sm font-medium text-clay transition hover:border-clay hover:bg-clay hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {reply.label}
        </button>
      ))}
    </div>
  )
}
