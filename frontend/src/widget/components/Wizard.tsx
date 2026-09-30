import type { ReactNode } from 'react'
import { Icon } from '../icons'

interface WizardProps {
  step: number
  total: number
  title: string
  children: ReactNode
  onBack: (() => void) | null
  onNext: () => void
  nextLabel: string
  nextDisabled: boolean
}

/** A few short steps, one decision each, ending in a real request. */
export function Wizard({ step, total, title, children, onBack, onNext, nextLabel, nextDisabled }: WizardProps) {
  return (
    <div className="wizard">
      <div className="steps" aria-hidden="true">
        {Array.from({ length: total }, (_, index) => (
          <i key={index} style={{ ['--fill' as string]: index < step ? '100%' : index === step ? '12%' : '0%' }} />
        ))}
      </div>
      <div className="step-head">
        <strong>{title}</strong>
        <span className="step-pill">
          Step {step + 1} of {total}
        </span>
      </div>
      <div className="step-body">{children}</div>
      <div className="step-foot">
        {onBack && (
          <button className="btn ghost" onClick={onBack}>
            <Icon name="back" size={15} /> Back
          </button>
        )}
        <button className="btn" onClick={onNext} disabled={nextDisabled}>
          {nextLabel}
        </button>
      </div>
    </div>
  )
}

export function formatAmount(amount: number): string {
  return amount.toLocaleString('en-US', { maximumFractionDigits: 0 })
}

/** A budget picker: one big figure, a slider and a few round presets. */
export function BudgetPicker({
  value,
  onChange,
  min,
  max,
  step,
  presets,
  currency,
  caption,
}: {
  value: number
  onChange: (value: number) => void
  min: number
  max: number
  step: number
  presets: number[]
  currency: string
  caption: string
}) {
  return (
    <div>
      <div className="amount">
        {currency} {formatAmount(value)}
      </div>
      <small>{caption}</small>
      <input
        className="range"
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        aria-label={caption}
      />
      <div className="range-ends">
        <span>
          {currency} {formatAmount(min)}
        </span>
        <span>
          {currency} {formatAmount(max)}
        </span>
      </div>
      <span className="field-label">Quick presets</span>
      <div className="chips">
        {presets.map((preset) => (
          <button key={preset} className="chip" aria-pressed={value === preset} onClick={() => onChange(preset)}>
            {currency} {preset >= 1000 ? `${preset / 1000}K` : preset}
          </button>
        ))}
      </div>
    </div>
  )
}
