import type { ErrorBody } from '../api/types'

export function ErrorCard({ status, error }: { status: number | 'network'; error: ErrorBody }) {
  return (
    <div className="rounded-2xl border border-rose/40 bg-rose/5 px-4 py-3">
      <div className="flex items-center gap-2 text-xs font-semibold text-rose">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <circle cx="12" cy="12" r="10" />
          <path d="M12 8v5M12 16h.01" />
        </svg>
        <span>{status === 'network' ? 'Network' : `HTTP ${status}`}</span>
        <span className="font-mono font-normal text-rose/70">· {error.code}</span>
      </div>
      <p className="mt-1.5 text-sm text-ink">{error.message}</p>
      {error.trace_id && <p className="mt-1 font-mono text-[11px] text-muted">trace: {error.trace_id}</p>}
    </div>
  )
}
