import { useState } from 'react'
import { RENDER_VIEWS } from '../../api/types'
import type {
  BriefQuestion,
  ChatResponse,
  GroundedBundlePresentation,
  PiecePickerData,
  ProductBrief,
  ProductComparisonResult,
  RenderView,
  RoomRenderPresentation,
} from '../../api/types'
import { humanise, money, toNumber } from '../../lib/format'
import { useWidget } from '../context'
import { Icon } from '../icons'
import { Avatar } from './Avatar'
import { ProductCard, ProductImage } from './ProductCard'

interface AssistantTurnProps {
  data: ChatResponse
  /** The newest assistant turn: only it offers pickers and "show more". */
  latest: boolean
  /** The newest room package in the thread: only it can be swapped or drawn. */
  currentRoom: boolean
  renderDropped?: boolean
}

export function AssistantTurn({ data, latest, currentRoom, renderDropped }: AssistantTurnProps) {
  const { config, agent } = useWidget()
  const { response, presentation: p } = data
  const products = p?.products ?? []
  const isResults = p?.product_source === 'search'
  const listRevision = isResults ? (p?.list_revision ?? null) : null
  const choices = p?.choices ?? []
  const textChoices = choices.filter((c) => !c.product_action)
  const actionChoices = choices.filter((c) => c.product_action)
  const busy = agent.sending || agent.picking

  return (
    <div className="row-bot">
      <Avatar name={config.assistantName} url={config.avatarUrl} size={28} />
      <div className="bot-stack">
        <div className="bubble-bot">
          {response.message}
          {response.follow_up_question && <span className="follow">{response.follow_up_question}</span>}
        </div>

        {/* Answers to the question just asked sit under it — unless the turn
            is about one product, where they belong with its companions. */}
        {latest && textChoices.length > 0 && !p?.focus && (
          <div className="chips">
            {textChoices.map((choice) => (
              <button key={choice.value} className="chip" onClick={() => agent.choose(choice)} disabled={busy}>
                {choice.label}
              </button>
            ))}
          </div>
        )}

        {latest && p?.piece_picker && <PiecePicker picker={p.piece_picker} disabled={busy} />}

        {p?.brief?.mode === 'ask' && <BriefCard brief={p.brief} active={latest} />}

        {p?.focus && (
          <div className="cards">
            <ProductCard product={p.focus} listRevision={null} latest={false} />
          </div>
        )}

        {products.length > 0 && (
          <>
            {latest && agent.swap && isResults && (
              <div className="hint">
                Choose a replacement {agent.swap.role} — tap “Use this one” and I&apos;ll rebuild the room around it.
              </div>
            )}
            <div className="cards" role="list" aria-label="Products">
              {products.map((product, index) => (
                <ProductCard
                  key={product.grounding_ref}
                  product={product}
                  listRevision={listRevision}
                  latest={latest}
                  bestMatch={isResults && !!p?.best_match && index === 0}
                />
              ))}
            </div>
            {latest && isResults && !agent.swap && (
              <button className="link-btn" onClick={agent.showMore} disabled={busy}>
                <Icon name="swap" size={14} />
                Show different options
              </button>
            )}
          </>
        )}

        {p?.brief?.mode === 'narrow' && latest && <BriefCard brief={p.brief} active={latest} />}

        {latest && (actionChoices.length > 0 || (p?.focus && textChoices.length > 0)) && (
          <div className="chips" aria-label="Goes with it">
            {actionChoices.length > 0 && (
              <span className="chip-label" style={{ alignSelf: 'center', marginRight: 2 }}>
                Goes with it
              </span>
            )}
            {[...actionChoices, ...(p?.focus ? textChoices : [])].map((choice) => (
              <button key={choice.label} className="chip" onClick={() => agent.choose(choice)} disabled={busy}>
                {choice.label}
              </button>
            ))}
          </div>
        )}

        {p?.comparison && <Comparison comparison={p.comparison} />}
        {p?.room && <RoomPlan room={p.room} current={currentRoom} />}
        {p?.render && <Render render={p.render} canRedraw={latest && (p.render.source !== 'catalog' || agent.lastSelection != null)} />}
        {renderDropped && <div className="hint">The room picture isn&apos;t kept after the page reloads.</div>}

        {(p?.seating_bundles ?? []).map((bundle, index, all) => (
          <RoomPlan
            key={index}
            room={bundle}
            current={false}
            label={all.length > 1 ? `Seating option ${index + 1}` : 'Seating combination'}
          />
        ))}
      </div>
    </div>
  )
}

