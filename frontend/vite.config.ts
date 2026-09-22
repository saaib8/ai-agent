import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The backend only enables CORS when ZORY_API__CORS_ORIGINS is set, and the
// working .env leaves it empty. Rather than require a backend change, we proxy
// the API server-side: the browser calls same-origin (/v1, /health), Vite
// forwards to the FastAPI service, and CORS never enters the picture.
//
// Override the target without editing this file:  VITE_PROXY_TARGET=... npm run dev
const target = process.env.VITE_PROXY_TARGET ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 3000,
    // If 3000 is taken, Vite picks the next free port — the proxy still works,
    // so the frontend origin no longer has to match a CORS allowlist.
    strictPort: false,
    proxy: {
      '/v1': { target, changeOrigin: true },
      '/health': { target, changeOrigin: true },
      '/openapi.json': { target, changeOrigin: true },
    },
  },
})
