import { useState } from 'react'
import { RENDER_VIEWS } from '../../api/types'
import { EmptyState } from '../components/EmptyState'
import { useWidget } from '../context'
import { Icon } from '../icons'

/** Render the room package from a camera view. Only a room the agent has
 *  already put together can be drawn — the render shows exactly its pieces. */
export function Visualize() {
  const { config, agent, go } = useWidget()
  const hasRoom = agent.turns.some((t) => t.kind === 'assistant' && !!t.data.presentation?.room)
  if (!hasRoom) {
    return (
      <EmptyState
        icon="cube"
        title="Let's plan a room first"
        text={`${config.assistantName} draws the room plan — the real pieces chosen for your budget. Start by designing a room.`}
        action="Design my space"
        onAction={() => go('room-planner')}
      />
    )
  }
  return (
    <EmptyState
      icon="cube"
      title="See your room come together"
      text="Choose a view and I'll draw your room plan with its real pieces. It takes about a minute."
    >
      <div className="views" style={{ justifyContent: 'center', marginBottom: 6 }}>
        {RENDER_VIEWS.map((view) => (
          <button
            key={view.value}
            className="chip"
            disabled={agent.sending}
            onClick={() => {
              agent.visualize(view.value, view.label)
              go('chat')
            }}
          >
            {view.label}
          </button>
        ))}
      </div>
    </EmptyState>
  )
}

const QUESTIONS = [
  'What size rug should go under a 3-seat sofa?',
  'Which colours go well with a walnut coffee table?',
  'How much space should I leave around a dining table?',
  'How do I make a small living room feel bigger?',
  'Should my sofa and armchair match?',
]

/** Design questions go to the interior-design specialist and come back as
 *  advice, not a shelf of products. */
export function Advice() {
  const { config, ask, agent } = useWidget()
  return (
    <div className="scroll-inner">
      <div className="catalog">
        <div className="eyebrow">Size, colour & style advice</div>
        <h2>What would you like to know?</h2>
        <p className="note" style={{ marginTop: 0 }}>
          Ask {config.assistantName} anything about your space — or start with one of these.
        </p>
        <div className="sheet-list">
          {QUESTIONS.map((question) => (
            <button key={question} className="tool-row" onClick={() => ask(question)} disabled={agent.sending}>
              <span className="tool-ico">
                <Icon name="bulb" size={16} />
              </span>
              <span>
                <strong style={{ fontWeight: 500 }}>{question}</strong>
              </span>
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

const ROOM_TYPES = ['living room', 'bedroom', 'dining room', 'home office', 'majlis']

/** The room's size and who uses it, told to the agent in words so it is
 *  recorded on the room the way anything the shopper says is. */
export function RoomContext() {
  const { ask, agent, facets } = useWidget()
  const [room, setRoom] = useState('living room')
  const [length, setLength] = useState('')
  const [width, setWidth] = useState('')
  const [seats, setSeats] = useState('')
  const min = facets?.studio.min_room_side_m ?? 1.5
  const max = facets?.studio.max_room_side_m ?? 20
  const valid = (value: string) => {
    const n = Number(value)
    return value.trim() !== '' && Number.isFinite(n) && n >= min && n <= max
  }
  const ok = valid(length) && valid(width) && (seats === '' || (Number(seats) >= 1 && Number(seats) <= 40))

  const submit = () => {
    const people = seats ? ` Usually ${Number(seats)} people sit there.` : ''
    ask(`My ${room} is ${Number(length)} x ${Number(width)} m.${people}`)
  }

  return (
    <div className="scroll-inner">
      <div className="catalog">
        <div className="eyebrow">Tell me about your space</div>
        <h2>Your room</h2>
        <span className="field-label" style={{ marginTop: 8 }}>
          Room
        </span>
        <div className="chips">
          {ROOM_TYPES.map((value) => (
            <button key={value} className="chip" aria-pressed={room === value} onClick={() => setRoom(value)} style={{ textTransform: 'capitalize' }}>
              {value}
            </button>
          ))}
        </div>
        <span className="field-label">Size in metres</span>
        <div className="pair">
          <input className="input" inputMode="decimal" placeholder="Length" value={length} onChange={(e) => setLength(e.target.value)} aria-label="Room length in metres" />
          <span>×</span>
          <input className="input" inputMode="decimal" placeholder="Width" value={width} onChange={(e) => setWidth(e.target.value)} aria-label="Room width in metres" />
        </div>
        <span className="field-label">How many people sit here? (optional)</span>
        <input className="input" inputMode="numeric" placeholder="e.g. 4" value={seats} onChange={(e) => setSeats(e.target.value)} aria-label="People who usually sit in the room" />
        <p className="note">
          Sides between {min} and {max} m.
        </p>
        <button className="btn block" style={{ marginTop: 16 }} disabled={!ok || agent.sending} onClick={submit}>
          Tell me about this room
        </button>
      </div>
    </div>
  )
}

/** A photo of a piece or a room: the store's matches for anything in it. */
export function PhotoSearch() {
  const { config, agent, pickPhoto, go } = useWidget()
  const [over, setOver] = useState(false)
  return (
    <div
      className="empty"
      onDragOver={(event) => {
        event.preventDefault()
        setOver(true)
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(event) => {
        event.preventDefault()
        setOver(false)
        const file = event.dataTransfer.files?.[0]
        if (file && file.type.startsWith('image/')) {
          go('chat')
          void agent.uploadPhoto(file)
        }
      }}
      style={over ? { background: 'var(--accent-soft)' } : undefined}
    >
      <span className="ring">
        <Icon name="camera" size={22} />
      </span>
      <h3>Search by photo</h3>
      <p>
        Share a photo of a piece or a room you love. {config.assistantName} outlines the furniture — tap one to find
        similar pieces in the store.
      </p>
      <button className="btn small" onClick={pickPhoto} disabled={agent.sending}>
        <Icon name="upload" size={15} /> Choose a photo
      </button>
      <p style={{ marginTop: 10, fontSize: 11.5 }}>JPEG, PNG or WebP, up to 10 MB. Or drop it here.</p>
    </div>
  )
}
