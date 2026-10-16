import { useState } from 'react'
import type { CardLabels, CartLineView, CartView, PickView } from '../api/types'
import { cardWords, type CardWords } from '../lib/cardWords'
import { money } from '../lib/format'
import { productText } from '../lib/productWords'
import { CloseIcon, MinusIcon, PlusIcon, SparkIcon } from './icons'

/** What the tray's actions name a line by: its place in the cart (the same
 *  number as its pick), and its English name for the message a tap sends. */
export type CartLineRef = Pick<PickView, 'pick' | 'name_english'>

interface PicksTrayProps {
  picks: PickView[]
  /** The cart as the server last reported it; null until a reply or a tick
   *  carries one, when the picks are drawn without quantities. */
  cart: CartView | null
  /** A reply or a tick is in flight: actions wait for it. */
  busy: boolean
  /** Why the last tick or quantity change was refused, in the server's words. */
  error: string | null
  onRemove: (line: CartLineRef) => void
  onGoesWith: (line: CartLineRef) => void
  /** How many of a line, from now on (at least one; ✕ removes it). */
  onQuantity: (line: CartLineRef, quantity: number) => void
  /** The conversation's latest words (`ChatResponse.labels`); each line is
   *  named as the server worded it (`display`). */
  labels?: CardLabels | null
}

/** One line of the tray, from the cart - or from a pick before any cart came. */
interface TrayLine extends CartLineRef {
  image_url: string
  /** Its name as the conversation reads it. */
  name: string
  /** One piece's price. */
  price: string | null
  /** Null for a pick drawn before the server reported a cart: no quantity is
   *  guessed, so no stepper and no line total. */
  quantity: number | null
  lineTotal: string | null
  focused: boolean
}

function fromCart(line: CartLineView, focused: boolean): TrayLine {
  const text = productText({
    name_english: line.name_english,
    price_amount: String(line.unit_price),
    price_unit: line.price_unit,
    display: line.display,
  })
  return {
    pick: line.pick,
    name_english: line.name_english,
    image_url: line.image_url,
    name: text.name,
    price: text.price,
    quantity: line.quantity,
    lineTotal: line.line_total_display ?? money(String(line.line_total), line.price_unit),
    focused,
  }
}

function fromPick(pick: PickView): TrayLine {
  const text = productText(pick)
  return {
    pick: pick.pick,
    name_english: pick.name_english,
    image_url: pick.image_url,
    name: text.name,
    price: text.price,
    quantity: null,
    lineTotal: null,
    focused: pick.focused,
  }
}

/**
 * The customer's cart - their picks with how many of each - above the message
 * box, kept across searches.
 *
 * Drawn from what the server reports, never from local guesses, so a typed
 * "I'll take the second one", an "Add to cart" tap and a whole room added at
 * once land in the same tray. Each line's quantity can be stepped, and any line
 * can show what goes with it. Comparing is not done here: it is similar cards
 * checked on the results.
 */
