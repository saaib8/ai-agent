import { useState } from 'react'
import { BudgetPicker, Wizard, formatAmount } from '../components/Wizard'
import { useWidget } from '../context'

/** What people shop for by budget, as approved subcategories and the words a
 *  shopper would type. Only those the store stocks are offered. */
const KINDS: { subcategory: string; label: string; plural: string }[] = [
  { subcategory: 'sofa', label: 'Sofas', plural: 'sofas' },
  { subcategory: 'bed', label: 'Beds', plural: 'beds' },
  { subcategory: 'center-table', label: 'Coffee tables', plural: 'coffee tables' },
  { subcategory: 'dining-table', label: 'Dining tables', plural: 'dining tables' },
  { subcategory: 'chair', label: 'Accent chairs', plural: 'accent chairs' },
  { subcategory: 'carpet', label: 'Rugs', plural: 'rugs' },
  { subcategory: 'floor-lamp', label: 'Floor lamps', plural: 'floor lamps' },
  { subcategory: 'wardrobe', label: 'Wardrobes', plural: 'wardrobes' },
  { subcategory: 'nightstand', label: 'Nightstands', plural: 'nightstands' },
  { subcategory: 'tv-table', label: 'TV units', plural: 'TV units' },
]

export function BudgetShop() {
  const { facets, ask } = useWidget()
  const [step, setStep] = useState(0)
  const [kind, setKind] = useState<string | null>(null)
  const [budget, setBudget] = useState(3000)
  const currency = facets?.price?.currency ?? 'SAR'
  const stocked = new Set(facets?.categories.flatMap((c) => c.subcategories.map((s) => s.value)) ?? [])
  const offered = facets ? KINDS.filter((k) => stocked.has(k.subcategory)) : KINDS
  const ceiling = Math.max(5000, Math.ceil(Number(facets?.price?.max_amount ?? 20000) / 1000) * 1000)
  const chosen = KINDS.find((k) => k.subcategory === kind)

  if (step === 0) {
    return (
      <Wizard step={0} total={2} title="What are you shopping for?" onBack={null} onNext={() => setStep(1)} nextLabel="Continue" nextDisabled={!kind}>
        <div className="options">
          {offered.map((option) => (
            <button key={option.subcategory} className="option" aria-pressed={kind === option.subcategory} onClick={() => setKind(option.subcategory)}>
              {option.label}
            </button>
          ))}
        </div>
      </Wizard>
    )
  }

  return (
    <Wizard
      step={1}
      total={2}
      title="What's your limit?"
      onBack={() => setStep(0)}
      onNext={() => chosen && ask(`Show me ${chosen.plural} under ${formatAmount(budget)} ${currency}`)}
      nextLabel={`Show ${chosen?.plural ?? 'pieces'}`}
      nextDisabled={!chosen}
    >
      <BudgetPicker
        value={Math.min(budget, ceiling)}
        onChange={setBudget}
        min={500}
        max={ceiling}
        step={250}
        presets={[1000, 2000, 3000, 5000, 8000].filter((p) => p <= ceiling)}
        currency={currency}
        caption="Most you'd like to spend"
      />
      <p className="note">I&apos;ll only show pieces within your limit.</p>
    </Wizard>
  )
}
