import { useEffect, useRef } from 'react'
import type { KeyboardEvent } from 'react'
import { GridIcon, ImageIcon, SendIcon, StopIcon } from './icons'

export const PHOTO_TYPES = 'image/jpeg,image/png,image/webp'

const MAX_CHARS = 2000

interface ComposerProps {
  value: string
  onChange: (value: string) => void
  onSend: () => void
  onStop: () => void
  /** Attach a photo to the next message. */
  onPhoto: (file: File) => void
  /** The photo attached to the next message, as a preview URL. */
  photoUrl?: string | null
  onClearPhoto?: () => void
  onOpenCatalog: () => void
  disabled: boolean
  /** A reply is generating: the send button becomes a Stop button. */
  sending: boolean
}

export function Composer({
  value,
  onChange,
  onSend,
  onStop,
  onPhoto,
  photoUrl,
  onClearPhoto,
  onOpenCatalog,
  disabled,
  sending,
}: ComposerProps) {
  const ref = useRef<HTMLTextAreaElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  // Grow with content, up to a ceiling.
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }, [value])

  // A photo on its own is a message too: the assistant asks what to do with it.
  const canSend = !disabled && (value.trim().length > 0 || Boolean(photoUrl))

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      // While generating or typing out, Enter must not slip a message past Stop.
      if (canSend) onSend()
    }
  }

  return (
    <div className="border-t border-line bg-surface px-4 pb-4 pt-3">
      <div className="mx-auto max-w-3xl">
        <div className="flex items-end gap-2">
          <input
            ref={fileRef}
            type="file"
            accept={PHOTO_TYPES}
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0]
              // Cleared so choosing the same file again still fires onChange.
              e.target.value = ''
              if (file) onPhoto(file)
            }}
          />
          <button
            onClick={onOpenCatalog}
            aria-label="Browse catalogue"
            title="Browse catalogue"
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full border border-line bg-surface text-muted shadow-card transition hover:border-clay/40 hover:text-clay focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40"
          >
            <GridIcon size={19} />
          </button>
          <button
            onClick={() => fileRef.current?.click()}
            disabled={disabled}
            aria-label="Add a photo"
            title="Add a photo"
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full border border-line bg-surface text-muted shadow-card transition hover:border-clay/40 hover:text-clay focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <ImageIcon size={19} />
          </button>
          <div className="flex flex-1 items-end gap-2 rounded-[22px] border border-line bg-surface px-3.5 shadow-card transition focus-within:border-clay/50 focus-within:ring-2 focus-within:ring-clay/15">
            {photoUrl && (
              <div className="relative my-2 shrink-0">
                <img
                  src={photoUrl}
                  alt="Photo to send"
                  className="h-10 w-10 rounded-lg border border-line object-cover"
                />
                <button
                  onClick={onClearPhoto}
                  aria-label="Remove photo"
                  title="Remove photo"
                  className="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full bg-ink text-[11px] leading-none text-white"
                >
                  ×
                </button>
              </div>
            )}
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
          {sending ? (
            <button
              onClick={onStop}
              aria-label="Stop generating"
              title="Stop"
              className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-clay text-white transition hover:bg-clay-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
            >
              <StopIcon size={18} />
            </button>
          ) : (
            <button
              onClick={onSend}
              disabled={!canSend}
              aria-label="Send message"
              className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-clay text-white transition hover:bg-clay-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-not-allowed disabled:bg-line-strong"
            >
              <SendIcon size={18} />
            </button>
          )}
        </div>
        <p className="mt-2 text-center text-[11px] leading-snug text-muted">
          ZORY recommends real products from the retailer&apos;s catalog. Verify details on the
          merchant&apos;s site. Press Enter to send · Shift+Enter for a new line · Add a photo to
          find furniture like it or see your cart in your room.
        </p>
      </div>
    </div>
  )
}
