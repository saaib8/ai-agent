# Oakline — widget demo store

A pretend furniture store that installs the ZORY shopping assistant with the
same snippet a real store pastes. It runs on its own origin on purpose: on a
real store the widget is a cross-origin iframe, and that is what this tests.

```bash
# 1. the widget on :3000 — the one local origin the deployed stage API
#    allows in CORS; the snippet's data-api sends every /v1 call straight there
cd ai-agent/frontend
npm run dev -- --port 3000 --strictPort

# 2. the store on :5600
node ai-agent/widget-demo/server.js
```

Open http://localhost:5600 and click **Ask Nora**. The store's buttons use the
page API (`ZoryAgent.open('room-planner')`, `ZoryAgent.ask('…')`).

To load the widget from somewhere else — e.g. once it is deployed — without
editing the snippet:

```bash
WIDGET_ORIGIN=https://stage.ai-agent.zory.ai node ai-agent/widget-demo/server.js
```

Photos on this page are from Unsplash; the products Nora shows come from the
real store catalogue (store 50) through the API.
