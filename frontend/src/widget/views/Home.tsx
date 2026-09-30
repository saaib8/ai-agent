import { useRef, useState } from 'react'
import { Avatar } from '../components/Avatar'
import { useWidget } from '../context'
import { Icon } from '../icons'
import { TOOLS } from '../tools'
import type { ToolDef } from '../tools'

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
        <ToolRail title="Discover & design" tools={TOOLS.filter((t) => t.group === 'discover')} />
        <ToolRail title="Decide with confidence" tools={TOOLS.filter((t) => t.group === 'decide')} />
      </div>
    </div>
  )
}

function ToolRail({ title, tools }: { title: string; tools: ToolDef[] }) {
  const { go } = useWidget()
  const rail = useRef<HTMLDivElement>(null)
  const [edge, setEdge] = useState({ start: true, end: false })

  const update = () => {
    const el = rail.current
    if (!el) return
    setEdge({
      start: el.scrollLeft <= 2,
      end: el.scrollLeft + el.clientWidth >= el.scrollWidth - 2,
    })
  }
  const scroll = (direction: 1 | -1) =>
    rail.current?.scrollBy({ left: direction * 182, behavior: 'smooth' })

  const id = `rail-${title.replace(/\W+/g, '-').toLowerCase()}`
  return (
    <section aria-labelledby={id}>
      <div className="section-head">
        <h2 id={id}>{title}</h2>
        <div className="arrows">
          <button className="arrow" onClick={() => scroll(-1)} disabled={edge.start} aria-label={`Previous ${title} tools`}>
            <Icon name="back" size={14} />
          </button>
          <button className="arrow" onClick={() => scroll(1)} disabled={edge.end} aria-label={`Next ${title} tools`}>
            <Icon name="next" size={14} />
          </button>
        </div>
      </div>
      <div className="rail" ref={rail} onScroll={update}>
        {tools.map((tool) => (
          <button key={tool.id} className="tool-card" onClick={() => go(tool.id)}>
            <span className="tool-ico">
              <Icon name={tool.icon} size={17} />
            </span>
            <span>
              <strong>{tool.title}</strong>
              <small>{tool.blurb}</small>
            </span>
          </button>
        ))}
      </div>
    </section>
  )
}
