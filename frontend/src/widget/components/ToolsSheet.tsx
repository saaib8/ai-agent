import { useEffect, useRef, useState } from 'react'
import { useWidget } from '../context'
import { Icon } from '../icons'
import { TOOLS } from '../tools'

export function ToolsSheet({ onClose }: { onClose: () => void }) {
  const { go } = useWidget()
  const [query, setQuery] = useState('')
  const input = useRef<HTMLInputElement>(null)
  const needle = query.trim().toLowerCase()
  const shown = TOOLS.filter(
    (tool) => !needle || `${tool.title} ${tool.blurb}`.toLowerCase().includes(needle),
  )

  useEffect(() => {
    input.current?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        onClose()
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [onClose])

  return (
    <div className="sheet-backdrop" onClick={(event) => event.target === event.currentTarget && onClose()}>
      <div className="sheet" role="dialog" aria-modal="true" aria-labelledby="zw-tools-title">
        <div className="sheet-head">
          <div>
            <h3 id="zw-tools-title">What would you like to do?</h3>
            <p>Choose a tool to continue.</p>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="Close tools">
            <Icon name="close" size={18} />
          </button>
        </div>
        <div className="search">
          <Icon name="search" size={16} />
          <input
            ref={input}
            className="input"
            placeholder="Search tools"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            aria-label="Search tools"
          />
        </div>
        <div className="sheet-list">
          {shown.map((tool) => (
            <button
              key={tool.id}
              className="tool-row"
              onClick={() => {
                onClose()
                go(tool.id)
              }}
            >
              <span className="tool-ico">
                <Icon name={tool.icon} size={17} />
              </span>
              <span>
                <strong>{tool.title}</strong>
                <small>{tool.blurb}</small>
              </span>
            </button>
          ))}
          {shown.length === 0 && <p className="note">No tool matches “{query}”.</p>}
        </div>
      </div>
    </div>
  )
}
