import { useState } from 'react'
import type { PickView } from '../../api/types'
import { humanise, money } from '../../lib/format'
import { EmptyState } from '../components/EmptyState'
import { useWidget } from '../context'
import { Icon } from '../icons'

/**
 * The shopper's picks — the backend's own record of what they chose, which the
 * agent reads too ("compare the two I picked", "what goes with my sofa").
 * `mode="compare"` is the same list framed for choosing two to set side by side.
 */
export function Basket({ mode = 'basket' }: { mode?: 'basket' | 'compare' }) {
  const { config, agent, go, ask } = useWidget()
  const [checked, setChecked] = useState<number[]>([])
  const picks = agent.picks
  const busy = agent.sending || agent.picking

  if (picks.length === 0 || (mode === 'compare' && picks.length < 2)) {
    return mode === 'compare' ? (
      <EmptyState
        icon="columns"
        title="Pick two pieces to compare"
        text={`Tap compare on any two products ${config.assistantName} shows you, or add them to your basket first.`}
        action="Find furniture"
        onAction={() => go('catalog')}
      />
    ) : (
      <EmptyState
        icon="bag"
        title="Your basket is empty"
        text={`Browse the catalogue or ask ${config.assistantName} to help you find the right pieces.`}
        action="Browse furniture"
        onAction={() => go('catalog')}
      />
    )
  }

  const toggle = (pick: PickView) =>
    setChecked((current) =>
      current.includes(pick.pick)
        ? current.filter((n) => n !== pick.pick)
        : [...current, pick.pick].slice(-2),
    )
  const pair = checked.map((n) => picks.find((p) => p.pick === n)).filter((p): p is PickView => !!p)
  const currencies = new Set(picks.map((p) => p.price_unit))
  const total = currencies.size === 1 ? picks.reduce((sum, p) => sum + Number(p.price_amount || 0), 0) : null

  return (
    <div className="scroll-inner">
      <div className="basket">
        {mode === 'compare' && <div className="hint">Tick any two pieces, then compare them side by side.</div>}
        <div className="card">
          <ul className="items">
            {picks.map((pick) => (
              <li key={pick.pick}>
                <button
                  className="check"
                  role="checkbox"
                  aria-checked={checked.includes(pick.pick)}
                  aria-label={`Select ${pick.name_english} to compare`}
                  onClick={() => toggle(pick)}
                >
                  {checked.includes(pick.pick) && <Icon name="check" size={13} strokeWidth={2.6} />}
                </button>
                <img className="thumb" src={pick.image_url} alt="" loading="lazy" />
                <div className="item-main">
                  <strong>{pick.name_english}</strong>
                  <span>{humanise(pick.kind)}</span>
                  {mode === 'basket' && (
                    <button
                      className="swap"
                      onClick={() => {
                        agent.goesWith(pick)
                        go('chat')
                      }}
                      disabled={busy}
                    >
                      <Icon name="spark" size={12} /> What goes with it
                    </button>
                  )}
                </div>
                <div className="item-side">
                  <b>{money(pick.price_amount, pick.price_unit)}</b>
                  <button
                    className="icon-btn"
                    style={{ marginLeft: 'auto', width: 28, height: 28 }}
                    onClick={() => agent.removePick(pick)}
                    disabled={busy}
                    aria-label={`Remove ${pick.name_english}`}
                    title="Remove"
                  >
                    <Icon name="trash" size={15} />
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </div>

        <div className="card summary">
          {mode === 'basket' && total != null && (
            <div className="line">
              <span className="muted">
                {picks.length} {picks.length === 1 ? 'piece' : 'pieces'}
              </span>
              <b>{money(String(total), picks[0].price_unit)}</b>
            </div>
          )}
          <button
            className="btn block"
            disabled={pair.length !== 2 || busy}
            onClick={() => {
              agent.compare(pair[0], pair[1])
              go('chat')
            }}
          >
            <Icon name="columns" size={16} />
            {pair.length === 2 ? 'Compare these two' : `Select ${2 - pair.length} more to compare`}
          </button>
          {mode === 'basket' && (
            <button className="btn block ghost" onClick={() => ask("Show me what I've picked")} disabled={busy}>
              Review my picks with {config.assistantName}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
