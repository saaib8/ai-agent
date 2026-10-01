import { useEffect, useRef, useState } from 'react'
import { Avatar } from '../components/Avatar'
import { useWidget } from '../context'
import { Icon } from '../icons'
import { HOME_CARDS } from '../tools'

/** How long each pair of cards stays before the rail moves on. */
const AUTOPLAY_MS = 3500

export function Home() {
  const { config } = useWidget()
  return (
    <div className="scroll-inner">
      <div className="home">
        <div className="eyebrow">Your home, with a little guidance</div>
        <div className="hero">
          <div>
            <h1>
              Make room for
              <br />
              <em>what you love.</em>
            </h1>
            <p>
              Hi, I&apos;m {config.assistantName}. Let&apos;s find the right pieces for your space.
            </p>
          </div>
          <Avatar name={config.assistantName} url={config.avatarUrl} size={44} />
        </div>
        <ToolCarousel />
      </div>
    </div>
  )
}

/**
 * Every tool on one rail, two cards in view (four when expanded), moving on
 * by itself. It holds still while the shopper hovers, touches or tabs into
 * it, and never moves for someone who asked for reduced motion.
 */
function ToolCarousel() {
  const { go, ask } = useWidget()
  const rail = useRef<HTMLDivElement>(null)
  const [held, setHeld] = useState(false)
  const [page, setPage] = useState({ index: 0, count: 1 })

  /** One step is one card plus the gap; the last step wraps to the start. */
  const step = (direction: 1 | -1) => {
    const el = rail.current
    const card = el?.firstElementChild as HTMLElement | null
    if (!el || !card) return
    const width = card.offsetWidth + 10
    const atEnd = el.scrollLeft + el.clientWidth >= el.scrollWidth - 4
    const atStart = el.scrollLeft <= 4
    if (direction === 1 && atEnd) el.scrollTo({ left: 0, behavior: 'smooth' })
    else if (direction === -1 && atStart) el.scrollTo({ left: el.scrollWidth, behavior: 'smooth' })
    else el.scrollBy({ left: direction * width, behavior: 'smooth' })
  }

  const update = () => {
    const el = rail.current
    const card = el?.firstElementChild as HTMLElement | null
    if (!el || !card) return
    const width = card.offsetWidth + 10
    const perView = Math.max(1, Math.round(el.clientWidth / width))
    const count = Math.max(1, HOME_CARDS.length - perView + 1)
    setPage({ index: Math.min(count - 1, Math.round(el.scrollLeft / width)), count })
  }

  useEffect(() => {
    update()
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    if (held || reduced) return
    const timer = window.setInterval(() => step(1), AUTOPLAY_MS)
    return () => window.clearInterval(timer)
  }, [held])

  return (
    <section aria-label="What I can help with" aria-roledescription="carousel">
      <div className="section-head">
        <h2>What I can help with</h2>
        <div className="arrows">
          <button className="arrow" onClick={() => step(-1)} aria-label="Previous tools">
            <Icon name="back" size={14} />
          </button>
          <button className="arrow" onClick={() => step(1)} aria-label="Next tools">
            <Icon name="next" size={14} />
          </button>
        </div>
      </div>
      <div
        className="rail"
        ref={rail}
        onScroll={update}
        onMouseEnter={() => setHeld(true)}
        onMouseLeave={() => setHeld(false)}
        onFocus={() => setHeld(true)}
        onBlur={() => setHeld(false)}
        onTouchStart={() => setHeld(true)}
        onTouchEnd={() => window.setTimeout(() => setHeld(false), 4000)}
      >
        {HOME_CARDS.map((card) => (
          <button
            key={card.title}
            className="tool-card"
            onClick={() => ('ask' in card.action ? ask(card.action.ask) : go(card.action.view))}
          >
            <span className="tool-ico">
              <Icon name={card.icon} size={17} />
            </span>
            <span>
              {card.label && <span className="tool-label">{card.label}</span>}
              <strong>{card.title}</strong>
              {card.description && <small>{card.description}</small>}
            </span>
          </button>
        ))}
      </div>
      <div className="dots-nav" aria-hidden="true">
        {Array.from({ length: page.count }, (_, index) => (
          <i key={index} className={index === page.index ? 'on' : undefined} />
        ))}
      </div>
    </section>
  )
}
