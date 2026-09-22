import type { ReactNode } from 'react'
import type { HealthState } from '../hooks/useHealth'
import type { UseConfig } from '../hooks/useConfig'
import { HealthBadge } from './HealthBadge'

interface SettingsPopoverProps {
  config: UseConfig
  health: HealthState
  onCheckHealth: () => void
  revision: number | null
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-muted">
        {label}
      </label>
      {children}
    </div>
  )
}

const inputCls =
  'w-full rounded-lg border border-line bg-surface px-2.5 py-2 text-sm text-ink transition placeholder:text-muted/60 focus:border-clay/50 focus:outline-none focus:ring-2 focus:ring-clay/15'

/** All connection / session settings, relocated from the former sidebar. */
export function SettingsPopover({ config, health, onCheckHealth, revision }: SettingsPopoverProps) {
  const { config: c, set } = config

  return (
    <div className="flex flex-col gap-4 p-4">
      <div>
        <h2 className="text-sm font-semibold text-ink">Settings</h2>
        <p className="text-xs text-muted">Connection, store scope &amp; session.</p>
      </div>

      <HealthBadge health={health} onRefresh={onCheckHealth} />

      <Field label="API base URL">
        <input
          type="text"
          value={c.apiBase}
          onChange={(e) => set('apiBase', e.target.value)}
          placeholder="(dev proxy — leave blank)"
          className={inputCls}
        />
        <p className="mt-1 text-[11px] leading-snug text-muted">
          Blank routes through the Vite proxy (no CORS). An absolute URL calls a backend directly.
        </p>
      </Field>

      <div className="grid grid-cols-2 gap-3">
        <Field label="store_id">
          <input
            type="number"
            min={1}
            value={c.storeId}
            onChange={(e) => set('storeId', Number(e.target.value))}
            className={inputCls}
          />
        </Field>
        <Field label="session_id">
          <input
            type="text"
            value={c.sessionId}
            onChange={(e) => set('sessionId', e.target.value)}
            className={`${inputCls} font-mono text-xs`}
          />
        </Field>
      </div>

      <label className="flex cursor-pointer items-start gap-2.5 rounded-lg border border-line bg-surface p-3">
        <input
          type="checkbox"
          checked={c.sendExpectedRevision}
          onChange={(e) => set('sendExpectedRevision', e.target.checked)}
          className="mt-0.5 h-4 w-4 accent-clay"
        />
        <span className="text-xs leading-snug text-ink">
          Send <code className="text-[11px] text-muted">expected_session_revision</code>
          <span className="mt-0.5 block text-[11px] text-muted">
            Optimistic-concurrency guard. Off is correct for a plain chat box.
          </span>
        </span>
      </label>

      <div className="flex items-center justify-between border-t border-line pt-3 text-[11px] text-muted">
        <span>Session revision</span>
        <span className="font-mono text-ink">{revision ?? '—'}</span>
      </div>
    </div>
  )
}
