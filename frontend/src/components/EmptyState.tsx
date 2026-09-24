import { useRef } from 'react'
import type { ComponentType, SVGProps } from 'react'
import { PHOTO_TYPES } from './Composer'
import { HelpIcon, ImageIcon, RoomIcon, SearchIcon, SendIcon, SofaIcon, SparkIcon } from './icons'
import { Pill } from './ui/Pill'

type IconType = ComponentType<SVGProps<SVGSVGElement> & { size?: number }>

interface Suggestion {
  icon: IconType
  tag: string
  label: string
  prompt: string
}

const FEATURE = {
  icon: RoomIcon,
  title: 'Furnish a whole room',
  description:
    "Give me a room size, a budget and a style — I'll propose a complete, coherent bundle from the catalog.",
  prompt: 'Furnish my 4x5m bedroom under SAR 12,000 in a modern style',
}

const SUGGESTIONS: Suggestion[] = [
  {
    icon: SearchIcon,
    tag: 'Discovery',
    label: 'A modern sofa under 6,000 SAR',
    prompt: 'Show me a modern sofa under 6000 SAR',
  },
  {
    icon: SparkIcon,
    tag: 'Semantic',
    label: 'Something comfortable for reading',
    prompt: 'I want something comfortable for reading',
  },
  {
    icon: SofaIcon,
    tag: 'Discovery',
    label: 'Dining chairs under 800 SAR',
    prompt: 'Show me dining chairs under 800 SAR',
  },
  {
    icon: HelpIcon,
    tag: 'Design Q&A',
    label: 'How big should a rug be under a sofa?',
    prompt: 'How big should a rug be under a sofa?',
  },
]

interface EmptyStateProps {
  storeId: number
  onPick: (prompt: string) => void
  onPhoto: (file: File) => void
}

export function EmptyState({ storeId, onPick, onPhoto }: EmptyStateProps) {
  const fileRef = useRef<HTMLInputElement>(null)
  return (
    <div className="mx-auto max-w-3xl px-4 py-10 sm:py-14">
      <div className="flex flex-col items-center text-center">
        <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-clay text-white shadow-soft">
          <SofaIcon size={28} />
        </div>
        <h1 className="mt-5 font-display text-[28px] leading-tight text-ink sm:text-[32px]">
          How can I help you furnish your space?
        </h1>
        <p className="mt-3 max-w-md text-sm leading-relaxed text-muted">
          ZORY finds real products from the retailer&apos;s catalog, compares options, and designs
          whole rooms — grounded in verified data, never guessed.
        </p>
        <div className="mt-4 flex flex-wrap justify-center gap-2">
          <Pill>Store {storeId}</Pill>
          <Pill>Real merchant catalog</Pill>
          <Pill>Two specialist agents</Pill>
        </div>
      </div>

      <button
        onClick={() => onPick(FEATURE.prompt)}
        className="group mt-8 flex w-full items-center gap-4 rounded-2xl border border-clay/25 bg-clay-soft/40 p-4 text-left transition hover:border-clay/40 hover:bg-clay-soft/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
      >
        <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-clay text-white">
          <FEATURE.icon size={22} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-semibold text-ink">{FEATURE.title}</span>
          <span className="mt-0.5 block text-xs leading-relaxed text-muted">{FEATURE.description}</span>
        </span>
        <SendIcon size={18} className="shrink-0 text-clay transition group-hover:translate-x-0.5" />
      </button>

      <input
        ref={fileRef}
        type="file"
        accept={PHOTO_TYPES}
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0]
          e.target.value = ''
          if (file) onPhoto(file)
        }}
      />
      <button
        onClick={() => fileRef.current?.click()}
        className="group mt-3 flex w-full items-center gap-4 rounded-2xl border border-line bg-surface p-4 text-left shadow-card transition hover:border-clay/40 hover:bg-surface-muted/50 focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
      >
        <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-clay-soft/60 text-clay">
          <ImageIcon size={22} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-semibold text-ink">Find furniture in the image</span>
        </span>
        <SendIcon size={18} className="shrink-0 text-clay transition group-hover:translate-x-0.5" />
      </button>

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        {SUGGESTIONS.map((s) => (
          <button
            key={s.prompt}
            onClick={() => onPick(s.prompt)}
            className="group flex items-start gap-3 rounded-2xl border border-line bg-surface p-4 text-left shadow-card transition hover:border-line-strong hover:bg-surface-muted/50 focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
          >
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-clay-soft/60 text-clay">
              <s.icon size={18} />
            </span>
            <span className="min-w-0">
              <span className="block text-[11px] font-semibold uppercase tracking-wide text-muted">
                {s.tag}
              </span>
              <span className="mt-0.5 block text-sm text-ink group-hover:text-clay">{s.label}</span>
            </span>
          </button>
        ))}
      </div>
    </div>
  )
}
