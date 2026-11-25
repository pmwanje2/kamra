import type { ReactNode } from "react"
import { Link } from "react-router-dom"
import { externalRoute } from "../lib/erpBooks"

/** A link to an in-app route. When the site's ERP or outlet point of sale
 *  does that job instead (lib/erpBooks), it is a plain link to that screen,
 *  so nobody lands on a route this app no longer serves. */
export default function AppLink(props: { to: string; className?: string; children: ReactNode }) {
  const external = externalRoute(props.to)
  return external
    ? <a href={external} className={props.className}>{props.children}</a>
    : <Link to={props.to} className={props.className}>{props.children}</Link>
}
