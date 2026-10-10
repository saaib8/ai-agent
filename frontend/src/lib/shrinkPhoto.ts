// A photo of any size can be uploaded: one too big to send is shrunk here
// first. The server scales every photo down to about 2K anyway, so nothing it
// uses is lost - the upload is just faster, and stays under the request limit
// of whatever proxy sits in front of the API.

/** The longest side sent; the server works at this size. */
export const MAX_PHOTO_SIDE = 2048

/** Comfortably under a 1 MB proxy limit once the multipart fields are added. */
export const TARGET_PHOTO_BYTES = 900 * 1024

const QUALITIES = [0.88, 0.8, 0.7, 0.6]
const MAX_ATTEMPTS = 4

/** The photo as it should be uploaded: the file itself when it is already small
 *  enough, otherwise an upright JPEG under `TARGET_PHOTO_BYTES`. A format this
 *  browser cannot draw (HEIC in Chrome) is sent as it is. */
export async function shrinkPhoto(file: File): Promise<File> {
  if (file.size <= TARGET_PHOTO_BYTES) return file
  let bitmap: ImageBitmap
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' })
  } catch {
    return file
  }
  try {
    const longest = Math.max(bitmap.width, bitmap.height)
    let side = Math.min(MAX_PHOTO_SIDE, longest)
    for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
      const scale = side / longest
      const canvas = document.createElement('canvas')
      canvas.width = Math.max(1, Math.round(bitmap.width * scale))
      canvas.height = Math.max(1, Math.round(bitmap.height * scale))
      const context = canvas.getContext('2d')
      if (!context) return file
      // A transparent PNG would turn black as a JPEG; the server flattens on white.
      context.fillStyle = '#ffffff'
      context.fillRect(0, 0, canvas.width, canvas.height)
      context.drawImage(bitmap, 0, 0, canvas.width, canvas.height)
      for (const quality of QUALITIES) {
        const blob = await new Promise<Blob | null>((resolve) =>
          canvas.toBlob(resolve, 'image/jpeg', quality),
        )
        if (blob && blob.size <= TARGET_PHOTO_BYTES) {
          return new File([blob], jpegName(file.name), { type: 'image/jpeg' })
        }
      }
      side = Math.round(side * 0.75)
    }
    return file
  } finally {
    bitmap.close()
  }
}

function jpegName(name: string): string {
  const stem = name.replace(/\.[^.]+$/, '') || 'photo'
  return `${stem}.jpg`
}
