// SiteOps can serve the same SPA under its hotel URL mask, and clean
// shareable links (/book, /stay, /hk, /checkin/<token>) are answered at
// the site root where they were opened (kamra/hooks.py website_route_rules),
// with no redirect to a mount.
const pathname = typeof window === "undefined" ? "" : window.location.pathname
const hotelAlias = /^\/hotel(?:\/|$)/.test(pathname)
const rootRoute = /^\/(?:(?:book|stay)(?:\/|$)|hk\/?$|checkin\/)/.test(pathname)
export const ROUTER_BASENAME = hotelAlias ? "/hotel" : rootRoute ? "/" : import.meta.env.PROD ? "/kamra" : "/"

// Prefix an in-app path with the basename for a full-page navigation
// (window.location), which the router's own navigate() would otherwise add.
export function toFullPath(path: string): string {
  const base = ROUTER_BASENAME === "/" ? "" : ROUTER_BASENAME
  return base + (path.startsWith("/") ? path : "/" + path)
}
