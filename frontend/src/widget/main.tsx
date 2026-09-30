import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { readConfig } from './config'
import './widget.css'

const config = readConfig()
const root = createRoot(document.getElementById('root')!)

root.render(
  <StrictMode>
    {config ? (
      <App config={config} />
    ) : (
      <div className="empty" role="alert">
        <h3>This assistant isn&apos;t set up yet</h3>
        <p>The embed snippet needs a store id: add data-store-id to the script tag.</p>
      </div>
    )}
  </StrictMode>,
)
