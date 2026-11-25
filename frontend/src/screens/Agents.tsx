import { Fragment, useCallback, useEffect, useState } from "react"
import {
  Bot,
  Copy,
  ExternalLink,
  Loader2,
  MessageSquare,
  Plug,
  Sparkles,
} from "lucide-react"
import { call, getCurrentProperty } from "../lib/api"
import { useRealtime } from "../lib/realtime"
import Assistant from "./Assistant"
import { Badge } from "../components/ui/badge"
import { Button } from "../components/ui/button"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "../components/ui/card"
import { serverError } from "../lib/resource"
import { moneyLocale } from "../lib/money"
import { useAuth } from "../lib/auth"
import { useT, t as translate, useUILanguage, uiDateLocale } from "../lib/i18n"


interface SignInRow {
  name: string
  creation: string
  user: string
  full_name: string
  operation: "Login" | "Logout"
  status: string
}

/** Roles that may see staff sign-ins (Frappe's Login / Logout records). */
const SIGN_IN_ROLES = ["Hotel Admin", "Finance", "System Manager", "Administrator"]

/** Staff sign-ins: who signed in or out, when, and whether it worked. */
function SignInsTable({ property }: { property: string }) {
  const { t } = useT()
  const [rows, setRows] = useState<SignInRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [page, setPage] = useState(0)
  const PAGE = 50
  useEffect(() => {
    setRows(null)
    call<SignInRow[]>("kamra.agents_api.staff_sign_ins", { property, limit: PAGE, start: page * PAGE })
      .then((r) => {
        setRows(r)
        setError(null)
      })
      .catch((e) => setError(serverError(e)))
  }, [property, page])
  if (error)
    return (
      <div className="rounded-lg border border-danger-line bg-danger-subtle px-3 py-2 text-sm text-danger-ink">{translate(error)}</div>
    )
  if (!rows) return <LoadingRow />
  return (
    <div className="space-y-3">
      {rows.length === 0 ? (
        <p className="py-8 text-center text-sm text-muted-ink">{t("No sign-ins recorded yet.")}</p>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-line bg-surface">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-start text-xs font-medium uppercase tracking-wider text-muted-ink">
                <th className="px-3 py-2 text-start">{t("When")}</th>
                <th className="px-3 py-2 text-start">{t("Who")}</th>
                <th className="px-3 py-2 text-start">{t("What")}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line-subtle">
              {rows.map((r) => (
                <tr key={r.name}>
                  <td className="whitespace-nowrap px-3 py-2 text-xs text-muted-ink">{fmtWhen(r.creation)}</td>
                  <td className="whitespace-nowrap px-3 py-2">
                    <span className="font-medium">{r.full_name}</span>
                    <span className="ms-1 text-xs text-muted-ink">{r.user}</span>
                  </td>
                  <td className="whitespace-nowrap px-3 py-2">
                    <Badge tone={r.status === "Success" ? (r.operation === "Login" ? "green" : "zinc") : "rose"}>
                      {r.status !== "Success"
                        ? t("Sign-in failed")
                        : r.operation === "Login"
                          ? t("Signed in")
                          : t("Signed out")}
                    </Badge>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="flex items-center justify-between text-sm text-muted-ink">
        <span>{t("Page {n}", { n: page + 1 })}</span>
        <div className="flex gap-2">
          <Button variant="outline" disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))}>
            {t("Prev")}
          </Button>
          <Button variant="outline" disabled={rows.length < PAGE} onClick={() => setPage((p) => p + 1)}>
            {t("Next")}
          </Button>
        </div>
      </div>
    </div>
  )
}



const statusTone: Record<string, "zinc" | "sky" | "amber" | "rose" | "green"> = {
  Executed: "green",
  Suggested: "sky",
  Pending: "amber",
  Approved: "green",
  Rejected: "rose",
  Expired: "zinc",
}

export default function Agents() {
  useUILanguage()
  const property = getCurrentProperty()
  const [panel, setPanel] = useState<"none" | "connect">("none")

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Sparkles className="size-5 text-accent-text" aria-hidden />
            <h1 className="text-xl font-semibold tracking-tight">{translate("{assistant}")}</h1>
          </div>
          <p className="mt-0.5 text-sm text-muted-ink">{translate("Chat here, or connect your AI provider. Either way it acts as you, within your role.")}</p>
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-2">
          {panel !== "none" && (
            <button
              onClick={() => setPanel("none")}
              className="flex items-center gap-1.5 rounded-full bg-accent-fill px-3 py-1.5 text-sm font-medium text-on-solid hover:bg-accent-fill-hover"
            >
              <MessageSquare className="size-4" aria-hidden />{translate("Back to chat")}</button>
          )}
          <button
            onClick={() =>
              setPanel((p) => (p === "connect" ? "none" : "connect"))
            }
            title={translate("Connect your AI provider to this hotel over MCP - scoped to your role")}
            className={
              "flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm " +
              (panel === "connect"
                ? "border-accent-control bg-accent-subtle text-accent-ink"
                : "border-line text-muted-ink hover:bg-canvas hover:text-secondary-ink")
            }
          >
            <Plug className="size-4" aria-hidden />{translate("AI Provider")}</button>
        </div>
      </header>

      {panel === "connect" && <ConnectTab property={property} />}
      {panel === "none" && <Assistant />}
    </div>
  )
}

function LoadingRow() {
  useUILanguage()
  return (
    <p className="py-10 text-center text-sm text-muted-ink">
      <Loader2 className="mr-2 inline size-4 animate-spin" aria-hidden />{translate("Loading…")}</p>
  )
}

interface ActivityRow {
  name: string
  creation: string
  actor: string | null
  agent_name: string | null
  action_type: string
  action_channel: string
  approval_status: string
  approver: string | null
  reference_doctype: string | null
  reference_name: string | null
  rationale: string
  minutes_saved: number
}

interface ActivityDetail extends ActivityRow {
  executed_at: string | null
  property: string
  autonomy: string | null
  before_snapshot: unknown
  after_snapshot: unknown
}

const prettyAction = (t: string) =>
  t.replace(/^copilot_/, "").replace(/_/g, " ")


const fmtWhen = (d: string) =>
  new Date(d.replace(" ", "T")).toLocaleString(uiDateLocale(moneyLocale()), {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  })

export function ActivityTab({ property }: { property: string }) {
  const [rows, setRows] = useState<ActivityRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [kind, setKind] = useState("")
  const { roles } = useAuth()
  const { t } = useT()
  const canSeeSignIns = roles.some((r) => SIGN_IN_ROLES.includes(r))
  const [page, setPage] = useState(0)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [detail, setDetail] = useState<ActivityDetail | null>(null)
  const PAGE = 50

  const toggleRow = (rowName: string) => {
    if (expanded === rowName) {
      setExpanded(null)
      return
    }
    setExpanded(rowName)
    setDetail(null)
    call<ActivityDetail>("kamra.agents_api.activity_detail", { name: rowName })
      .then(setDetail)
      .catch(() => setDetail(null))
  }

  const load = useCallback((silent = false) => {
    if (kind === "signins") return
    if (!silent) setLoading(true)
    call<ActivityRow[]>("kamra.agents_api.activity_feed", {
      property,
      actor_kind: kind || null,
      limit: PAGE,
      start: page * PAGE,
    })
      .then((r) => {
        setRows(r)
        setError(null)
      })
      .catch((e) => setError(serverError(e)))
      .finally(() => { if (!silent) setLoading(false) })
  }, [property, kind, page])

  // live audit stream: refetch silently on any change (no loading flash)
  useRealtime(useCallback(() => load(true), [load]))

  useEffect(() => {
    load()
  }, [load])

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={kind}
          onChange={(e) => {
            setKind(e.target.value)
            setPage(0)
          }}
          className="rounded-lg border border-control bg-surface px-2.5 py-1.5 text-sm"
          aria-label={translate("Filter by who acted")}
        >
          <option value="">{t("Everyone")}</option>
          <option value="human">{t("People only")}</option>
          <option value="agent">{t("AI only")}</option>
          {canSeeSignIns && <option value="signins">{t("Staff sign-ins")}</option>}
        </select>
        <p className="text-xs text-muted-ink">
          {kind === "signins"
            ? t("Who signed in to and out of {product}, newest first.")
            : t("Every action on the property, newest first - who did it and what changed.")}
        </p>
      </div>
      {kind === "signins" ? (
        <SignInsTable property={property} />
      ) : (
      <>

      {error && (
        <div className="rounded-lg border border-danger-line bg-danger-subtle px-3 py-2 text-sm text-danger-ink">
          {translate(error)}
        </div>
      )}
      {loading ? (
        <LoadingRow />
      ) : rows.length === 0 ? (
        <p className="py-8 text-center text-sm text-muted-ink">{translate("Nothing logged yet.")}</p>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-line bg-surface">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs font-medium uppercase tracking-wider text-muted-ink">
                <th className="px-3 py-2">{translate("When")}</th>
                <th className="px-3 py-2">{translate("Who")}</th>
                <th className="px-3 py-2">{translate("What")}</th>
                <th className="px-3 py-2">{translate("On")}</th>
                <th className="px-3 py-2">{translate("Status")}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line-subtle">
              {rows.map((r) => (
                <Fragment key={r.name}>
                <tr
                  className="cursor-pointer hover:bg-canvas"
                  onClick={() => toggleRow(r.name)}
                >
                  <td className="whitespace-nowrap px-3 py-2 text-xs text-muted-ink">
                    {fmtWhen(r.creation)}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2">
                    {r.agent_name ? (
                      <span className="inline-flex items-center gap-1">
                        <Bot className="size-3.5 text-accent-text" aria-hidden />
                        <span className="font-medium">{r.agent_name}</span>
                      </span>
                    ) : (
                      <span className="font-medium">
                        {(r.actor ?? "system").split("@")[0]}
                      </span>
                    )}
                  </td>
                  <td className="max-w-md px-3 py-2">
                    <span className="font-medium capitalize">
                      {prettyAction(r.action_type)}
                    </span>
                    {r.rationale && (
                      <span className="text-muted-ink"> - {r.rationale}</span>
                    )}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2 text-xs text-muted-ink">
                    {r.reference_name ?? "-"}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2">
                    <Badge tone={statusTone[r.approval_status] ?? "zinc"}>
                      {r.approval_status}
                    </Badge>
                    {r.approver && (
                      <span className="ml-1 text-xs text-muted-ink">{translate("by ")}{r.approver.split("@")[0]}
                      </span>
                    )}
                  </td>
                </tr>
                {expanded === r.name && (
                  <tr className="bg-canvas/60">
                    <td colSpan={5} className="px-4 py-3">
                      {!detail ? (
                        <p className="text-xs text-muted-ink">{translate("Loading…")}</p>
                      ) : (
                        <div className="space-y-3 text-sm">
                          <dl className="grid grid-cols-2 gap-x-6 gap-y-1.5 sm:grid-cols-4">
                            {([
                              ["Logged", fmtWhen(detail.creation)],
                              ["Executed", detail.executed_at ? fmtWhen(detail.executed_at) : "-"],
                              ["Actor", detail.actor ?? "system"],
                              ["Agent", detail.agent_name ?? "-"],
                              ["Channel", detail.action_channel ?? "-"],
                              ["Autonomy", detail.autonomy ?? "-"],
                              [
                                "Reference",
                                detail.reference_name
                                  ? `${detail.reference_doctype} · ${detail.reference_name}`
                                  : "-",
                              ],
                              [
                                "Minutes saved",
                                detail.minutes_saved ? String(detail.minutes_saved) : "-",
                              ],
                            ] as [string, string][]).map(([k, v]) => (
                              <div key={k}>
                                <dt className="text-[11px] font-medium uppercase tracking-wider text-muted-ink">
                                  {translate(k)}
                                </dt>
                                <dd className="text-secondary-ink">{v}</dd>
                              </div>
                            ))}
                          </dl>
                          {detail.rationale && (
                            <p className="text-secondary-ink">
                              <span className="text-[11px] font-medium uppercase tracking-wider text-muted-ink">{translate("Why:")}{" "}
                              </span>
                              {detail.rationale}
                            </p>
                          )}
                          {(detail.before_snapshot || detail.after_snapshot) != null && (
                            <div className="grid gap-3 sm:grid-cols-2">
                              {(
                                [
                                  ["Before", detail.before_snapshot],
                                  ["After", detail.after_snapshot],
                                ] as [string, unknown][]
                              ).map(([label, snap]) =>
                                snap == null ? null : (
                                  <div key={label as string}>
                                    <p className="mb-1 text-[11px] font-medium uppercase tracking-wider text-muted-ink">
                                      {translate(label)}
                                    </p>
                                    <pre className="max-h-56 overflow-auto rounded-lg border border-line bg-surface p-2.5 text-xs leading-relaxed text-secondary-ink">
                                      {typeof snap === "string"
                                        ? snap
                                        : JSON.stringify(snap, null, 2)}
                                    </pre>
                                  </div>
                                ),
                              )}
                            </div>
                          )}
                        </div>
                      )}
                    </td>
                  </tr>
                )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="flex items-center justify-between text-sm text-muted-ink">
        <span>{translate("Page ")}{page + 1}</span>
        <div className="flex gap-2">
          <Button
            variant="outline"
            disabled={page === 0}
            onClick={() => setPage((p) => Math.max(0, p - 1))}
          >{translate("Prev")}</Button>
          <Button
            variant="outline"
            disabled={rows.length < PAGE}
            onClick={() => setPage((p) => p + 1)}
          >{translate("Next")}</Button>
        </div>
      </div>
      </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Connect tab - one click into Claude's prefilled connector dialog + OAuth
// ---------------------------------------------------------------------------

interface ConnectInfo {
  disabled?: boolean
  mcp_url: string
  claude_install_url: string
  claude_code: string
  is_public_https: boolean
  property: string
  property_name: string
  user: string
  tool_count: number
  active_grants: number
  last_mcp: { creation: string; action_type: string } | null
}

export function ConnectTab({ property }: { property: string }) {
  useUILanguage()
  const [info, setInfo] = useState<ConnectInfo | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState<"url" | "code" | null>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      setInfo(
        await call<ConnectInfo>("kamra.mcp_oauth.connect_info", { property }),
      )
    } catch (e) {
      setError(serverError(e))
    }
  }, [property])

  useEffect(() => {
    void load()
  }, [load])

  function markCopied(which: "url" | "code", text: string) {
    navigator.clipboard.writeText(text)
    setCopied(which)
    setTimeout(() => setCopied(null), 1500)
  }

  if (info?.disabled) {
    return (
      <div className="max-w-2xl space-y-4">
        <Card>
          <CardHeader>
            <CardTitle>{translate("AI Provider")}</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-secondary-ink">{translate("AI provider connections are turned off on this site. On your own {product}, every staff member can connect an AI provider and act within their role.")}</p>
          </CardContent>
        </Card>
      </div>
    )
  }

  return (
    <div className="max-w-2xl space-y-4">
      <Card>
        <CardHeader>
          <div>
            <CardTitle>{translate("AI Provider")}</CardTitle>
            <p className="mt-0.5 text-xs text-muted-ink">{translate("Your AI provider acts as you at ")}{info?.property_name || translate("this hotel")}{translate(": your role decides what it can see and do. A front-desk connection books and checks in; it cannot touch rates or finance. Every action lands in Activity under your name.")}</p>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {info && !info.is_public_https && (
            <p className="rounded-lg bg-warning-subtle px-3 py-2 text-sm text-warning-ink">{translate("Remote AI providers need a public HTTPS URL to reach this hotel. For a local site, use a local MCP client or the stdio sidecar.")}</p>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Button
              disabled={!info?.claude_install_url}
              onClick={() => {
                if (!info) return
                window.open(info.claude_install_url, "_blank", "noopener")
              }}
            >
              <ExternalLink className="size-4" aria-hidden />{translate("Open Claude")}</Button>
            {info && info.active_grants > 0 && (
              <Button
                variant="outline"
                disabled={busy}
                onClick={async () => {
                  if (
                    !confirm(
                      translate("Disconnect your AI providers? They will have to sign in again."),
                    )
                  )
                    return
                  setBusy(true)
                  try {
                    await call("kamra.mcp_oauth.revoke_my_grants", {
                      property,
                    })
                    await load()
                  } catch (e) {
                    setError(serverError(e))
                  } finally {
                    setBusy(false)
                  }
                }}
              >{translate("Disconnect")}</Button>
            )}
          </div>
          {info?.last_mcp && (
            <p className="text-xs text-muted-ink">{translate("Last MCP action: ")}{prettyAction(info.last_mcp.action_type)} ·{" "}
              {fmtWhen(info.last_mcp.creation)}
            </p>
          )}
          {info && info.active_grants > 0 && !info.last_mcp && (
            <p className="text-xs text-positive-ink">{translate("An AI provider is authorised for this property. Enable its connector in your AI chat, then ask about the hotel.")}</p>
          )}
          <p className="text-sm text-secondary-ink">{translate("Connect ChatGPT, Claude or another MCP-compatible AI provider using this hotel's MCP URL and OAuth. Sign in to {product}, select the property and allow access. The Claude shortcut opens with the MCP URL filled in.")}</p>
          <p className="text-sm text-secondary-ink">{translate("Enable the connector in your AI chat and say things like \"occupancy this week\" or \"book a Deluxe for Friday\".")}</p>
          {info && (
            <>
              <div>
                <div className="mb-1 text-xs font-medium text-muted-ink">{translate("MCP URL · ")}{info.tool_count}{translate(" governed tools")}</div>
                <div className="flex items-stretch gap-2">
                  <code className="flex-1 overflow-x-auto rounded-lg bg-subtle px-3 py-2 font-mono text-xs text-ink">
                    {info.mcp_url}
                  </code>
                  <Button
                    variant="outline"
                    onClick={() => markCopied("url", info.mcp_url)}
                  >
                    <Copy className="size-3.5" aria-hidden />
                    {copied === "url" ? translate("Copied") : translate("Copy")}
                  </Button>
                </div>
              </div>
              <div>
                <div className="mb-1 text-xs font-medium text-muted-ink">
                  Claude Code
                </div>
                <div className="flex items-stretch gap-2">
                  <pre className="flex-1 overflow-x-auto rounded-lg bg-subtle p-3 text-xs leading-relaxed text-secondary-ink">
                    {info.claude_code}
                  </pre>
                  <Button
                    variant="outline"
                    onClick={() => markCopied("code", info.claude_code)}
                  >
                    <Copy className="size-3.5" aria-hidden />
                    {copied === "code" ? translate("Copied") : translate("Copy")}
                  </Button>
                </div>
              </div>
            </>
          )}
          {error && (
            <p className="rounded-lg bg-danger-subtle px-3 py-2 text-sm text-danger-text">
              {translate(error)}
            </p>
          )}
        </CardContent>
      </Card>
      <p className="text-xs text-muted-ink">{translate("Need a platform-wide or service key (voice partner, unattended jobs)? That is issued by your system admin under Developers. Staff should use AI Provider to connect with OAuth.")}</p>
    </div>
  )
}
