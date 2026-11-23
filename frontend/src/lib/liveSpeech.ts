import type { LiveSpeechSession } from '../api/types'

// Live voice input: the microphone streamed to ElevenLabs over a WebSocket,
// with a single-use token the API minted. The words grow as the customer
// speaks - `partial_transcript` replaces the words still being heard, and
// `committed_transcript` settles them at each pause.

/** How long stop waits for the last words to settle before giving up. */
const SETTLE_MS = 4000
/** Audio is sent in 100 ms pieces. */
const CHUNKS_PER_SECOND = 10

// Runs on the audio thread: averages the microphone down to the rate the
// stream expects and hands back 16-bit pieces. Inline, so nothing is served.
const WORKLET = `
class LivePcm extends AudioWorkletProcessor {
  constructor(options) {
    super()
    this.step = sampleRate / options.processorOptions.rate
    this.size = options.processorOptions.size
    this.next = this.step; this.at = 0; this.sum = 0; this.count = 0; this.out = []
    this.port.onmessage = () => this.flush()
  }
  flush() {
    if (!this.out.length) return
    const pcm = new Int16Array(this.out.length)
    for (let i = 0; i < this.out.length; i++) {
      const s = Math.max(-1, Math.min(1, this.out[i]))
      pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff
    }
    this.out = []
    this.port.postMessage(pcm.buffer, [pcm.buffer])
  }
  process(inputs) {
    const channel = inputs[0] && inputs[0][0]
    if (!channel) return true
    for (let i = 0; i < channel.length; i++) {
      this.sum += channel[i]; this.count++; this.at++
      if (this.at >= this.next) {
        this.out.push(this.sum / this.count)
        this.sum = 0; this.count = 0; this.next += this.step
      }
    }
    if (this.out.length >= this.size) this.flush()
    return true
  }
}
registerProcessor('live-pcm', LivePcm)
`

const ERROR_TYPES = new Set([
  'error',
  'auth_error',
  'quota_exceeded',
  'commit_throttled',
  'unaccepted_terms',
  'rate_limited',
  'queue_overflow',
  'resource_exhausted',
  'session_time_limit_exceeded',
  'input_error',
  'invalid_request',
  'chunk_size_exceeded',
  'transcriber_error',
])

export interface LiveSpeech {
  /** Stop listening; resolves with the final words once they settle. */
  stop: () => Promise<string>
  /** Drop everything at once (the page is closing). */
  cancel: () => void
}

interface LiveHandlers {
  /** Every word heard so far, settled and still-changing together. */
  onText: (text: string) => void
  /** The stream ended on its own; the words so far stay with the customer. */
  onFailure: (message: string) => void
}

/** Whether this browser can stream at all; otherwise the caller records. */
export function liveSpeechSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof AudioWorkletNode !== 'undefined' &&
    typeof WebSocket !== 'undefined' &&
    Boolean(navigator.mediaDevices?.getUserMedia)
  )
}

function toBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer)
  let binary = ''
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  }
  return btoa(binary)
}

function streamUrl(session: LiveSpeechSession): string {
  const query = new URLSearchParams({
    model_id: session.model_id,
    token: session.token,
    audio_format: session.audio_format,
    commit_strategy: session.commit_strategy,
  })
  return `${session.url}?${query}`
}

/**
 * Open the microphone and the stream. Rejects - with nothing left running -
 * when the microphone is refused or the stream cannot open, so the caller can
 * fall back to recording.
 */
export async function startLiveSpeech(
  session: LiveSpeechSession,
  handlers: LiveHandlers,
): Promise<LiveSpeech> {
  const settled: string[] = []
  let hearing = ''
  let done = false
  let settle: (() => void) | null = null

  const words = () => [...settled, hearing].filter(Boolean).join(' ').trim()

  const microphone = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  })
  const context = new AudioContext()
  const release = () => {
    microphone.getTracks().forEach((track) => track.stop())
    void context.close().catch(() => undefined)
  }

  // Listening starts before the stream opens: what they say in the first
  // moment is held and sent once it does, so no opening words are lost.
  const held: string[] = []
  let socket: WebSocket | null = null
  let node: AudioWorkletNode
  const send = (audio: string, commit: boolean) => {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      if (!commit) held.push(audio)
      return
    }
    socket.send(
      JSON.stringify({
        message_type: 'input_audio_chunk',
        audio_base_64: audio,
        commit,
        sample_rate: session.sample_rate,
      }),
    )
  }
  try {
    const module = URL.createObjectURL(new Blob([WORKLET], { type: 'text/javascript' }))
    await context.audioWorklet.addModule(module)
    URL.revokeObjectURL(module)
    node = new AudioWorkletNode(context, 'live-pcm', {
      processorOptions: { rate: session.sample_rate, size: session.sample_rate / CHUNKS_PER_SECOND },
    })
    node.port.onmessage = (event: MessageEvent<ArrayBuffer>) => send(toBase64(event.data), false)
    context.createMediaStreamSource(microphone).connect(node)
    socket = await new Promise<WebSocket>((resolve, reject) => {
      const ws = new WebSocket(streamUrl(session))
      ws.onopen = () => resolve(ws)
      ws.onerror = () => reject(new Error('live stream did not open'))
    })
  } catch (err) {
    release()
    throw err
  }
  const open = socket
  for (const audio of held.splice(0)) send(audio, false)

  const finish = () => {
    if (done) return
    done = true
    node.port.onmessage = null
    release()
    if (open.readyState === WebSocket.OPEN) open.close()
    settle?.()
  }

  open.onmessage = (event: MessageEvent<string>) => {
    let message: { message_type?: string; text?: string }
    try {
      message = JSON.parse(event.data)
    } catch {
      return
    }
    const kind = message.message_type ?? ''
    if (kind === 'partial_transcript') {
      hearing = (message.text ?? '').trim()
      handlers.onText(words())
    } else if (kind === 'committed_transcript') {
      const text = (message.text ?? '').trim()
      if (text) settled.push(text)
      hearing = ''
      handlers.onText(words())
      settle?.()
    } else if (ERROR_TYPES.has(kind)) {
      const quiet = done
      finish()
      if (!quiet) handlers.onFailure('Live voice input stopped. Your words so far are kept.')
    }
  }
  open.onclose = () => {
    if (done) return
    finish()
    handlers.onFailure('Live voice input stopped. Your words so far are kept.')
  }

  return {
    stop: async () => {
      if (done) return words()
      // Send what is still on the audio thread, then ask for the last words.
      node.port.postMessage('flush')
      await new Promise((resolve) => setTimeout(resolve, 150))
      const waiting = new Promise<void>((resolve) => {
        settle = resolve
      })
      send('', true)
      await Promise.race([waiting, new Promise((resolve) => setTimeout(resolve, SETTLE_MS))])
      // Anything still being heard when it closed counts: it was said.
      if (hearing) {
        settled.push(hearing)
        hearing = ''
      }
      finish()
      return words()
    },
    cancel: finish,
  }
}
