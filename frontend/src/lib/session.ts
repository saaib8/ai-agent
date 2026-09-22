// Generate a session_id that satisfies the backend's rule:
//   [A-Za-z0-9][A-Za-z0-9_.-]{0,127}
export function newSessionId(): string {
  const raw = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`
  const slug = raw.replace(/[^A-Za-z0-9]/g, '').slice(0, 12)
  return `web-${slug}`
}
