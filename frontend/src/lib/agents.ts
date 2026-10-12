// The three assistants a reply can be shown as. The server decides which
// (`ChatResponse.agent`); this is only how each one looks. One assistant
// answers every turn behind them.

export type AgentKey = 'sarah' | 'salman' | 'nora'

export interface AgentLook {
  name: string
  initial: string
  /** Avatar colours: Sarah and Salman share an initial, so colour tells them apart. */
  avatar: string
}

export const AGENTS: Record<AgentKey, AgentLook> = {
  sarah: { name: 'Sarah', initial: 'S', avatar: 'bg-clay text-white' },
  salman: { name: 'Salman', initial: 'S', avatar: 'bg-ink text-white' },
  nora: { name: 'Nora', initial: 'N', avatar: 'bg-sage text-white' },
}

/** How a reply's assistant looks; Sarah, the session's own voice, when none is named. */
export function agentLook(agent: string | null | undefined): AgentLook {
  return AGENTS[agent as AgentKey] ?? AGENTS.sarah
}
