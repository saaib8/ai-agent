import { useLayoutEffect, useRef, useState } from 'react'
import { useWidget } from '../context'
import { Icon } from '../icons'

/** Query understanding reads at most this much of one message. */
const MAX_CHARS = 1000

export function Composer({ onTools }: { onTools: () => void }) {
  const { agent, ask, pickPhoto } = useWidget()
  const [text, setText] = useState('')
  const box = useRef<HTMLTextAreaElement>(null)
  const canSend = text.trim().length > 0 && !agent.sending

  // Grow with the message up to the CSS max-height, then scroll.
  useLayoutEffect(() => {
    const el = box.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
    el.style.overflowY = el.scrollHeight > 120 ? 'auto' : 'hidden'
  }, [text])

  const submit = () => {
    if (!canSend) return
    ask(text)
    setText('')
  }

  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault()
        submit()
      }}
    >
      <label htmlFor="zw-message" className="sr-only">
        Message
      </label>
      <textarea
        id="zw-message"
        ref={box}
        rows={1}
        value={text}
        maxLength={MAX_CHARS}
        placeholder="Ask about your room or furniture…"
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault()
            submit()
          }
        }}
      />
      <div className="composer-row">
        <button type="button" className="tools-pill" onClick={onTools} aria-label="Open assistant tools">
          <Icon name="plus" size={14} strokeWidth={2} />
          Tools
        </button>
        <button
          type="button"
          className="icon-btn"
          onClick={pickPhoto}
          disabled={agent.sending}
          aria-label="Search with a photo"
          title="Search with a photo"
        >
          <Icon name="clip" size={16} />
        </button>
        <button type="submit" className="send-btn" disabled={!canSend} aria-label="Send message">
          <Icon name="send" size={16} strokeWidth={2} />
        </button>
      </div>
    </form>
  )
}
