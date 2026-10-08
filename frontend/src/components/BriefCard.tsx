import { useState } from 'react'
import type { BriefAnswerAction, BriefQuestion, ProductBrief } from '../api/types'
import { SlidersIcon, SparkIcon } from './icons'

interface BriefCardProps {
  brief: ProductBrief
  /** Only the latest turn's card can be answered; older ones stay as a record. */
  active: boolean
  busy: boolean
  /** The answers as the keys the card offered, and words for the chat thread. */
  onSubmit: (answer: BriefAnswerAction, summary: string) => void
}

type Picked = Record<string, string[]>

/**
 * A card of short questions for a product search - the kind, budget, colours,
 * the feel, the style - answered by tapping and sent together.
 *
 * Every chip is a choice the server offered and leads to real products; the
 * card sends back only their keys. Anything left blank is simply not asked
 * about, so sending an empty card shows what they first asked for. Beside
 * results it starts folded, as a way to narrow them down.
 */
export function BriefCard({ brief, active, busy, onSubmit }: BriefCardProps) {
  const [open, setOpen] = useState(brief.mode === 'ask')
  const [picked, setPicked] = useState<Picked>({})
  const [sent, setSent] = useState(false)
  const disabled = !active || busy || sent

  const toggle = (question: BriefQuestion, key: string) =>
    setPicked((current) => {
      const chosen = current[question.kind] ?? []
      if (chosen.includes(key)) {
        return { ...current, [question.kind]: chosen.filter((k) => k !== key) }
      }
      // One answer replaces the last; several keep the newest few.
      const next =
        question.max_choices === 1 ? [key] : [...chosen, key].slice(-question.max_choices)
      return { ...current, [question.kind]: next }
    })

  const labelOf = (question: BriefQuestion, key: string) =>
    question.choices.find((c) => c.key === key)?.label ?? key
  const summaryParts = brief.questions.flatMap((q) => {
    const chosen = picked[q.kind] ?? []
    return chosen.length ? [chosen.map((k) => labelOf(q, k)).join(' or ')] : []
  })
  const anything = summaryParts.length > 0

  const submit = () => {
    const first = (kind: string) => (picked[kind] ?? [])[0] ?? null
    setSent(true)
    onSubmit(
      {
        kind: 'brief',
        card: brief.card,
        piece: first('type'),
        budget: first('budget'),
        space: first('space'),
        colours: picked.colour ?? [],
        styles: picked.style ?? [],
        feel: first('feel'),
      },
      anything ? summaryParts.join(' · ') : `Just ${brief.submit_label.toLowerCase()}`,
    )
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        disabled={disabled}
        className="inline-flex w-fit items-center gap-2 rounded-full border border-clay/30 bg-surface px-3.5 py-1.5 text-sm font-medium text-clay shadow-card transition hover:border-clay hover:bg-clay hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
      >
        <SlidersIcon size={15} />
        Narrow down
      </button>
    )
  }

  return (
    <div className="flex max-w-3xl animate-rise flex-col gap-3.5 rounded-2xl border border-line bg-surface p-4 shadow-card">
      {brief.mode === 'ask' ? (
        <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-clay">
          <SparkIcon size={14} />
          A few quick taps - skip any
        </div>
      ) : (
        <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-clay">
          <SlidersIcon size={14} />
          Narrow these down
        </div>
      )}
      {brief.questions.map((question) => {
        const chosen = picked[question.kind] ?? []
        return (
          <div key={question.kind} className="flex flex-col gap-1.5">
            <div className="text-xs font-medium text-muted">
              {question.label}
              {question.max_choices > 1 && (
                <span className="font-normal"> · up to {question.max_choices}</span>
              )}
            </div>
            <div className="flex flex-wrap gap-1.5">
              {question.choices.map((choice) => {
                const on = chosen.includes(choice.key)
                return (
                  <button
                    key={choice.key}
                    onClick={() => toggle(question, choice.key)}
                    disabled={disabled}
                    aria-pressed={on}
                    className={
                      'rounded-full border px-3 py-1 text-[13px] font-medium transition focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 disabled:cursor-not-allowed ' +
                      (on
                        ? 'border-clay bg-clay text-white'
                        : 'border-line bg-surface text-ink/80 hover:border-clay/50 hover:text-ink disabled:opacity-60')
                    }
                  >
                    {choice.label}
                  </button>
                )
              })}
            </div>
          </div>
        )
      })}
      <div className="flex flex-wrap items-center gap-2 pt-0.5">
        <button
          onClick={submit}
          disabled={disabled}
          className="rounded-full bg-ink px-4 py-1.5 text-sm font-medium text-white transition hover:bg-ink/85 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {anything ? brief.submit_label : `Just ${brief.submit_label.toLowerCase()}`}
        </button>
        {brief.mode === 'narrow' && !sent && (
          <button
            onClick={() => setOpen(false)}
            disabled={disabled}
            className="rounded-full px-3 py-1.5 text-sm font-medium text-muted transition hover:text-ink disabled:opacity-50"
          >
            Cancel
          </button>
        )}
      </div>
    </div>
  )
}
