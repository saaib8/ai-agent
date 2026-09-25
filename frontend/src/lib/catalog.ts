import type { CatalogItem, RenderView, RoomType } from '../api/types'
import { humanise, money, toNumber } from './format'

/** One product in the customer's selection, and how many of it. */
export interface SelectedPiece {
  item: CatalogItem
  quantity: number
}

/** The room as the customer is setting it up: sides stay strings while typed. */
export interface RoomDraft {
  roomType: RoomType
  style: string
  length: string
  width: string
  view: RenderView
}

/** The style is left empty: it is filled from the store's own most common
 *  style once the catalogue's facets arrive, never from a copy kept here. */
export const DEFAULT_ROOM: RoomDraft = {
  roomType: 'living_room',
  style: '',
  length: '4',
  width: '5',
  view: 'corner',
}

export const ROOM_PRESETS: [number, number][] = [
  [3, 4],
  [4, 5],
  [5, 6],
]

/** A side in metres within the allowed range, or null. */
export function parseSide(value: string, min: number, max: number): number | null {
  const n = Number(value)
  return Number.isFinite(n) && n >= min && n <= max ? n : null
}

/** "8,450 SAR", summed per currency so amounts are never added across them. */
export function selectionTotal(pieces: SelectedPiece[]): string | null {
  const byCurrency = new Map<string, number>()
  for (const { item, quantity } of pieces) {
    const amount = toNumber(item.price_amount)
    if (amount == null) continue
    byCurrency.set(item.price_unit, (byCurrency.get(item.price_unit) ?? 0) + amount * quantity)
  }
  const parts = [...byCurrency].map(([unit, sum]) => money(String(sum), unit))
  return parts.length ? parts.join(' + ') : null
}

export function unitCount(pieces: SelectedPiece[]): number {
  return pieces.reduce((n, p) => n + p.quantity, 0)
}

export interface FitCheck {
  floorM2: number
  coveredM2: number
  /** Share of the floor the counted pieces cover; can exceed 1. */
  ratio: number
  level: 'ok' | 'crowded' | 'over'
  /** Pieces that cannot fit the room whichever way they are turned. */
  tooBig: { name: string; lengthM: number }[]
  /** Floor pieces with no usable size, so not counted. */
  uncounted: number
}

/**
 * How much floor the selection covers. Advisory: it warns, it never blocks.
 *
 * Only floor furniture counts, at its catalog footprint times its quantity.
 * A piece fits a rectangular room, turned square to the walls, only when its
 * short side fits the room's short side and its long side the long side.
 */
export function fitCheck(
  pieces: SelectedPiece[],
  lengthM: number,
  widthM: number,
  crowdedRatio: number,
): FitCheck {
  const floorM2 = lengthM * widthM
  const roomShort = Math.min(lengthM, widthM) * 100
  const roomLong = Math.max(lengthM, widthM) * 100
  let coveredCm2 = 0
  let uncounted = 0
  const tooBig: FitCheck['tooBig'] = []
  for (const { item, quantity } of pieces) {
    if (!item.stands_on_floor) continue
    if (item.footprint_cm2 == null || item.longest_side_cm == null) {
      uncounted += quantity
      continue
    }
    coveredCm2 += item.footprint_cm2 * quantity
    const long = item.longest_side_cm
    const short = item.footprint_cm2 / long
    if (long > roomLong || short > roomShort) {
      tooBig.push({ name: item.name_english, lengthM: Math.round(long) / 100 })
    }
  }
  const coveredM2 = coveredCm2 / 10_000
  const ratio = floorM2 > 0 ? coveredM2 / floorM2 : 0
  const level = ratio > 1 ? 'over' : ratio > crowdedRatio ? 'crowded' : 'ok'
  return { floorM2, coveredM2, ratio, level, tooBig, uncounted }
}

/** "Modern_Classic" → "Modern Classic". */
export function styleLabel(style: string): string {
  return humanise(style)
}
