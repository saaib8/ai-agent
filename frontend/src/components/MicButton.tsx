import { useEffect, useRef, useState } from 'react'
import type { Fetched } from '../api/client'
import type { LiveSpeechSession, SpeechTextResponse } from '../api/types'
import { liveSpeechSupported, startLiveSpeech } from '../lib/liveSpeech'
import type { LiveSpeech } from '../lib/liveSpeech'
import { MicIcon, StopIcon } from './icons'

const NOTICE_MS = 5000
const NOTHING_HEARD = "I didn't catch that. Please try again."
const MIC_BLOCKED = 'Allow microphone access to speak your message.'

// The first one the browser can record: Chrome and Firefox take webm/opus,
// Safari records mp4. None supported: the browser's own default.
const RECORDING_TYPES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg']

/** live: words appear while they speak. recording: words come after stop. */
type Phase = 'idle' | 'connecting' | 'live' | 'recording' | 'finishing'

interface MicButtonProps {
  /** Send a recording; the answer is the words heard. The fallback. */
  onRecorded: (audio: Blob, filename: string) => Promise<Fetched<SpeechTextResponse>>
  /** The words heard from a recording, for the composer. Never sent here. */
  onText: (text: string) => void
  /** Ask for a live session; without it, the mic always records. */
  onLiveSession?: () => Promise<Fetched<LiveSpeechSession>>
  /** Live listening began: what is in the box now stays in front of the words. */
  onLiveStart?: () => void
  /** Every word heard so far, while they speak - replaces the last call's. */
  onLiveText?: (text: string) => void
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

function microphoneRefused(err: unknown): boolean {
  return err instanceof DOMException && (err.name === 'NotAllowedError' || err.name === 'SecurityError')
}

export function MicButton({
  onRecorded,
  onText,
  onLiveSession,
  onLiveStart,
  onLiveText,
  disabled,
}: MicButtonProps) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [seconds, setSeconds] = useState(0)
  const [notice, setNotice] = useState<string | null>(null)
  const recorder = useRef<MediaRecorder | null>(null)
  const stream = useRef<MediaStream | null>(null)
  const chunks = useRef<Blob[]>([])
  const live = useRef<LiveSpeech | null>(null)
  const ticker = useRef<number | null>(null)

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

  // Leaving the page while listening must not leave the microphone on.
  useEffect(
    () => () => {
      releaseRecording()
      live.current?.cancel()
    },
    [],
  )

  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(null), NOTICE_MS)
    return () => window.clearTimeout(timer)
  }, [notice])

  // ── record, then transcribe (the fallback) ─────────────────────────────────

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
      if (text) onText(text)
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

  // ── live: words while they speak ───────────────────────────────────────────

  /** True when live listening started; false sends the caller to recording. */
  const startLive = async (): Promise<boolean> => {
    if (!onLiveSession || !liveSpeechSupported()) return false
    const session = await onLiveSession()
    if (!session.ok) return false
    onLiveStart?.()
    try {
      live.current = await startLiveSpeech(session.data, {
        onText: (text) => onLiveText?.(text),
        onFailure: (message) => {
          live.current = null
          stopClock()
          setPhase('idle')
          setNotice(message)
        },
      })
    } catch (err) {
      if (microphoneRefused(err)) {
        setPhase('idle')
        setNotice(MIC_BLOCKED)
        return true
      }
      return false
    }
    setPhase('live')
    startClock()
    return true
  }

  const stopLive = async () => {
    const listening = live.current
    live.current = null
    stopClock()
    if (!listening) return
    setPhase('finishing')
    const text = await listening.stop()
    onLiveText?.(text)
    setPhase('idle')
    if (!text) setNotice(NOTHING_HEARD)
  }

  // ── the button ──────────────────────────────────────────────────────────────

  const start = async () => {
    setNotice(null)
    setPhase('connecting')
    if (await startLive()) return
    await startRecording()
  }

  const stop = () => {
    if (phase === 'live') void stopLive()
    else if (recorder.current?.state === 'recording') recorder.current.stop()
  }

  const listening = phase === 'live' || phase === 'recording'
  const busy = phase === 'connecting' || phase === 'finishing'

  const label = listening ? 'Stop' : busy ? 'One moment…' : 'Speak your message'

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
          {phase === 'live' ? 'Listening' : 'Recording'} {clock(seconds)}
        </div>
      )}
      <button
        type="button"
        onClick={() => (listening ? stop() : void start())}
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
