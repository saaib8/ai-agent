import { useState } from 'react'
import type { BriefAnswerAction, BriefQuestion, ProductBrief } from '../api/types'
import { briefWords } from '../lib/briefWords'
import type { ReplyLanguage } from '../lib/turnWords'
import { SlidersIcon, SparkIcon } from './icons'

interface BriefCardProps {
  brief: ProductBrief
  language: ReplyLanguage
  /** Only the latest turn's card can be answered; older ones stay as a record. */
  active: boolean
  busy: boolean
  /** The answers as the keys the card offered, and words for the chat thread -
   *  or, when they typed something more, no answer and all of it as words. */
  onSubmit: (answer: BriefAnswerAction | null, summary: string) => void
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
export function BriefCard({ brief, language, active, busy, onSubmit }: BriefCardProps) {
  const words = briefWords(language)
  const [open, setOpen] = useState(brief.mode === 'ask')
  // Narrow down opens showing what the search on screen already uses.
  const [picked, setPicked] = useState<Picked>(() =>
    Object.fromEntries(
      brief.questions.flatMap((q) => (q.selected?.length ? [[q.kind, q.selected]] : [])),
    ),
  )
  const [extra, setExtra] = useState('')
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
    const typed = extra.trim()
    if (typed) {
      // Words only the agent can read: the taps go with them, as words.
      onSubmit(null, [...summaryParts, typed].join(' · '))
      return
    }
    onSubmit(
      {
        kind: 'brief',
        card: brief.card,
        piece: first('type'),
        room: first('room'),
        people: first('people'),
        budget: first('budget'),
        space: first('space'),
        colours: picked.colour ?? [],
        styles: picked.style ?? [],
        feel: first('feel'),
      },
      anything ? summaryParts.join(' · ') : brief.skip_label,
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
        {words.narrowDown}
      </button>
    )
  }

  return (
    <div className="flex max-w-3xl animate-rise flex-col gap-3.5 rounded-2xl border border-line bg-surface p-4 shadow-card">
      {brief.mode === 'ask' ? (
        <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-clay">
          <SparkIcon size={14} />
          {words.quickTaps}
        </div>
      ) : (
        <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-clay">
          <SlidersIcon size={14} />
          {words.narrowTheseDown}
        </div>
      )}
      {brief.questions.map((question) => {
        const chosen = picked[question.kind] ?? []
        return (
          <div key={question.kind} className="flex flex-col gap-1.5">
            <div className="text-xs font-medium text-muted">
              {question.label}
              {question.max_choices > 1 && (
                <span className="font-normal"> · {words.upTo(question.max_choices)}</span>
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
      {brief.mode === 'narrow' && (
        <input
          value={extra}
          onChange={(event) => setExtra(event.target.value)}
          onKeyDown={(event) => event.key === 'Enter' && !disabled && submit()}
          disabled={disabled}
          maxLength={200}
          placeholder={words.anythingElse}
          className="w-full rounded-xl border border-line bg-canvas/60 px-3 py-2 text-sm text-ink placeholder:text-muted focus:border-clay/50 focus:outline-none disabled:opacity-60"
        />
      )}
      <div className="flex flex-wrap items-center gap-2 pt-0.5">
        <button
          onClick={submit}
          disabled={disabled}
          className="rounded-full bg-ink px-4 py-1.5 text-sm font-medium text-white transition hover:bg-ink/85 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {anything || extra.trim() ? brief.submit_label : brief.skip_label}
        </button>
        {brief.mode === 'narrow' && !sent && (
          <button
            onClick={() => setOpen(false)}
            disabled={disabled}
            className="rounded-full px-3 py-1.5 text-sm font-medium text-muted transition hover:text-ink disabled:opacity-50"
          >
            {words.cancel}
          </button>
        )}
      </div>
    </div>
  )
}
