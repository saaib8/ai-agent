import { useWidget } from '../context'
import { Icon } from '../icons'

/**
 * The pieces ticked for a side-by-side, waiting above the composer until the
 * shopper runs the comparison. Comparison is of exactly two, so the button
 * counts up to two and only then becomes active.
 */
export function CompareTray() {
  const { agent } = useWidget()
  const queued = agent.comparing
    .map((n) => agent.picks.find((pick) => pick.pick === n))
    .filter((pick) => pick != null)
  if (queued.length === 0) return null
  const busy = agent.sending || agent.picking
  const ready = queued.length === 2

  return (
    <section className="compare-tray" aria-label="Compare">
      <span className="tray-label">Compare</span>
      <ul className="tray-items">
        {queued.map((pick) => (
          <li key={pick.pick} className="tray-item">
            {pick.image_url ? <img src={pick.image_url} alt="" /> : <span className="tray-thumb" />}
            <span className="tray-name" title={pick.name_english}>
              {pick.name_english}
            </span>
            <button
              className="tray-remove"
              onClick={() => agent.uncompare(pick.pick)}
              aria-label={`Remove ${pick.name_english} from comparison`}
            >
              <Icon name="close" size={13} strokeWidth={2} />
            </button>
          </li>
        ))}
        {!ready && <li className="tray-item tray-empty">Tick one more piece</li>}
      </ul>
      <div className="tray-actions">
        <button className="tray-clear" onClick={agent.clearCompare}>
          Clear
        </button>
        <button className="tray-go" onClick={agent.runCompare} disabled={!ready || busy}>
          <Icon name="columns" size={15} />
          Compare {queued.length}
        </button>
      </div>
    </section>
  )
}
