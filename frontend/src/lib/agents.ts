// The four assistants a reply can be shown as. The server decides which
// (`ChatResponse.agent`); this is only how each one looks. One assistant
// answers every turn behind them.

export type AgentKey = 'sara' | 'salman' | 'nora' | 'faysal'

export interface AgentLook {
  name: string
  initial: string
  /** Avatar colours: Sara and Salman share an initial, so colour tells them apart. */
  avatar: string
}

export const AGENTS: Record<AgentKey, AgentLook> = {
  sara: { name: 'Sara', initial: 'S', avatar: 'bg-clay text-white' },
  salman: { name: 'Salman', initial: 'S', avatar: 'bg-ink text-white' },
  nora: { name: 'Nora', initial: 'N', avatar: 'bg-sage text-white' },
  /** The store's support: its own policies, and the hand-over to its team. */
  faysal: { name: 'Faysal', initial: 'F', avatar: 'bg-amber text-white' },
}

/** How a reply's assistant looks; Sara, the session's own voice, when none is named. */
export function agentLook(agent: string | null | undefined): AgentLook {
  return AGENTS[agent as AgentKey] ?? AGENTS.sara
}
