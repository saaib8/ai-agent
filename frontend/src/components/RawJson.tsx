/** A render's picture is a data URL of a few hundred KB: shown whole, it would
 *  bury everything else in the response. */
function shorten(_key: string, value: unknown): unknown {
  if (typeof value === 'string' && value.startsWith('data:') && value.length > 120) {
    return `${value.slice(0, 40)}… (${Math.round(value.length / 1024)} KB)`
  }
  return value
}

export function RawJson({ value }: { value: unknown }) {
  return (
    <details className="group mt-1">
      <summary className="cursor-pointer select-none text-[11px] text-muted transition hover:text-ink">
        raw response
      </summary>
      <pre className="mt-1.5 max-h-80 overflow-auto rounded-lg border border-line bg-ink/95 p-3 text-[11px] leading-relaxed text-canvas">
        {JSON.stringify(value, shorten, 2)}
      </pre>
    </details>
  )
}