// ── comparison ──────────────────────────────────────────────────────────────

/** The backend's comparison fields, in a shopper's words. */
const FIELD_LABELS: Record<string, string> = {
  price: 'Price',
  commerce_subcategory: 'Type',
  seating_capacity: 'Seats',
  main_color: 'Colour',
  styles: 'Style',
  length: 'Length',
  overall_width: 'Width',
  depth: 'Depth',
  height: 'Height',
}

/** "2550.00 SAR" → "2,550 SAR"; anything else is shown as the backend wrote it. */
function cellText(field: string, value: string): string {
  const price = field === 'price' ? /^(-?\d+(?:\.\d+)?)\s+(\S+)$/.exec(value) : null
  if (price) return money(price[1], price[2]) ?? value
  if (field === 'commerce_subcategory') return humanise(value)
  return value
}

function Comparison({ comparison }: { comparison: ProductComparisonResult }) {
  // A row no listing fills tells the shopper nothing.
  const rows = comparison.rows.filter((row) => row.cells.some((cell) => cell.known))
  return (
    <div className="card">
      <div className="card-head">Side by side</div>
      <table className="compare-table">
        <thead>
          <tr>
            <th aria-label="Detail" style={{ width: 72 }} />
            {comparison.products.map((product) => (
              <th key={product.grounding_ref}>
                <ProductImage src={product.image_url} alt="" />
                <a href={product.product_url} target="_blank" rel="noopener noreferrer" className="pname">
                  {product.name_english}
                </a>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.field}>
              <th scope="row">{FIELD_LABELS[row.field] ?? humanise(row.field)}</th>
              {row.cells.map((cell, index) => (
                <td
                  key={index}
                  className={!cell.known ? 'unknown' : row.status === 'different' ? 'diff' : undefined}
                >
                  {cell.known && cell.value ? cellText(row.field, cell.value) : '—'}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── room plan ───────────────────────────────────────────────────────────────

const STATUS_LABEL = { complete: 'Complete', partial: 'Partial', infeasible: 'Over budget' } as const

function RoomPlan({
  room,
  current,
  label = 'Your room plan',
}: {
  room: GroundedBundlePresentation
  current: boolean
  label?: string
}) {
  const { agent } = useWidget()
  const [drawing, setDrawing] = useState(false)
  const t = room.totals
  const spend = toNumber(t.new_spend_total)
  const budget = toNumber(t.budget_max_amount)
  const pct = spend != null && budget ? Math.min(100, (spend / budget) * 100) : null
  const over = t.within_budget === false
  const remaining = spend != null && budget != null ? budget - spend : null
  const busy = agent.sending || agent.picking
  const images = room.items.filter((item) => item.image_url).slice(0, 4)

  return (
    <div className="card">
      {images.length >= 2 && (
        <div className="room-collage" aria-hidden="true">
          {images.map((item) => (
            <div key={item.grounding_ref}>
              <ProductImage src={item.image_url} alt="" />
            </div>
          ))}
        </div>
      )}
      <div className="card-head">
        <span>{label}</span>
        <span className={`status ${room.status}`}>{STATUS_LABEL[room.status]}</span>
      </div>
      {(t.new_spend_total || t.budget_max_amount) && (
        <div className="room-total">
          <div className="line">
            <span>Room total</span>
            <span>
              <b>{money(t.new_spend_total, t.currency) ?? '—'}</b>
              {t.budget_max_amount && <> / {money(t.budget_max_amount, t.budget_currency)}</>}
            </span>
          </div>
          {pct != null && (
            <div className={`bar${over ? ' over' : ''}`}>
              <i style={{ width: `${pct}%` }} />
            </div>
          )}
          {remaining != null && (
            <small>
              {over
                ? `${money(String(-remaining), t.budget_currency)} over your budget`
                : `${money(String(remaining), t.budget_currency)} remaining`}
            </small>
          )}
        </div>
      )}
      <ul className="items">
        {room.items.map((item) => {
          const kind = item.commerce.subcategory ?? item.commerce.category ?? 'piece'
          const canSwap = current && item.acquisition === 'to_buy' && !item.locked
          return (
            <li key={item.grounding_ref}>
              <img className="thumb" src={item.image_url} alt="" loading="lazy" />
              <div className="item-main">
                <strong>
                  <a href={item.product_url} target="_blank" rel="noopener noreferrer" style={{ color: 'inherit', textDecoration: 'none' }}>
                    {item.name_english}
                  </a>
                </strong>
                <span>
                  {humanise(kind)}
                  {item.quantity > 1 && ` · ×${item.quantity}`}
                  {item.locked && ' · kept'}
                  {item.acquisition === 'already_owned' && ' · already yours'}
                </span>
                {canSwap && (
                  <button className="swap" onClick={() => agent.startSwap(item.grounding_ref, humanise(kind))} disabled={busy}>
                    <Icon name="swap" size={12} /> Other options
                  </button>
                )}
              </div>
              <div className="item-side">
                <b>
                  {item.acquisition === 'already_owned'
                    ? '—'
                    : (money(item.new_spend_line_total, item.price_unit) ?? money(item.unit_price, item.price_unit))}
                </b>
                {item.quantity > 1 && <small>{money(item.unit_price, item.price_unit)} each</small>}
              </div>
            </li>
          )
        })}
      </ul>
      {current && room.items.length > 0 && (
        <div className="card-foot">
          {drawing ? (
            <>
              <span className="chip-label">Choose a view</span>
              <div className="views">
                {RENDER_VIEWS.map((view) => (
                  <button
                    key={view.value}
                    className="chip"
                    disabled={busy}
                    onClick={() => {
                      setDrawing(false)
                      agent.visualize(view.value, view.label)
                    }}
                  >
                    {view.label}
                  </button>
                ))}
              </div>
            </>
          ) : (
            <button className="btn block" onClick={() => setDrawing(true)} disabled={busy}>
              <Icon name="cube" size={17} />
              Visualize this room
            </button>
          )}
        </div>
      )}
    </div>
  )
}

// ── render ──────────────────────────────────────────────────────────────────

function Render({ render, canRedraw }: { render: RoomRenderPresentation; canRedraw: boolean }) {
  const { agent } = useWidget()
  const [viewer, setViewer] = useState(false)
  const busy = agent.sending || agent.picking
  const others = RENDER_VIEWS.filter((view) => view.value !== render.view)
  const alt = `Your room, ${render.view_label.toLowerCase()} view`
  const fileName = `room-${render.view.replace(/_/g, '-')}.jpg`
  // A catalogue room is drawn again from the selection it was made from.
  const redraw = (view: RenderView, label: string) =>
    render.source === 'catalog' && agent.lastSelection
      ? agent.visualizeSelection(agent.lastSelection, view, `Show my selection — ${label.toLowerCase()} view`)
      : agent.visualize(view, label)

  /** The browser's own full screen where it is allowed, otherwise a viewer
   *  over the panel - an iframe without the permission is refused, and some
   *  embedded browsers never answer the request at all. */
  const openFullScreen = (event: React.MouseEvent<HTMLButtonElement>) => {
    const figure = event.currentTarget.closest('figure')
    const img = figure?.querySelector('img')
    if (!img || !document.fullscreenEnabled || !img.requestFullscreen) {
      setViewer(true)
      return
    }
    const fallback = () => {
      if (!document.fullscreenElement) setViewer(true)
    }
    const timer = window.setTimeout(fallback, 600)
    img.requestFullscreen().catch(() => {
      window.clearTimeout(timer)
      setViewer(true)
    })
  }

  return (
    <figure className="render card">
      <div className="card-head">
        <span>Your room</span>
        <span className="status">{render.view_label} view</span>
      </div>
      <img src={render.image_url} alt={alt} />
      <div className="render-actions">
        <button className="link-btn" onClick={openFullScreen}>
          <Icon name="fullscreen" size={14} /> Full screen
        </button>
        <a className="link-btn" href={render.image_url} download={fileName}>
          <Icon name="download" size={14} /> Download
        </a>
        <span className="muted render-count">{render.items.length} pieces</span>
      </div>
      {canRedraw && (
        <div className="views render-views">
          <span className="chip-label">Try another view</span>
          {others.map((view) => (
            <button
              key={view.value}
              className="chip"
              disabled={busy}
              onClick={() => redraw(view.value as RenderView, view.label)}
            >
              {view.label}
            </button>
          ))}
        </div>
      )}
      {viewer && (
        <div className="lightbox" role="dialog" aria-modal="true" aria-label={alt} onClick={() => setViewer(false)}>
          <img src={render.image_url} alt={alt} />
          <button className="lightbox-close" onClick={() => setViewer(false)} aria-label="Close full screen">
            <Icon name="close" size={18} />
          </button>
        </div>
      )}
    </figure>
  )
}

// ── the card of questions ───────────────────────────────────────────────────

function BriefCard({ brief, active }: { brief: ProductBrief; active: boolean }) {
  const { agent } = useWidget()
  const [open, setOpen] = useState(brief.mode === 'ask')
  const [picked, setPicked] = useState<Record<string, string[]>>({})
  const [sent, setSent] = useState(false)
  const disabled = !active || sent || agent.sending

  const toggle = (question: BriefQuestion, key: string) =>
    setPicked((current) => {
      const chosen = current[question.kind] ?? []
      if (chosen.includes(key)) return { ...current, [question.kind]: chosen.filter((k) => k !== key) }
      const next = question.max_choices === 1 ? [key] : [...chosen, key].slice(-question.max_choices)
      return { ...current, [question.kind]: next }
    })

  const summary = brief.questions.flatMap((q) => {
    const chosen = picked[q.kind] ?? []
    return chosen.length
      ? [chosen.map((key) => q.choices.find((c) => c.key === key)?.label ?? key).join(' or ')]
      : []
  })

  const submit = () => {
    const first = (kind: string) => (picked[kind] ?? [])[0] ?? null
    setSent(true)
    agent.submitBrief(
      {
        kind: 'brief',
        card: brief.card,
        piece: first('type'),
        budget: first('budget'),
        colours: picked.colour ?? [],
        styles: picked.style ?? [],
        feel: first('feel'),
      },
      summary.length ? summary.join(' · ') : `Just ${brief.submit_label.toLowerCase()}`,
    )
  }

  if (!open) {
    return (
      <button className="link-btn" onClick={() => setOpen(true)} disabled={disabled}>
        <Icon name="sliders" size={14} />
        Narrow these down
      </button>
    )
  }

  return (
    <div className="card">
      <div className="card-head">{brief.mode === 'ask' ? 'A few quick taps — skip any' : 'Narrow these down'}</div>
      <div className="brief">
        {brief.questions.map((question) => {
          const chosen = picked[question.kind] ?? []
          return (
            <div key={question.kind} className="brief-q">
              <div>
                {question.label}
                {question.max_choices > 1 && <small> · up to {question.max_choices}</small>}
              </div>
              <div className="chips">
                {question.choices.map((choice) => (
                  <button
                    key={choice.key}
                    className="chip"
                    aria-pressed={chosen.includes(choice.key)}
                    onClick={() => toggle(question, choice.key)}
                    disabled={disabled}
                  >
                    {choice.label}
                  </button>
                ))}
              </div>
            </div>
          )
        })}
        <button className="btn block" onClick={submit} disabled={disabled}>
          {summary.length ? brief.submit_label : `Just ${brief.submit_label.toLowerCase()}`}
        </button>
      </div>
    </div>
  )
}

// ── a room's pieces ─────────────────────────────────────────────────────────

function PiecePicker({ picker, disabled }: { picker: PiecePickerData; disabled: boolean }) {
  const { agent } = useWidget()
  const [ticked, setTicked] = useState<Set<string>>(
    () => new Set(picker.pieces.filter((piece) => piece.selected).map((piece) => piece.label)),
  )
  const chosen = picker.pieces.filter((piece) => ticked.has(piece.label)).map((piece) => piece.label)
  const toggle = (label: string) =>
    setTicked((current) => {
      const next = new Set(current)
      if (next.has(label)) next.delete(label)
      else next.add(label)
      return next
    })

  return (
    <div className="card">
      <div className="card-head">Pieces for this room</div>
      <div className="brief">
        <div className="chips">
          {picker.pieces.map((piece) => (
            <button
              key={piece.label}
              className="chip"
              aria-pressed={ticked.has(piece.label)}
              onClick={() => toggle(piece.label)}
              disabled={disabled}
            >
              <Icon name={ticked.has(piece.label) ? 'check' : 'plus'} size={13} strokeWidth={2} />
              {piece.label}
            </button>
          ))}
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button
            className="btn small"
            style={{ flex: 1 }}
            disabled={disabled || chosen.length === 0}
            onClick={() => void agent.send(`I'd like these pieces: ${chosen.join(', ')}`)}
          >
            {picker.submit_label}
          </button>
          <button
            className="btn small ghost"
            disabled={disabled}
            onClick={() => void agent.send(picker.choose_for_me.value)}
          >
            {picker.choose_for_me.label}
          </button>
        </div>
      </div>
    </div>
  )
}
