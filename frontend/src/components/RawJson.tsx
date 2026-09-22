export function RawJson({ value }: { value: unknown }) {
  return (
    <details className="group mt-1">
      <summary className="cursor-pointer select-none text-[11px] text-muted transition hover:text-ink">
        raw response
      </summary>
      <pre className="mt-1.5 max-h-80 overflow-auto rounded-lg border border-line bg-ink/95 p-3 text-[11px] leading-relaxed text-canvas">
        {JSON.stringify(value, null, 2)}
      </pre>
    </details>
  )
}
