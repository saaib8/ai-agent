import type { HealthState } from '../hooks/useHealth'
import { RefreshIcon } from './icons'

const DOT: Record<HealthState['status'], string> = {
  unknown: 'bg-muted',
  checking: 'bg-amber animate-pulse',
  ok: 'bg-sage',
  degraded: 'bg-amber',
  unreachable: 'bg-rose',
}

function label(health: HealthState): string {
  switch (health.status) {
    case 'unknown':
      return 'Health unknown'
    case 'checking':
      return 'Checking…'
    case 'ok':
      return `Healthy · ${health.report.environment}`
    case 'degraded':
      return 'Degraded'
    case 'unreachable':
      return 'Unreachable'
  }
}

export function HealthBadge({ health, onRefresh }: { health: HealthState; onRefresh: () => void }) {
  const deps = health.status === 'ok' || health.status === 'degraded' ? health.report.dependencies : null

  return (
    <div className="rounded-xl border border-line bg-surface-muted/50 p-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className={`h-2.5 w-2.5 rounded-full ${DOT[health.status]}`} />
          <span className="text-xs font-medium text-ink">{label(health)}</span>
        </div>
        <button
          onClick={onRefresh}
          aria-label="Refresh health"
          className="inline-flex h-7 w-7 items-center justify-center rounded-lg text-muted transition hover:bg-surface hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-clay/40"
        >
          <RefreshIcon size={15} />
        </button>
      </div>

      {deps && (
        <ul className="mt-2 space-y-1">
          {Object.entries(deps).map(([name, dependencyStatus]) => (
            <li key={name} className="flex items-center justify-between text-[11px]">
              <span className="text-muted">{name}</span>
              <span
                className={
                  dependencyStatus === 'ok' || dependencyStatus === 'up'
                    ? 'font-medium text-sage'
                    : 'font-medium text-rose'
                }
              >
                {dependencyStatus}
              </span>
            </li>
          ))}
        </ul>
      )}

      {health.status === 'unreachable' && (
        <p className="mt-2 text-[11px] leading-snug text-muted">
          Is the service running? The dev proxy targets <code className="text-ink">localhost:8000</code>.
        </p>
      )}
    </div>
  )
}
