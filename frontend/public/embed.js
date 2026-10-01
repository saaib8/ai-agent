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
 *   data-greeting        one teaser message, or "off" to hide the teaser
 *   data-greetings       several teaser messages separated by "|"; they rotate
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
  // The teaser cycles through a few short offers, each opening the tool it
  // names. A store can replace them, give one, or turn the teaser off.
  var DEFAULT_GREETINGS = [
    { text: 'Looking for the perfect sofa?', tool: 'catalog' },
    { text: 'Need help furnishing your home?', tool: 'room-planner' },
    { text: "Tell me your budget \u2014 I'll narrow the options.", tool: 'budget' },
    { text: "Snap a photo \u2014 I'll find similar pieces.", tool: 'photo' },
  ]
  var greetingOption = option('greeting', 'greeting')
  var greetingsOption = option('greetings', 'greetings')
  var greetings =
    greetingOption === 'off'
      ? []
      : greetingsOption
        ? greetingsOption.split('|').map(function (s) { return { text: s.trim().slice(0, 120), tool: '' } }).filter(function (g) { return g.text })
        : greetingOption
          ? [{ text: greetingOption.slice(0, 120), tool: '' }]
          : DEFAULT_GREETINGS
  var autoOpen = option('open', 'open') === 'true'
  var apiOrigin = originOf(option('api', 'api'))

  // ── styles (all under one prefix so nothing touches the store's page) ──────

  var css =
    '.zory-agent-launcher{position:fixed;bottom:24px;' + side + ':24px;z-index:' + Z + ';display:flex;align-items:center;gap:12px;height:56px;padding:0 18px 0 7px;border:0;border-radius:28px;background:#17191c;color:#fff;font:600 17px/1 "DM Sans",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;letter-spacing:-.01em;cursor:pointer;box-shadow:0 12px 32px rgba(24,33,45,.28),0 2px 6px rgba(24,33,45,.12);transition:transform .15s ease,box-shadow .15s ease;-webkit-font-smoothing:antialiased}' +
    '.zory-agent-launcher:hover{transform:translateY(-1px);box-shadow:0 16px 38px rgba(24,33,45,.32),0 2px 6px rgba(24,33,45,.12)}' +
    '.zory-agent-launcher:focus-visible,.zory-agent-teaser button:focus-visible{outline:3px solid ' + accent + ';outline-offset:3px}' +
    '.zory-agent-chev{width:16px;height:16px;margin-left:2px;color:#9aa4ab}' +
    '.zory-agent-face{position:relative;display:grid;place-items:center;width:42px;height:42px;flex-shrink:0;border-radius:50%;background:linear-gradient(145deg,#fff,' + accent + ');color:#17191c;font-weight:700;font-size:16px;line-height:1;box-shadow:0 0 0 2.5px #fff}' +
    '.zory-agent-face img{width:100%;height:100%;border-radius:50%;object-fit:cover}' +
    '.zory-agent-dot{position:absolute;top:-4px;right:-4px;min-width:18px;height:18px;padding:0 5px;border-radius:999px;background:#e5484d;color:#fff;font:700 11px/18px system-ui,sans-serif;text-align:center;box-shadow:0 0 0 2px #17191c;display:none}' +
    '.zory-agent-teaser{position:fixed;bottom:96px;' + side + ':24px;z-index:' + Z + ';width:270px;max-width:calc(100vw - 48px);padding:12px 12px 14px 16px;border:1px solid #dce0e5;border-radius:20px;background:#fff;color:#17191c;font:400 13px/1.4 "DM Sans",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;box-shadow:0 12px 32px rgba(24,33,45,.12),0 2px 6px rgba(24,33,45,.05);opacity:0;transform:translateY(8px);transition:opacity .25s ease,transform .25s ease;pointer-events:none;-webkit-font-smoothing:antialiased;cursor:pointer}' +
    '.zory-agent-teaser.is-on{opacity:1;transform:none;pointer-events:auto}' +
    '.zory-agent-teaser .zt-top{display:flex;align-items:center;gap:6px;color:#59616c;font-size:12px}' +
    '.zory-agent-teaser .zt-spark{width:18px;height:18px;margin:-6px 0 0 -2px;color:' + accent + '}' +
    '.zory-agent-teaser .zt-ctrl{margin-left:auto;display:flex;align-items:center;gap:2px}' +
    '.zory-agent-teaser .zt-ctrl button{display:inline-flex;align-items:center;gap:3px;height:24px;min-width:24px;padding:0 5px;border:0;border-radius:7px;background:none;color:#59616c;font:500 12px/1 "DM Sans",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;cursor:pointer}' +
    '.zory-agent-teaser .zt-ctrl button:hover{background:#eceef1;color:#17191c}' +
    '.zory-agent-teaser .zt-ctrl svg{width:13px;height:13px}' +
    '.zory-agent-teaser .zt-msg{margin-top:4px;font-size:14px;font-weight:600;line-height:1.35;letter-spacing:-.01em;transition:opacity .2s ease}' +
    '.zory-agent-panel{position:fixed;bottom:24px;' + side + ':24px;z-index:' + Z + ';width:376px;height:min(676px,calc(100vh - 48px));max-width:calc(100vw - 32px);border:1px solid #ccd2da;border-radius:20px;overflow:hidden;background:#fff;box-shadow:0 26px 72px rgba(24,33,45,.19),0 4px 15px rgba(24,33,45,.08);opacity:0;transform:translateY(12px) scale(.985);transform-origin:bottom ' + side + ';transition:opacity .2s ease,transform .2s ease,width .25s ease,height .25s ease;visibility:hidden}' +
    '.zory-agent-panel.is-open{opacity:1;transform:none;visibility:visible}' +
    '.zory-agent-panel.is-large{width:min(1560px,calc(100vw - 48px));height:calc(100vh - 48px);max-width:none}' +
    '.zory-agent-panel.is-full{top:0;left:0;right:0;bottom:0;width:100%;height:100%;max-width:none;border:0;border-radius:0}' +
    '.zory-agent-panel iframe{display:block;width:100%;height:100%;border:0;background:#fff}' +
    '@media (prefers-reduced-motion:reduce){.zory-agent-launcher,.zory-agent-teaser,.zory-agent-teaser .zt-msg,.zory-agent-panel{transition:none}}'

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

  var SVG = 'http://www.w3.org/2000/svg'
  function icon(path, className, width) {
    var svg = document.createElementNS(SVG, 'svg')
    svg.setAttribute('viewBox', '0 0 24 24')
    svg.setAttribute('fill', 'none')
    svg.setAttribute('stroke', 'currentColor')
    svg.setAttribute('stroke-width', width || '2')
    svg.setAttribute('stroke-linecap', 'round')
    svg.setAttribute('stroke-linejoin', 'round')
    svg.setAttribute('aria-hidden', 'true')
    if (className) svg.setAttribute('class', className)
    var p = document.createElementNS(SVG, 'path')
    p.setAttribute('d', path)
    svg.appendChild(p)
    return svg
  }
  var CHEVRON = 'M9 6l6 6-6 6'
  function makeFace(className) {
    var el = document.createElement('span')
    el.className = 'zory-agent-face' + (className ? ' ' + className : '')
    if (avatar) {
      var img = document.createElement('img')
      img.alt = ''
      img.src = avatar
      img.onerror = function () {
        if (img.parentNode) img.parentNode.removeChild(img)
        el.insertBefore(document.createTextNode(assistantName.charAt(0).toUpperCase()), el.firstChild)
      }
      el.appendChild(img)
    } else {
      el.textContent = assistantName.charAt(0).toUpperCase()
    }
    return el
  }

  var face = makeFace('')
  var dot = document.createElement('span')
  dot.className = 'zory-agent-dot'
  face.appendChild(dot)
  launcher.appendChild(face)
  launcher.appendChild(document.createTextNode('Ask ' + assistantName))
  launcher.appendChild(icon(CHEVRON, 'zory-agent-chev', '2'))

  // ── teaser: rotating offers beside the launcher ───────────────────────────
  var teaser = null
  var teaserMsg = null
  var teaserCount = null
  var pauseBtn = null
  var greetingIndex = 0
  var rotation = null
  var paused = false
  var PAUSE = 'M9 5v14M15 5v14'
  var PLAY = 'M8 5l11 7-11 7z'

  if (greetings.length) {
    teaser = document.createElement('div')
    teaser.className = 'zory-agent-teaser'
    teaser.setAttribute('role', 'status')
    teaser.setAttribute('aria-live', 'polite')

    var top = document.createElement('div')
    top.className = 'zt-top'
    top.appendChild(icon('M6 4l1.5 3M3 10l3.3.6M5 16.5l2.7-1.8', 'zt-spark', '2'))
    var label = document.createElement('span')
    label.textContent = assistantName + ' can help'
    top.appendChild(label)

    var ctrl = document.createElement('span')
    ctrl.className = 'zt-ctrl'
    if (greetings.length > 1) {
      var next = document.createElement('button')
      next.type = 'button'
      next.setAttribute('aria-label', 'Next message')
      teaserCount = document.createElement('span')
      next.appendChild(teaserCount)
      next.appendChild(icon(CHEVRON, '', '2.2'))
      next.addEventListener('click', function (event) {
        event.stopPropagation()
        showGreeting(greetingIndex + 1)
      })
      ctrl.appendChild(next)

      pauseBtn = document.createElement('button')
      pauseBtn.type = 'button'
      pauseBtn.addEventListener('click', function (event) {
        event.stopPropagation()
        paused = !paused
        renderPause()
        if (paused) stopRotation()
        else startRotation()
      })
      ctrl.appendChild(pauseBtn)
    }
    var dismiss = document.createElement('button')
    dismiss.type = 'button'
    dismiss.setAttribute('aria-label', 'Dismiss')
    dismiss.appendChild(icon('M18 6L6 18M6 6l12 12', '', '2'))
    dismiss.addEventListener('click', function (event) {
      event.stopPropagation()
      hideTeaser(true)
    })
    ctrl.appendChild(dismiss)
    top.appendChild(ctrl)
    teaser.appendChild(top)

    teaserMsg = document.createElement('div')
    teaserMsg.className = 'zt-msg'
    teaser.appendChild(teaserMsg)
    showGreeting(0)
    renderPause()
  }

  function renderPause() {
    if (!pauseBtn) return
    pauseBtn.textContent = ''
    pauseBtn.appendChild(icon(paused ? PLAY : PAUSE, '', '2.4'))
    pauseBtn.setAttribute('aria-label', paused ? 'Resume messages' : 'Pause messages')
  }

  function showGreeting(index) {
    greetingIndex = (index + greetings.length) % greetings.length
    teaserMsg.style.opacity = '0'
    setTimeout(function () {
      teaserMsg.textContent = greetings[greetingIndex].text
      teaserMsg.style.opacity = '1'
    }, teaserMsg.textContent ? 150 : 0)
    if (teaserCount) teaserCount.textContent = greetingIndex + 1 + '/' + greetings.length
  }

  function teaserDismissed() {
    try {
      return sessionStorage.getItem('zory-agent:teaser') === 'dismissed'
    } catch (e) {
      return false
    }
  }

  function showTeaserSoon() {
    if (!teaser || teaserDismissed()) return
    setTimeout(function () {
      if (open || teaserDismissed()) return
      teaser.classList.add('is-on')
      startRotation()
    }, 1200)
  }

  function startRotation() {
    stopRotation()
    if (greetings.length > 1 && !paused) {
      rotation = setInterval(function () {
        showGreeting(greetingIndex + 1)
      }, 5000)
    }
  }

  function stopRotation() {
    if (rotation) clearInterval(rotation)
    rotation = null
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
    iframe.setAttribute('allow', 'clipboard-write; fullscreen')
    iframe.setAttribute('allowfullscreen', '')
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
    // Expanding makes a larger window in the corner, never the whole page;
    // only a phone, where the small panel would not fit, gets full screen.
    var full = open && isMobile()
    panel.classList.toggle('is-full', full)
    panel.classList.toggle('is-large', open && expanded && !full)
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
    stopRotation()
    teaser.classList.remove('is-on')
    if (remember) {
      try {
        sessionStorage.setItem('zory-agent:teaser', 'dismissed')
      } catch (e) {}
    }
  }

  function setOpen(value) {
    if (value === open) return
    open = value
    if (open) {
      ensureFrame()
      hideTeaser(false)
      dot.style.display = 'none'
    } else {
      expanded = false
      showTeaserSoon()
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
      var tool = greetings[greetingIndex].tool
      setOpen(true)
      if (tool) command('tool', { tool: tool })
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
    if (!autoOpen) showTeaserSoon()
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount)
  else mount()
})()
