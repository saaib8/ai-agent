import { RENDER_VIEWS } from '../../api/types'
import type { RenderView } from '../../api/types'

interface ViewPickerProps {
  value: RenderView
  onChange: (view: RenderView) => void
  disabled?: boolean
  /** Each view's name in the reply's language (`ChatResponse.labels`) -
   *  English without them. */
  labels?: Record<RenderView, string>
  ariaLabel?: string
}

/** The camera views a render can use, as a segmented control. */
export function ViewPicker({
  value,
  onChange,
  disabled = false,
  labels,
  ariaLabel = 'Camera view',
}: ViewPickerProps) {
  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className="inline-flex rounded-full border border-line bg-canvas p-0.5"
    >
      {RENDER_VIEWS.map((view) => {
        const selected = view.value === value
        return (
          <button
            key={view.value}
            role="radio"
            aria-checked={selected}
            disabled={disabled}
            onClick={() => onChange(view.value)}
            className={`rounded-full px-3 py-1 text-xs font-medium transition focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 disabled:cursor-not-allowed disabled:opacity-50 ${
              selected ? 'bg-surface text-ink shadow-card' : 'text-muted hover:text-ink'
            }`}
          >
            {labels?.[view.value] ?? view.label}
          </button>
        )
      })}
    </div>
  )
}
