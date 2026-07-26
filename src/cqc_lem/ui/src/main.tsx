import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { captureAttribution } from './utils/attribution'
import { initAnalytics } from './utils/analytics'

// Before the router can rewrite the URL — the landing UTMs are only there on the first paint.
captureAttribution()

// No-op (and no posthog-js chunk fetched) when VITE_POSTHOG_KEY is unset.
initAnalytics()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
