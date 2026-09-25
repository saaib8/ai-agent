import { MinusIcon, PlusIcon } from '../icons'

interface QuantityStepperProps {
  value: number
  max: number
  /** Called with 0 when the customer steps below one: the piece is removed. */
  onChange: (value: number) => void
  label: string
  size?: 'sm' | 'md'
}

/** − n + for one product's quantity. Stepping below one removes it. */
export function QuantityStepper({ value, max, onChange, label, size = 'md' }: QuantityStepperProps) {
  const box = size === 'sm' ? 'h-7 w-7' : 'h-8 w-8'
  const button = `flex ${box} items-center justify-center rounded-full text-ink transition hover:bg-surface-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 disabled:cursor-not-allowed disabled:opacity-40`
  return (
    <div
      role="group"
      aria-label={`Quantity of ${label}`}
      className="inline-flex items-center rounded-full border border-line bg-surface"
    >
      <button
        type="button"
        onClick={() => onChange(value - 1)}
        aria-label={value === 1 ? `Remove ${label}` : `One fewer ${label}`}
        className={button}
      >
        <MinusIcon size={14} />
      </button>
      <span className="min-w-6 text-center text-sm font-semibold tabular-nums text-ink" aria-live="polite">
        {value}
      </span>
      <button
        type="button"
        onClick={() => onChange(value + 1)}
        disabled={value >= max}
        aria-label={`One more ${label}`}
        title={value >= max ? `At most ${max} of one product` : undefined}
        className={button}
      >
        <PlusIcon size={14} />
      </button>
    </div>
  )
}
