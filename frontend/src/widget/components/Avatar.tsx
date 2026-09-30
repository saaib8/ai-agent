import { useState } from 'react'

/** The assistant's face: the merchant's image when they configured one,
 *  otherwise a monogram in the brand colour. */
export function Avatar({ name, url, size = 36 }: { name: string; url: string | null; size?: number }) {
  const [failed, setFailed] = useState(false)
  return (
    <span className="avatar" style={{ width: size, height: size, fontSize: size * 0.42 }} aria-hidden="true">
      {url && !failed ? (
        <img src={url} alt="" onError={() => setFailed(true)} />
      ) : (
        name.charAt(0).toUpperCase()
      )}
    </span>
  )
}
