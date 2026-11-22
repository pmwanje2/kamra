// SiteOps can serve the same SPA under its hotel URL mask.
const hotelAlias = typeof window !== "undefined" && /^\/hotel(?:\/|$)/.test(window.location.pathname)
export const ROUTER_BASENAME = hotelAlias ? "/hotel" : import.meta.env.PROD ? "/kamra" : "/"

// Prefix an in-app path with the basename for a full-page navigation
// (window.location), which the router's own navigate() would otherwise add.
export function toFullPath(path: string): string {
  const base = ROUTER_BASENAME === "/" ? "" : ROUTER_BASENAME
  return base + (path.startsWith("/") ? path : "/" + path)
}
