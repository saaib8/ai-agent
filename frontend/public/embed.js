/*
 * ZORY AI shopping assistant — embed loader.
 *
 *   <script src="https://<zory-agent-host>/embed.js"
 *           data-store-id="50"
 *           data-store-name="Oakline"
 *           async></script>
 *
 * Draws a launcher ("Ask Nora") in the corner of the store's page and, on the
 * first click, an iframe holding the assistant (<host>/widget/). The iframe is
 * created lazily and kept alive when closed, so a reply in progress survives
 * the shopper collapsing the panel.
 *
 * Options (data- attributes, or window.zoryAgentSettings before this script):
 *   data-store-id        required — the ZORY store whose catalogue to use
 *   data-store-name      shown as "Your <store> assistant"
 *   data-assistant-name  default "Nora"
 *   data-accent          brand colour, hex (default #3e8da8)
 *   data-avatar          https URL of the assistant's picture
 *   data-position        "right" (default) or "left"
 *   data-greeting        teaser text beside the launcher; "off" hides it
 *   data-open            "true" opens the panel on load
 *   data-origin          where the widget is hosted (default: this script's origin)
 *   data-api             where the /v1 API is, e.g. https://stage.ai-agent.zory.ai
 *                        (default: the widget's own host). A different host
 *                        must allow the widget's origin in its CORS settings.
 *
 * Page API (calls made before load are queued):
 *   ZoryAgent.open(tool?)   tool: room-planner, budget, catalog, photo, visualize,
 *                           compare, advice, room-context, basket, chat
 *   ZoryAgent.ask(message)  open and send a question
 *   ZoryAgent.close() / toggle() / isOpen()
 *   ZoryAgent.on(event, fn) events: open, close, basket:updated — returns unsubscribe
 */
