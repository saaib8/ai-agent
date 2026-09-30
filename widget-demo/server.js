// A pretend merchant store that installs the ZORY widget with its snippet.
//
//   node widget-demo/server.js                      → http://localhost:5600
//   WIDGET_ORIGIN=https://stage.ai-agent.zory.ai node widget-demo/server.js
//
// It runs on its own origin on purpose: the widget is a cross-origin iframe on
// a real store, and testing it same-origin would hide exactly the things that
// break there (postMessage origins, storage, framing).
//
// index.html carries the snippet pointing at the local widget
// (http://localhost:3000, the ai-agent frontend's dev server). WIDGET_ORIGIN
// swaps that for another host — e.g. the deployed stage — without editing it.

const http = require('node:http')
const fs = require('node:fs')
const path = require('node:path')

const ROOT = __dirname
const PORT = Number(process.env.PORT) || 5600
const DEFAULT_ORIGIN = 'http://localhost:3000'
const WIDGET_ORIGIN = (process.env.WIDGET_ORIGIN || DEFAULT_ORIGIN).replace(/\/+$/, '')
const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
}

http
  .createServer((req, res) => {
    const pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname)
    const file = path.join(ROOT, pathname === '/' ? 'index.html' : pathname)
    if (!file.startsWith(ROOT + path.sep) || path.basename(file) === 'server.js') {
      res.writeHead(404).end('Not found')
      return
    }
    fs.readFile(file, (err, body) => {
      if (err) {
        res.writeHead(404).end('Not found')
        return
      }
      const type = TYPES[path.extname(file)] || 'application/octet-stream'
      const out = file.endsWith('.html')
        ? body.toString('utf8').split(DEFAULT_ORIGIN).join(WIDGET_ORIGIN)
        : body
      res.writeHead(200, { 'Content-Type': type, 'Cache-Control': 'no-store' }).end(out)
    })
  })
  .listen(PORT, () => {
    console.log(`Oakline demo store: http://localhost:${PORT}  (widget from ${WIDGET_ORIGIN})`)
  })
