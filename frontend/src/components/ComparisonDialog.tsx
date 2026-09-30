import { useEffect } from 'react'
import type { CardComparisonResponse } from '../api/types'
import { CloseIcon, CompareIcon } from './icons'
import { ComparisonTable } from './presentation/ComparisonTable'

export type ComparisonPopup =
  | { status: 'loading'; names: [string, string] }
  | { status: 'ready'; names: [string, string]; data: CardComparisonResponse }
  | { status: 'error'; names: [string, string]; message: string }

interface ComparisonDialogProps {
  popup: ComparisonPopup
  onClose: () => void
}

/**
 * Two checked products side by side, over the chat.
 *
 * Opened by the Compare button once two cards are checked. A look, not a
 * turn: the server compares the two cards it can see on screen and words a
 * short take, and closing leaves the conversation exactly as it was, with the
 * two still checked. Only similar products reach here - the checkboxes of
 * other kinds are disabled, and the server refuses them regardless.
 */
export function ComparisonDialog({ popup, onClose }: ComparisonDialogProps) {
  useEffect(() => {
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => {
      document.body.style.overflow = previous
      window.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center sm:items-center sm:p-4">
      <button
        type="button"
        aria-label="Close comparison"
        tabIndex={-1}
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-ink/40"
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="comparison-title"
        className="relative z-10 flex max-h-[92dvh] w-full max-w-3xl animate-rise flex-col overflow-hidden rounded-t-2xl border border-line bg-canvas shadow-soft sm:rounded-2xl"
      >
        <header className="flex shrink-0 items-center justify-between gap-3 border-b border-line bg-surface px-4 py-3 sm:px-5">
          <div className="flex min-w-0 items-center gap-3">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-clay-soft/60 text-clay">
              <CompareIcon size={18} />
            </span>
            <h2 id="comparison-title" className="truncate text-[15px] font-semibold text-ink">
              {popup.names[0]} <span className="font-normal text-muted">vs</span> {popup.names[1]}
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close comparison"
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-muted transition hover:bg-canvas hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40"
          >
            <CloseIcon size={18} />
          </button>
        </header>

        <div className="flex-1 overflow-y-auto p-4 sm:p-5">
          {popup.status === 'loading' && (
            <div className="flex items-center gap-3 py-10 text-sm text-muted">
              <span className="h-4 w-4 animate-spin rounded-full border-2 border-clay/30 border-t-clay" />
              Comparing the two…
            </div>
          )}
          {popup.status === 'error' && (
            <div className="rounded-xl border border-amber/25 bg-amber/10 px-4 py-3 text-sm text-amber">
              {popup.message}
            </div>
          )}
          {popup.status === 'ready' && (
            <div className="flex flex-col gap-4">
              <p className="rounded-2xl border border-line bg-surface px-4 py-3 text-[15px] leading-relaxed text-ink shadow-card">
                {popup.data.message}
              </p>
              <ComparisonTable comparison={popup.data.comparison} />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