export function PicksTray({
  picks,
  cart,
  busy,
  error,
  onRemove,
  onGoesWith,
  onQuantity,
  labels,
}: PicksTrayProps) {
  const lines = cart
    ? cart.lines.map((line) =>
        fromCart(line, picks.some((p) => p.pick === line.pick && p.focused)),
      )
    : picks.map(fromPick)
  if (lines.length === 0 && picks.length === 0) return error ? <TrayError message={error} /> : null

  const words = cardWords(labels).tray
  return (
    <div className="border-t border-line bg-canvas/70 px-4 pt-2.5">
      <div className="mx-auto max-w-3xl">
        {error && <TrayError message={error} />}
        <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted">
          {words.yourPicks(cart?.item_count ?? picks.length)}
        </div>
        {lines.length === 0 ? (
          <div className="pb-2.5 text-xs text-muted">{words.empty}</div>
        ) : (
          <div className="flex gap-2 overflow-x-auto pb-2.5 [scrollbar-width:thin]">
            {lines.map((line) => (
              <CartTile
                key={line.pick}
                line={line}
                busy={busy}
                words={words}
                onGoesWith={() => onGoesWith(line)}
                onRemove={() => onRemove(line)}
                onQuantity={(quantity) => onQuantity(line, quantity)}
              />
            ))}
          </div>
        )}
        {cart && cart.totals.length > 0 && (
          <div className="flex flex-col items-end gap-0.5 pb-2.5">
            {cart.totals.map((total) => (
              <div key={total.price_unit} className="text-sm font-semibold text-ink" dir="auto">
                {words.total(total.display ?? money(String(total.amount), total.price_unit) ?? '')}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function CartTile({
  line,
  busy,
  words,
  onGoesWith,
  onRemove,
  onQuantity,
}: {
  line: TrayLine
  busy: boolean
  words: CardWords['tray']
  onGoesWith: () => void
  onRemove: () => void
  onQuantity: (quantity: number) => void
}) {
  const [imgFailed, setImgFailed] = useState(false)
  const { name } = line
  return (
    // The ring marks the pick "it" refers to: the last one ticked or asked
    // about. A tick is silent, so it is not labelled as a question.
    <div
      title={line.focused ? words.focused : undefined}
      className={`flex w-[19rem] shrink-0 flex-col gap-2 rounded-xl border bg-surface p-2 shadow-card ${
        line.focused ? 'border-clay ring-1 ring-clay/25' : 'border-line'
      }`}
    >
      <div className="flex items-center gap-2.5">
        <div className="h-11 w-11 shrink-0 overflow-hidden rounded-lg bg-canvas">
          {!imgFailed && line.image_url ? (
            <img
              src={line.image_url}
              alt=""
              onError={() => setImgFailed(true)}
              className="h-full w-full object-cover"
            />
          ) : null}
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs font-medium text-ink" title={name} dir="auto">
            {name}
          </div>
          <div className="text-[11px] text-muted" dir="auto">
            {line.price}
          </div>
        </div>
        {line.lineTotal && (
          <div className="shrink-0 text-xs font-semibold text-ink" dir="auto">
            {line.lineTotal}
          </div>
        )}
        <button
          onClick={onRemove}
          disabled={busy}
          aria-label={words.removeName(name)}
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-muted transition hover:bg-canvas hover:text-rose disabled:cursor-not-allowed disabled:opacity-50"
        >
          <CloseIcon size={13} />
        </button>
      </div>
      <div className="flex items-center justify-between gap-2">
        {line.quantity != null ? (
          <CartQuantity
            value={line.quantity}
            name={name}
            busy={busy}
            words={words}
            onChange={onQuantity}
          />
        ) : (
          <span />
        )}
        <button
          onClick={onGoesWith}
          disabled={busy}
          aria-label={words.goesWithName(name)}
          title={words.goesWithTitle}
          className="inline-flex shrink-0 items-center gap-1 rounded-full border border-clay/30 px-2 py-1 text-[11px] font-semibold text-clay transition hover:border-clay hover:bg-clay hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <SparkIcon size={12} />
          {words.goesWith}
        </button>
      </div>
    </div>
  )
}

/** − n + for one line. Never below one - ✕ removes a line - and never past
 *  the server's limit, which refuses the step in its own words. */
function CartQuantity({
  value,
  name,
  busy,
  words,
  onChange,
}: {
  value: number
  name: string
  busy: boolean
  words: CardWords['tray']
  onChange: (value: number) => void
}) {
  const button =
    'flex h-6 w-6 items-center justify-center rounded-full text-ink transition hover:bg-canvas focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/30 disabled:cursor-not-allowed disabled:opacity-40'
  return (
    <div className="flex items-center gap-1.5">
      <span aria-hidden className="text-[11px] text-muted">
        {words.quantity}
      </span>
      <div
        role="group"
        aria-label={words.quantity}
        className="inline-flex items-center rounded-full border border-line bg-surface"
      >
        <button
          type="button"
          onClick={() => onChange(value - 1)}
          disabled={busy || value <= 1}
          aria-label={words.decreaseName(name)}
          className={button}
        >
          <MinusIcon size={12} />
        </button>
        <span
          className="min-w-5 text-center text-xs font-semibold tabular-nums text-ink"
          aria-live="polite"
        >
          {value}
        </span>
        <button
          type="button"
          onClick={() => onChange(value + 1)}
          disabled={busy}
          aria-label={words.increaseName(name)}
          className={button}
        >
          <PlusIcon size={12} />
        </button>
      </div>
    </div>
  )
}

function TrayError({ message }: { message: string }) {
  return (
    <div className="mx-auto mb-2 max-w-3xl rounded-lg border border-amber/25 bg-amber/10 px-3 py-1.5 text-xs text-amber">
      {message}
    </div>
  )
}