(function () {
  'use strict'

  var existing = window.ZoryAgent
  if (existing && existing.__loaded) return

  var SOURCE = 'zory-agent'
  var Z = 2147483000
  var MOBILE = 640
  var script =
    document.currentScript ||
    (function () {
      var all = document.querySelectorAll('script[src*="embed.js"]')
      return all[all.length - 1] || null
    })()
  var settings = window.zoryAgentSettings || {}

  function attr(name) {
    return (script && script.getAttribute('data-' + name)) || ''
  }
  function option(key, dataName) {
    var value = settings[key]
    return value != null && value !== '' ? String(value) : attr(dataName)
  }
  // Absolute URLs only: resolving "" against the page would silently point
  // the widget at the store's own origin.
  function originOf(url) {
    if (!url || !/^https?:\/\//i.test(url)) return ''
    try {
      return new URL(url).origin
    } catch (e) {
      return ''
    }
  }

  var storeId = option('storeId', 'store-id')
  if (!/^\d+$/.test(storeId)) {
    console.error('[ZoryAgent] data-store-id is required (a number).')
    return
  }
  var origin = originOf(option('origin', 'origin')) || (script && script.src ? originOf(script.src) : '')
  if (!/^https?:\/\//.test(origin)) {
    console.error('[ZoryAgent] Could not tell where the widget is hosted. Set data-origin.')
    return
  }

  var HEX = /^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/
  var assistantName = (option('assistantName', 'assistant-name') || 'Nora').slice(0, 24)
  var storeName = (option('storeName', 'store-name') || 'store').slice(0, 40)
  var accent = HEX.test(option('accent', 'accent')) ? option('accent', 'accent') : '#3e8da8'
  var avatar = option('avatar', 'avatar')
  if (avatar && !/^https:\/\//.test(avatar)) avatar = ''
  var side = option('position', 'position') === 'left' ? 'left' : 'right'
  var greetingOption = option('greeting', 'greeting')
  var greeting = greetingOption === 'off' ? '' : greetingOption || 'A little help with your home?'
  var autoOpen = option('open', 'open') === 'true'
  var apiOrigin = originOf(option('api', 'api'))

  // ── styles (all under one prefix so nothing touches the store's page) ──────

  var css =
    '.zory-agent-launcher{position:fixed;bottom:24px;' + side + ':24px;z-index:' + Z + ';display:flex;align-items:center;gap:10px;height:52px;padding:0 20px 0 8px;border:0;border-radius:16px;background:#17191c;color:#fff;font:600 16px/1 "DM Sans",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;cursor:pointer;box-shadow:0 10px 30px rgba(24,33,45,.28),0 2px 6px rgba(24,33,45,.12);transition:transform .15s ease,box-shadow .15s ease;-webkit-font-smoothing:antialiased}' +
    '.zory-agent-launcher:hover{transform:translateY(-1px);box-shadow:0 14px 36px rgba(24,33,45,.32),0 2px 6px rgba(24,33,45,.12)}' +
    '.zory-agent-launcher:focus-visible,.zory-agent-teaser:focus-visible{outline:3px solid ' + accent + ';outline-offset:3px}' +
    '.zory-agent-face{position:relative;display:grid;place-items:center;width:36px;height:36px;border-radius:50%;overflow:hidden;background:linear-gradient(145deg,#fff,' + accent + ');color:#17191c;font-weight:700;font-size:15px;line-height:1;box-shadow:0 0 0 2px rgba(255,255,255,.9)}' +
    '.zory-agent-face img{width:100%;height:100%;object-fit:cover}' +
    '.zory-agent-dot{position:absolute;top:-3px;' + side + ':-3px;min-width:18px;height:18px;padding:0 5px;border-radius:999px;background:#e5484d;color:#fff;font:700 11px/18px system-ui,sans-serif;text-align:center;box-shadow:0 0 0 2px #fff;display:none}' +
    '.zory-agent-teaser{position:fixed;bottom:88px;' + side + ':24px;z-index:' + Z + ';max-width:260px;padding:10px 14px;border:1px solid #dce0e5;border-radius:14px;background:#fff;color:#3c434b;font:400 13px/1.4 "DM Sans",ui-sans-serif,system-ui,sans-serif;text-align:left;cursor:pointer;box-shadow:0 8px 24px rgba(24,33,45,.12);opacity:0;transform:translateY(6px);transition:opacity .25s ease,transform .25s ease;pointer-events:none}' +
    '.zory-agent-teaser.is-on{opacity:1;transform:none;pointer-events:auto}' +
    '.zory-agent-panel{position:fixed;bottom:24px;' + side + ':24px;z-index:' + Z + ';width:376px;height:min(676px,calc(100vh - 48px));max-width:calc(100vw - 32px);border:1px solid #ccd2da;border-radius:20px;overflow:hidden;background:#fff;box-shadow:0 26px 72px rgba(24,33,45,.19),0 4px 15px rgba(24,33,45,.08);opacity:0;transform:translateY(12px) scale(.985);transform-origin:bottom ' + side + ';transition:opacity .2s ease,transform .2s ease;visibility:hidden}' +
    '.zory-agent-panel.is-open{opacity:1;transform:none;visibility:visible}' +
    '.zory-agent-panel.is-full{top:0;left:0;right:0;bottom:0;width:100%;height:100%;max-width:none;border:0;border-radius:0}' +
    '.zory-agent-panel iframe{display:block;width:100%;height:100%;border:0;background:#fff}' +
    '@media (prefers-reduced-motion:reduce){.zory-agent-launcher,.zory-agent-teaser,.zory-agent-panel{transition:none}}'

  var style = document.createElement('style')
  style.setAttribute('data-zory-agent', '')
  style.textContent = css

  // ── elements ───────────────────────────────────────────────────────────────

  var launcher = document.createElement('button')
  launcher.type = 'button'
  launcher.className = 'zory-agent-launcher'
  launcher.setAttribute('aria-haspopup', 'dialog')
  launcher.setAttribute('aria-expanded', 'false')
  launcher.setAttribute('aria-label', 'Ask ' + assistantName + ', your shopping assistant')

  var face = document.createElement('span')
  face.className = 'zory-agent-face'
  if (avatar) {
    var img = document.createElement('img')
    img.alt = ''
    img.src = avatar
    img.onerror = function () {
      face.removeChild(img)
      face.textContent = assistantName.charAt(0).toUpperCase()
    }
    face.appendChild(img)
  } else {
    face.textContent = assistantName.charAt(0).toUpperCase()
  }
  var dot = document.createElement('span')
  dot.className = 'zory-agent-dot'
  face.appendChild(dot)
  launcher.appendChild(face)
  launcher.appendChild(document.createTextNode('Ask ' + assistantName))

  var teaser = null
  if (greeting) {
    teaser = document.createElement('button')
    teaser.type = 'button'
    teaser.className = 'zory-agent-teaser'
    teaser.textContent = greeting.slice(0, 120)
    teaser.setAttribute('aria-hidden', 'true')
    teaser.tabIndex = -1
  }

  var panel = document.createElement('div')
  panel.className = 'zory-agent-panel'
  panel.id = 'zory-agent-panel'
  panel.setAttribute('role', 'dialog')
  panel.setAttribute('aria-label', assistantName + ' shopping assistant')
  launcher.setAttribute('aria-controls', panel.id)

  var iframe = null
  var ready = false
  var open = false
  var expanded = false
  var pending = []
  var handlers = {}
  var previousOverflow = ''

  function isMobile() {
    return window.innerWidth < MOBILE
  }

  function widgetUrl() {
    var params = [
      ['store', storeId],
      ['name', assistantName],
      ['storeName', storeName],
      ['accent', accent],
      ['host', window.location.origin],
    ]
    if (avatar) params.push(['avatar', avatar])
    if (apiOrigin) params.push(['api', apiOrigin])
    return (
      origin +
      '/widget/?' +
      params
        .map(function (p) {
          return encodeURIComponent(p[0]) + '=' + encodeURIComponent(p[1])
        })
        .join('&')
    )
  }

  function ensureFrame() {
    if (iframe) return
    iframe = document.createElement('iframe')
    iframe.title = assistantName + ', shopping assistant'
    iframe.setAttribute('allow', 'clipboard-write')
    iframe.src = widgetUrl()
    panel.appendChild(iframe)
  }

  function post(type, data) {
    if (!iframe || !iframe.contentWindow) return
    iframe.contentWindow.postMessage({ source: SOURCE, v: 1, type: type, data: data || {} }, origin)
  }

  function command(type, data) {
    if (ready) post(type, data)
    else pending.push([type, data])
  }

  function syncState() {
    var full = open && (expanded || isMobile())
    panel.classList.toggle('is-full', full)
    if (full) {
      if (document.documentElement.style.overflow !== 'hidden') {
        previousOverflow = document.documentElement.style.overflow
        document.documentElement.style.overflow = 'hidden'
      }
    } else if (document.documentElement.style.overflow === 'hidden') {
      document.documentElement.style.overflow = previousOverflow
    }
    post('state', { open: open, expanded: expanded, mobile: isMobile() })
  }

  function emit(name, payload) {
    ;(handlers[name] || []).slice().forEach(function (fn) {
      try {
        fn(payload)
      } catch (err) {
        console.error('[ZoryAgent] handler error', err)
      }
    })
  }

  function hideTeaser(remember) {
    if (!teaser) return
    teaser.classList.remove('is-on')
    if (remember) {
      try {
        sessionStorage.setItem('zory-agent:teaser', 'seen')
      } catch (e) {}
    }
  }

  function setOpen(value) {
    if (value === open) return
    open = value
    if (open) {
      ensureFrame()
      hideTeaser(true)
      dot.style.display = 'none'
    } else {
      expanded = false
    }
    panel.classList.toggle('is-open', open)
    launcher.style.display = open ? 'none' : ''
    launcher.setAttribute('aria-expanded', String(open))
    syncState()
    if (open) {
      setTimeout(function () {
        if (iframe) iframe.focus()
      }, 50)
    } else {
      launcher.focus()
    }
    emit(open ? 'open' : 'close', {})
  }

  launcher.addEventListener('click', function () {
    setOpen(true)
  })
  if (teaser) {
    teaser.addEventListener('click', function () {
      setOpen(true)
    })
  }
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && open) setOpen(false)
  })
  window.addEventListener('resize', function () {
    if (open) syncState()
  })

  // Only our own iframe may drive the panel: exact origin and window.
  window.addEventListener('message', function (event) {
    if (!iframe || event.source !== iframe.contentWindow || event.origin !== origin) return
    var msg = event.data || {}
    if (msg.source !== SOURCE) return
    var data = msg.data || {}
    switch (msg.type) {
      case 'ready':
        ready = true
        syncState()
        pending.splice(0).forEach(function (item) {
          post(item[0], item[1])
        })
        break
      case 'close':
        setOpen(false)
        break
      case 'expand':
        expanded = true
        syncState()
        break
      case 'collapse':
        expanded = false
        syncState()
        break
      case 'badge':
        if (!open && data.count > 0) {
          dot.textContent = data.count > 9 ? '9+' : String(data.count)
          dot.style.display = 'block'
        }
        break
      case 'event':
        if (typeof data.name === 'string') emit(data.name, data.payload)
        break
    }
  })

  // ── page API ───────────────────────────────────────────────────────────────

  var api = {
    __loaded: true,
    open: function (tool) {
      setOpen(true)
      if (typeof tool === 'string' && tool) command('tool', { tool: tool })
    },
    ask: function (message) {
      if (typeof message !== 'string' || !message.trim()) return
      setOpen(true)
      command('ask', { message: message.slice(0, 1000) })
    },
    close: function () {
      setOpen(false)
    },
    toggle: function () {
      setOpen(!open)
    },
    isOpen: function () {
      return open
    },
    on: function (name, fn) {
      if (typeof fn !== 'function') return function () {}
      ;(handlers[name] = handlers[name] || []).push(fn)
      return function () {
        handlers[name] = (handlers[name] || []).filter(function (f) {
          return f !== fn
        })
      }
    },
  }

  var queued = existing && Array.isArray(existing.q) ? existing.q : []
  window.ZoryAgent = api

  function mount() {
    document.head.appendChild(style)
    document.body.appendChild(panel)
    if (teaser) document.body.appendChild(teaser)
    document.body.appendChild(launcher)
    queued.forEach(function (call) {
      var name = call && call[0]
      if (typeof api[name] === 'function') api[name].apply(null, Array.prototype.slice.call(call, 1))
    })
    if (autoOpen) setOpen(true)
    var seen = false
    try {
      seen = sessionStorage.getItem('zory-agent:teaser') === 'seen'
    } catch (e) {}
    if (teaser && !seen) {
      setTimeout(function () {
        if (!open) teaser.classList.add('is-on')
      }, 1200)
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount)
  else mount()
})()
