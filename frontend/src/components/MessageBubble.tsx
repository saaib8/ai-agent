import type { ChatResponse } from '../api/types'
import { HelpIcon } from './icons'
import { ComparisonTable } from './presentation/ComparisonTable'
import { ProductGrid } from './presentation/ProductGrid'
import type { SearchRefineControls } from './presentation/ProductGrid'
import { RoomBundle } from './presentation/RoomBundle'
import { RawJson } from './RawJson'

export function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex animate-rise justify-end">
      <div className="max-w-[80%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-ink px-4 py-2.5 text-[15px] leading-relaxed text-white shadow-card">
        {text}
      </div>
    </div>
  )
}

export function ZoryAvatar() {
  return (
    <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-clay font-display text-sm font-semibold text-white">
      Z
    </div>
  )
}

interface AssistantBubbleProps {
  data: ChatResponse
  busy?: boolean
  /** Start swapping a room piece: opens the alternatives picker for that role. */
  onSwapStart?: (bundleOrdinal: number, role: string) => void
  /** Present only while choosing a replacement: turns product cards into pickers. */
  pick?: { role: string; onPick: (alternativeOrdinal: number) => void }
  /** Present only on the latest search grid: "different options" and per-card exclude. */
  refine?: SearchRefineControls
}

export function AssistantBubble({ data, busy, onSwapStart, pick, refine }: AssistantBubbleProps) {
  const { response, presentation } = data
  const hasProducts = !!presentation?.products?.length
  const hasComparison = !!presentation?.comparison
  const hasRoom = !!presentation?.room

  return (
    <div className="flex animate-rise gap-3">
      <ZoryAvatar />
      <div className="flex min-w-0 flex-1 flex-col gap-3">
        <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-tl-md border border-line bg-surface px-4 py-2.5 text-[15px] leading-relaxed text-ink shadow-card">
          {response.message}
        </div>

        {response.follow_up_question && (
          <div className="flex max-w-[85%] items-start gap-2.5 rounded-xl border border-clay/15 bg-clay-soft/40 px-3.5 py-2.5">
            <HelpIcon size={16} className="mt-0.5 shrink-0 text-clay" />
            <div className="text-sm leading-relaxed text-ink">{response.follow_up_question}</div>
          </div>
        )}

        {hasProducts && (
          <ProductGrid products={presentation!.products} pick={pick} refine={refine} busy={busy} />
        )}
        {hasComparison && <ComparisonTable comparison={presentation!.comparison!} />}
        {hasRoom && <RoomBundle room={presentation!.room!} onSwapStart={onSwapStart} busy={busy} />}

        <RawJson value={data} />
      </div>
    </div>
  )
}
