import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { BrowserRouter } from "react-router-dom"
import "./index.css"
import App from "./App"
import { initTheme } from "./lib/theme"
import { initLang } from "./lib/dir"
import { asset } from "./lib/asset"
import { brandTitle, logo, siteBrand, whiteLabel } from "./lib/brand"
import { AuthProvider } from "./lib/auth"
import { CashierAuthProvider } from "./lib/cashierAuth"
import { ROUTER_BASENAME } from "./lib/routing"

initTheme()

// Favicons, base-aware (see index.html note).
function setIcon(rel: string, href: string, type?: string) {
  const link = document.createElement("link")
  link.rel = rel
  link.href = href
  if (type) link.type = type
  document.head.appendChild(link)
}
if (import.meta.env.DEV) document.title = brandTitle()
const favicon = siteBrand()?.favicon
if (favicon) setIcon("icon", favicon)
else if (!whiteLabel()) {
  const mark = logo()
  if (mark) setIcon("icon", mark)
  setIcon("icon", asset("favicon-32.png"), "image/png")
  setIcon("apple-touch-icon", asset("apple-touch-180.png"))
}

function mount() {
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <BrowserRouter basename={ROUTER_BASENAME}>
        <AuthProvider>
          <CashierAuthProvider>
            <App />
          </CashierAuthProvider>
        </AuthProvider>
      </BrowserRouter>
    </StrictMode>,
  )
}

// Wait for the active locale chunk so the first paint is already
// translated; English skips the network round-trip.
void initLang().then(mount)
