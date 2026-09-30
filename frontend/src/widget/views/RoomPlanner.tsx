import { useEffect, useRef, useState } from 'react'
import { getCatalogProducts } from '../../api/client'
import { humanise } from '../../lib/format'
import { ProductImage } from '../components/ProductCard'
import { BudgetPicker, Wizard, formatAmount } from '../components/Wizard'
import { useWidget } from '../context'
import { Icon } from '../icons'
import type { IconName } from '../icons'

const ROOMS: { value: string; label: string; icon: IconName }[] = [
  { value: 'living room', label: 'Living Room', icon: 'home' },
  { value: 'bedroom', label: 'Bedroom', icon: 'bed' },
  { value: 'dining room', label: 'Dining Room', icon: 'dining' },
  { value: 'home office', label: 'Home Office', icon: 'office' },
]

const NO_PREFERENCE = '__none__'

/**
 * "Design my space": room, total budget and style, then one message to the
 * agent. The agent's own room flow takes it from there — it asks which pieces
 * (as chips), how many people will sit and the colours, one question per turn,
 * then builds the room from real products within the budget.
 */
export function RoomPlanner() {
  const { config, facets, ask } = useWidget()
  const [step, setStep] = useState(0)
  const [room, setRoom] = useState<string | null>(null)
  const [budget, setBudget] = useState(15000)
  const [style, setStyle] = useState<string | null>(null)
  const currency = facets?.price?.currency ?? 'SAR'
  const styles = (facets?.styles ?? []).filter((s) => s.count >= 3).slice(0, 6).map((s) => s.value)
  const images = useStyleImages(config.apiBase, config.storeId, step === 2 ? styles : [])

  const submit = () => {
    const wanted = style && style !== NO_PREFERENCE ? ` I'd like a ${humanise(style)} style.` : ''
    ask(`Design my ${room}. My total budget is ${formatAmount(budget)} ${currency}.${wanted}`)
  }

  if (step === 0) {
    return (
      <Wizard step={0} total={3} title="Which room?" onBack={null} onNext={() => setStep(1)} nextLabel="Continue" nextDisabled={!room}>
        <div className="options">
          {ROOMS.map((option) => (
            <button key={option.value} className="option" aria-pressed={room === option.value} onClick={() => setRoom(option.value)}>
              <Icon name={option.icon} size={22} />
              {option.label}
            </button>
          ))}
        </div>
      </Wizard>
    )
  }

  if (step === 1) {
    return (
      <Wizard step={1} total={3} title="What's your budget?" onBack={() => setStep(0)} onNext={() => setStep(2)} nextLabel="Continue" nextDisabled={false}>
        <BudgetPicker
          value={budget}
          onChange={setBudget}
          min={3000}
          max={50000}
          step={500}
          presets={[5000, 10000, 15000, 25000, 50000]}
          currency={currency}
          caption="Total room budget"
        />
      </Wizard>
    )
  }

  return (
    <Wizard step={2} total={3} title="Pick a style" onBack={() => setStep(1)} onNext={submit} nextLabel="Create room plan" nextDisabled={!style}>
      <div className="options" style={{ gridTemplateColumns: 'repeat(2, minmax(0, 1fr))' }}>
        {styles.map((value) => (
          <button key={value} className="style-card" aria-pressed={style === value} onClick={() => setStyle(value)}>
            <div className="img">{images[value] !== undefined && <ProductImage src={images[value] ?? ''} alt="" />}</div>
            <span>{humanise(value)}</span>
            {style === value && (
              <i className="tick">
                <Icon name="check" size={13} strokeWidth={2.4} />
              </i>
            )}
          </button>
        ))}
        <button className="option" aria-pressed={style === NO_PREFERENCE} onClick={() => setStyle(NO_PREFERENCE)}>
          <Icon name="spark" size={20} />
          No preference
        </button>
      </div>
      {styles.length === 0 && <p className="note">Loading the store&apos;s styles…</p>}
    </Wizard>
  )
}

/** A real sofa photo per style, so a style card shows what the store actually
 *  sells in it. A few are read per style and each card takes the first not
 *  already on another card — many products carry several styles. A failed
 *  read leaves the card without a picture. */
function useStyleImages(apiBase: string, storeId: number, styles: string[]): Record<string, string | null> {
  const [candidates, setCandidates] = useState<Record<string, string[]>>({})
  const requested = useRef(new Set<string>())
  const key = styles.join('|')
  useEffect(() => {
    if (!key) return
    for (const style of key.split('|')) {
      if (requested.current.has(style)) continue
      requested.current.add(style)
      void getCatalogProducts(apiBase, storeId, {
        style,
        category: 'seating',
        subcategory: 'sofa',
        page_size: 6,
      }).then((result) => {
        const urls = result.ok ? result.data.items.map((item) => item.image_url).filter(Boolean) : []
        setCandidates((current) => ({ ...current, [style]: urls }))
      })
    }
  }, [apiBase, key, storeId])

  const images: Record<string, string | null> = {}
  const used = new Set<string>()
  for (const style of styles) {
    const options = candidates[style]
    if (!options) continue
    const pick = options.find((url) => !used.has(url)) ?? options[0] ?? null
    if (pick) used.add(pick)
    images[style] = pick
  }
  return images
}
