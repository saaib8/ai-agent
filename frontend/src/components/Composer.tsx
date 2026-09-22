import { useEffect, useRef } from 'react'
import type { KeyboardEvent } from 'react'
import { SendIcon } from './icons'

const MAX_CHARS = 2000

interface ComposerProps {
  value: string
  onChange: (value: string) => void
  onSend: () => void
  disabled: boolean
}

export function Composer({ value, onChange, onSend, disabled }: ComposerProps) {
  const ref = useRef<HTMLTextAreaElement>(null)

  // Grow with content, up to a ceiling.
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }, [value])

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      onSend()
    }
  }

  const canSend = !disabled && value.trim().length > 0

  return (
    <div className="border-t border-line bg-surface px-4 pb-4 pt-3">
      <div className="mx-auto max-w-3xl">
        <div className="flex items-end gap-2">
          <div className="flex flex-1 items-end rounded-[22px] border border-line bg-surface px-3.5 shadow-card transition focus-within:border-clay/50 focus-within:ring-2 focus-within:ring-clay/15">
            <textarea
              ref={ref}
              value={value}
              onChange={(e) => onChange(e.target.value.slice(0, MAX_CHARS))}
              onKeyDown={handleKeyDown}
              rows={1}
              aria-label="Message ZORY"
              placeholder="Message ZORY…"
              className="max-h-40 w-full resize-none bg-transparent py-3 text-[15px] text-ink placeholder:text-muted/60 focus:outline-none"
            />
          </div>
          <button
            onClick={onSend}
            disabled={!canSend}
            aria-label="Send message"
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-clay text-white transition hover:bg-clay-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-not-allowed disabled:bg-line-strong"
          >
            <SendIcon size={18} />
          </button>
        </div>
        <p className="mt-2 text-center text-[11px] leading-snug text-muted">
          ZORY recommends real products from the retailer&apos;s catalog. Verify details on the
          merchant&apos;s site. Press Enter to send · Shift+Enter for a new line.
        </p>
      </div>
    </div>
  )
}
