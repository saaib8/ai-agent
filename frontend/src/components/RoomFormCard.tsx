import { useMemo, useState } from 'react'
import type { CardLabels, RoomForm, RoomFormAction, RoomFormQuestion } from '../api/types'
import { ENGLISH } from '../lib/cardWords'

interface RoomFormCardProps {
  form: RoomForm
  labels: CardLabels | null | undefined
  /** Only the newest reply's form can be sent. */
  active: boolean
  busy: boolean
  /** The answers, and the customer's words for the chat thread. */
  onSubmit: (answer: RoomFormAction, summary: string) => void
}

/** The room form beneath Nora's reply: budget (required), size, colours and
 *  style - chips plus a box to type (docs/room-form-plan.md). */
export function RoomFormCard({ form, labels, active, busy, onSubmit }: RoomFormCardProps) {
  const words = labels ?? ENGLISH
  const [picked, setPicked] = useState<Record<string, string[]>>({})
  const [typed, setTyped] = useState<Record<string, string>>({})
  const [sides, setSides] = useState<{ length: string; width: string }>({ length: '', width: '' })
  const [needBudget, setNeedBudget] = useState(!!form.budget_missing)
  const [sent, setSent] = useState<string | null>(null)
  const disabled = !active || busy || sent !== null

  const budgetQuestion = useMemo(() => form.questions.find((q) => q.kind === 'budget'), [form])

  const toggle = (question: RoomFormQuestion, key: string) => {
    setPicked((current) => {
      const chosen = current[question.kind] ?? []
      if (chosen.includes(key)) return { ...current, [question.kind]: chosen.filter((k) => k !== key) }
      const next = question.max_choices > 1 ? [...chosen, key].slice(-question.max_choices) : [key]
      return { ...current, [question.kind]: next }
    })
    if (question.kind === 'budget') setNeedBudget(false)
    // A size chip and the size boxes are one answer: the newest wins.
    if (question.kind === 'size') setSides({ length: '', width: '' })
  }

  const submit = () => {
    const budget = picked.budget?.[0] ?? null
    const budgetText = typed.budget?.trim() || null
    if (budgetQuestion?.required && !budget && !budgetText) {
      setNeedBudget(true)
      return
    }
    const length = Number(sides.length) > 0 ? Number(sides.length) : null
    const width = Number(sides.width) > 0 ? Number(sides.width) : null
    const sized = length !== null && width !== null
    const answer: RoomFormAction = {
      form: form.number,
      budget,
      budget_text: budget ? null : budgetText,
      size: picked.size?.[0] ?? null,
      length_m: picked.size?.[0] || !sized ? null : length,
      width_m: picked.size?.[0] || !sized ? null : width,
      colours: picked.colours ?? [],
      styles: picked.style ?? [],
      look_text: typed.style?.trim() || null,
    }
    const parts = form.questions.map((q) => {
      const labelsOf = (picked[q.kind] ?? []).map((k) => q.choices.find((c) => c.key === k)?.label ?? k)
      const text = typed[q.kind]?.trim()
      const size =
        q.kind === 'size' && sized && !picked.size?.[0] && q.dimensions
          ? [`${length} × ${width} ${q.dimensions.unit}`]
          : []
      return { kind: q.kind, said: [...labelsOf, ...size, ...(text ? [text] : [])] }
    })
    // Only about the form's own questions: a blank size and look are left to
    // Nora, and never read as "skip the rest" (docs/room-form-plan.md).
    const lookOrSize = parts.some((p) => p.kind !== 'budget' && p.said.length > 0)
    const asked = parts.some((p) => p.kind !== 'budget')
    const summary = [
      ...parts.flatMap((p) => p.said),
      ...(asked && !lookOrSize ? [words.room_form_no_preference] : []),
    ].join(' · ')
    setSent(summary || words.room_form_no_preference)
    onSubmit(answer, summary || words.room_form_no_preference)
  }

  // Once sent, the form folds into one line of what was chosen. An older
  // form never sent from this view says nothing: the reply below it does.
  if (sent !== null || !active) {
    if (sent === null) return null
    return (
      <div className="flex w-fit max-w-3xl items-center gap-2 rounded-full border border-line bg-surface px-3.5 py-1.5 text-sm text-ink/80 shadow-card">
        <span className="text-clay" aria-hidden="true">
          ✓
        </span>
        {sent}
      </div>
    )
  }

  return (
    <div className="flex max-w-3xl animate-rise flex-col gap-3.5 rounded-2xl border border-line bg-surface p-4 shadow-card">
      {form.questions.map((question) => {
        const chosen = picked[question.kind] ?? []
        const marked = question.kind === 'budget' && needBudget
        return (
          <div key={question.kind} className="flex flex-col gap-1.5">
            <div className={'text-xs font-medium ' + (marked ? 'text-red-600' : 'text-muted')}>
              {question.label}
              {question.required && ' *'}
              {question.max_choices > 1 && (
                <span className="font-normal">
                  {' · '}
                  {words.room_form_up_to.replace('{max}', String(question.max_choices))}
                </span>
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
                      'max-w-full rounded-full border px-3 py-1 text-start text-[13px] font-medium leading-snug transition focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 disabled:cursor-not-allowed ' +
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
            {question.dimensions && (
              <div className="flex flex-wrap gap-3">
                {(['length', 'width'] as const).map((side) => (
                  <label key={side} className="flex flex-col gap-1 text-xs text-muted">
                    {side === 'length' ? question.dimensions!.length_label : question.dimensions!.width_label}
                    <div className="flex items-center gap-1.5">
                      <input
                        type="number"
                        inputMode="decimal"
                        step={Number(question.dimensions!.step)}
                        min={Number(question.dimensions!.minimum)}
                        max={Number(question.dimensions!.maximum)}
                        value={sides[side]}
                        onChange={(event) => {
                          setSides((current) => ({ ...current, [side]: event.target.value }))
                          setPicked((current) => ({ ...current, size: [] }))
                        }}
                        disabled={disabled}
                        className="w-24 rounded-xl border border-line bg-canvas/60 px-3 py-1.5 text-sm text-ink focus:border-clay/50 focus:outline-none disabled:opacity-60"
                      />
                      <span className="text-sm text-muted">{question.dimensions!.unit}</span>
                    </div>
                  </label>
                ))}
              </div>
            )}
            {question.placeholder && (
              <input
                value={typed[question.kind] ?? ''}
                onChange={(event) => {
                  setTyped((current) => ({ ...current, [question.kind]: event.target.value }))
                  if (question.kind === 'budget') setNeedBudget(false)
                }}
                onKeyDown={(event) => event.key === 'Enter' && !disabled && submit()}
                disabled={disabled}
                maxLength={200}
                placeholder={question.placeholder}
                className="w-full max-w-xs rounded-xl border border-line bg-canvas/60 px-3 py-1.5 text-sm text-ink placeholder:text-muted focus:border-clay/50 focus:outline-none disabled:opacity-60"
              />
            )}
            {marked && <div className="text-xs text-red-600">{form.budget_needed}</div>}
          </div>
        )
      })}
      <div className="pt-0.5">
        <button
          onClick={submit}
          disabled={disabled}
          className="rounded-full bg-ink px-4 py-1.5 text-sm font-medium text-white transition hover:bg-ink/85 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {form.submit_label}
        </button>
      </div>
    </div>
  )
}
