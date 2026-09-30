import type { IconName } from './icons'

export type ViewName =
  | 'home'
  | 'chat'
  | 'room-planner'
  | 'budget'
  | 'catalog'
  | 'photo'
  | 'visualize'
  | 'compare'
  | 'advice'
  | 'room-context'
  | 'basket'

export interface ToolDef {
  id: ViewName
  title: string
  blurb: string
  icon: IconName
  group: 'discover' | 'decide'
}

/** Every tool the home screen and the Tools sheet offer, in display order.
 *  Each one ends in a real agent call — a message, a picks action, a photo
 *  search or a render — never in a screen that only pretends. */
export const TOOLS: ToolDef[] = [
  { id: 'room-planner', title: 'Design my space', blurb: 'Furnish a complete room', icon: 'sofa', group: 'discover' },
  { id: 'budget', title: 'Shop within budget', blurb: 'Stay within your price range', icon: 'wallet', group: 'discover' },
  { id: 'catalog', title: 'Find furniture', blurb: 'Search the catalogue', icon: 'search', group: 'discover' },
  { id: 'photo', title: 'Search by photo', blurb: 'Find visually similar pieces', icon: 'camera', group: 'discover' },
  { id: 'visualize', title: 'Visualize in room', blurb: 'See your room come together', icon: 'cube', group: 'decide' },
  { id: 'compare', title: 'Compare products', blurb: 'Compare pieces side by side', icon: 'columns', group: 'decide' },
  { id: 'advice', title: 'Help me choose', blurb: 'Size, colour & style advice', icon: 'bulb', group: 'decide' },
  { id: 'room-context', title: 'Add room context', blurb: "Tell me your room's size", icon: 'ruler', group: 'decide' },
]

/** Names a merchant may pass to `ZoryAgent.open(tool)`. */
const ALIASES: Record<string, ViewName> = {
  'design-my-space': 'room-planner',
  'room-planner': 'room-planner',
  'shop-within-budget': 'budget',
  budget: 'budget',
  'find-furniture': 'catalog',
  catalog: 'catalog',
  'search-by-photo': 'photo',
  photo: 'photo',
  visualize: 'visualize',
  compare: 'compare',
  'help-me-choose': 'advice',
  advice: 'advice',
  'room-context': 'room-context',
  basket: 'basket',
  chat: 'chat',
}

export function resolveTool(name: string): ViewName | null {
  return ALIASES[name.trim().toLowerCase()] ?? null
}

export const VIEW_TITLES: Record<ViewName, string> = {
  home: '',
  chat: '',
  'room-planner': 'Room Planner',
  budget: 'Shop within budget',
  catalog: 'Browse Catalogue',
  photo: 'Search by photo',
  visualize: 'Visualize in room',
  compare: 'Compare products',
  advice: 'Help me choose',
  'room-context': 'Room context',
  basket: 'Basket',
}
