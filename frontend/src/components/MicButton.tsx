import { useEffect, useRef, useState } from 'react'
import type { Fetched } from '../api/client'
import type { SpeechTextResponse } from '../api/types'
import { MicIcon, StopIcon } from './icons'

const NOTICE_MS = 5000
const NOTHING_HEARD = "I didn't catch that. Please try again."
const MIC_BLOCKED = 'Allow microphone access to speak your message.'

// The first one the browser can record: Chrome and Firefox take webm/opus,
// Safari records mp4. None supported: the browser's own default.
const RECORDING_TYPES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg']

/** The whole recording is sent at stop; its words come back at once. */
type Phase = 'idle' | 'recording' | 'finishing'

interface MicButtonProps {
  /** Send the whole recording; the answer is the words heard. */
  onRecorded: (audio: Blob, filename: string) => Promise<Fetched<SpeechTextResponse>>
  /** The words heard, for the composer. Never sent here. */
  onText: (text: string) => void
  disabled: boolean
}

function recordingType(): string | undefined {
  if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return undefined
  return RECORDING_TYPES.find((type) => MediaRecorder.isTypeSupported(type))
}

function extensionFor(type: string): string {
  if (type.includes('mp4')) return 'm4a'
  if (type.includes('ogg')) return 'ogg'
  return 'webm'
}

function clock(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

export function MicButton({ onRecorded, onText, disabled }: MicButtonProps) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [seconds, setSeconds] = useState(0)
  const [notice, setNotice] = useState<string | null>(null)
  const recorder = useRef<MediaRecorder | null>(null)
  const stream = useRef<MediaStream | null>(null)
  const chunks = useRef<Blob[]>([])
  const ticker = useRef<number | null>(null)
  // The words land in the box as it is when recording stops, not as it was
  // when it started: anything typed meanwhile is kept.
  const textTo = useRef(onText)
  textTo.current = onText

  const stopClock = () => {
    if (ticker.current !== null) window.clearInterval(ticker.current)
    ticker.current = null
  }
  const startClock = () => {
    setSeconds(0)
    ticker.current = window.setInterval(() => setSeconds((s) => s + 1), 1000)
  }
  const releaseRecording = () => {
    stopClock()
    stream.current?.getTracks().forEach((track) => track.stop())
    stream.current = null
  }

  // Leaving the page while recording must not leave the microphone on.
  useEffect(() => () => releaseRecording(), [])

  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(null), NOTICE_MS)
    return () => window.clearTimeout(timer)
  }, [notice])

  const finishRecording = async (type: string) => {
    releaseRecording()
    const audio = new Blob(chunks.current, { type })
    chunks.current = []
    if (audio.size === 0) {
      setPhase('idle')
      return
    }
    setPhase('finishing')
    const result = await onRecorded(audio, `recording.${extensionFor(type)}`)
    setPhase('idle')
    if (result.ok) {
      const text = result.data.text.trim()
      if (text) textTo.current(text)
      else setNotice(NOTHING_HEARD)
    } else if (result.status !== 'aborted') {
      setNotice(result.error.message)
    }
  }

  const startRecording = async () => {
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      setPhase('idle')
      setNotice("Voice input isn't supported in this browser.")
      return
    }
    try {
      stream.current = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch {
      setPhase('idle')
      setNotice(MIC_BLOCKED)
      return
    }
    const preferred = recordingType()
    const media = new MediaRecorder(stream.current, preferred ? { mimeType: preferred } : undefined)
    chunks.current = []
    media.ondataavailable = (event) => {
      if (event.data.size > 0) chunks.current.push(event.data)
    }
    media.onstop = () => void finishRecording(media.mimeType || preferred || 'audio/webm')
    recorder.current = media
    media.start()
    setPhase('recording')
    startClock()
  }

  // ── the button ──────────────────────────────────────────────────────────────

  const start = () => {
    setNotice(null)
    void startRecording()
  }

  const stop = () => {
    if (recorder.current?.state === 'recording') recorder.current.stop()
  }

  const listening = phase === 'recording'
  const busy = phase === 'finishing'

  const label = listening ? 'Stop' : busy ? 'Writing your words…' : 'Speak your message'

  return (
    <div className="relative shrink-0">
      {notice && (
        <div
          role="status"
          className="absolute bottom-full right-0 mb-2 w-max max-w-[260px] rounded-xl border border-rose/40 bg-surface px-3 py-2 text-[12px] leading-snug text-rose shadow-card"
        >
          {notice}
        </div>
      )}
      {listening && (
        <div className="absolute bottom-full right-0 mb-2 flex items-center gap-1.5 rounded-full bg-ink px-2.5 py-1 text-[11px] font-medium tabular-nums text-white shadow-card">
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-rose" />
          Recording {clock(seconds)}
        </div>
      )}
      <button
        type="button"
        onClick={() => (listening ? stop() : start())}
        disabled={busy || (disabled && !listening)}
        aria-label={label}
        title={label}
        aria-pressed={listening}
        className={
          listening
            ? 'flex h-11 w-11 items-center justify-center rounded-full bg-rose text-white shadow-card transition focus:outline-none focus-visible:ring-2 focus-visible:ring-rose/40'
            : 'flex h-11 w-11 items-center justify-center rounded-full border border-line bg-surface text-muted shadow-card transition hover:border-clay/40 hover:text-clay focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 disabled:cursor-not-allowed disabled:opacity-50'
        }
      >
        {listening ? (
          <StopIcon size={16} />
        ) : busy ? (
          <span className="h-4 w-4 animate-spin rounded-full border-2 border-clay/30 border-t-clay" />
        ) : (
          <MicIcon size={19} />
        )}
      </button>
    </div>
  )
}
