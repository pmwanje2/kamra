/** Channel Mapping & Rate Parity Health Control Tower (/channel-health).
 *  100% in-app hotel operations view: connection health, ARI outbox queue,
 *  rate parity comparison (0/2/3dp exact), and synced OTA stay restrictions. */
import { Fragment, useCallback, useEffect, useState } from "react"
import { Link } from "react-router-dom"
import { AlertTriangle, CheckCircle2, RefreshCw, RotateCcw, Send } from "lucide-react"
import { call, getCurrentProperty } from "../lib/api"
import { useAuth } from "../lib/auth"
import { useT, t as translate, useUILanguage } from "../lib/i18n"
import { momentParts } from "../lib/localDates"
import { fmtAmount } from "../lib/money"
import { serverError } from "../lib/resource"
import {
  canEditRates,
  healthTone,
  parityTone,
  type ChannelConnectionHealth,
  type ChannelHealthSnapshot,
  type ChannelParityRow,
} from "../lib/restrictions"
import { Badge } from "../components/ui/badge"
import { Button } from "../components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card"
import { cn } from "../lib/utils"

function formatRate(value: number | null, currency: string): string {
  if (value == null) return "—"
  return `${currency} ${fmtAmount(value, currency)}`
}

function formatDelta(value: number | null, currency: string): string {
  if (value == null) return "—"
  if (value === 0) return fmtAmount(0, currency)
  const sign = value > 0 ? "+" : "-"
  return `${sign}${fmtAmount(Math.abs(value), currency)}`
}

export default function ChannelHealth() {
  const { t } = useT()
  const lang = useUILanguage()
  const { roles } = useAuth()
  const [data, setData] = useState<ChannelHealthSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busyConn, setBusyConn] = useState<string | null>(null)
  const [confirmSync, setConfirmSync] = useState<{ conn: ChannelConnectionHealth; since: string } | null>(null)
  const [parityFilter, setParityFilter] = useState<"all" | "issues">("all")
  const [outboxFilter, setOutboxFilter] = useState<"all" | "Pending" | "Failed" | "Sent">("all")
  const [expandedMapping, setExpandedMapping] = useState<string | null>(null)

  const moment = (value: string | null) => {
    if (!value) return t("Never")
    const parts = momentParts(value, lang)
    return parts ? t("{date} at {time}", parts) : value
  }

  const load = useCallback(async () => {
    setLoading(true)
    setErr(null)
    try {
      const snap = await call<ChannelHealthSnapshot>("kamra.restrictions.channel_health_snapshot", {
        property: getCurrentProperty(),
        days: 14,
      })
      setData(snap)
    } catch (e) {
      setErr(serverError(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const canManage = Boolean(data?.can_manage && canEditRates(roles))

  async function retryFailed(connection: string) {
    setBusyConn(connection)
    setErr(null)
    setNotice(null)
    try {
      const res = await call<{ ok: boolean; retried: number }>("kamra.restrictions.retry_outbox_failures", {
        connection,
      })
      setNotice(t("Re-queued {n} failed outbox item(s).", { n: res.retried }))
      await load()
    } catch (e) {
      setErr(serverError(e))
    } finally {
      setBusyConn(null)
    }
  }

  async function pushFullAri(conn: ChannelConnectionHealth, confirm = 0) {
    setBusyConn(conn.name)
    setErr(null)
    setNotice(null)
    try {
      if (conn.uses_outbox) {
        const out = await call<{ queued?: boolean; needs_confirm?: boolean; last_full_sync?: string }>(
          "kamra.channel_ari.request_full_sync",
          { connection: conn.name, confirm },
        )
        if (out.needs_confirm) {
          setConfirmSync({ conn, since: out.last_full_sync || conn.last_full_sync || "" })
          return
        }
        setConfirmSync(null)
        setNotice(t("Full ARI sync queued for {provider}.", { provider: conn.provider }))
      } else {
        await call("kamra.channel_manager.push_ari", { connection: conn.name })
        setConfirmSync(null)
        setNotice(t("Full ARI push completed for {provider}.", { provider: conn.provider }))
      }
      await load()
    } catch (e) {
      setErr(serverError(e))
    } finally {
      setBusyConn(null)
    }
  }

  const filteredParity = (data?.parity ?? []).filter((row) =>
    parityFilter === "all" ? true : row.parity_status !== "OK",
  )

  const filteredOutbox = (data?.outbox ?? []).filter((row) =>
    outboxFilter === "all" ? true : row.status === outboxFilter,
  )

  const renderRestrictionsChips = (row: ChannelParityRow) => {
    const r = row.restrictions_synced
    const chips: { label: string; tone: "rose" | "amber" | "sky" | "zinc" }[] = []
    if (r.stop_sell) chips.push({ label: "SS", tone: "rose" })
    if (r.min_stay_arrival && r.min_stay_arrival > 1) chips.push({ label: `${r.min_stay_arrival}+`, tone: "sky" })
    if (r.max_stay && r.max_stay > 0) chips.push({ label: `≤${r.max_stay}`, tone: "sky" })
    if (r.closed_to_arrival) chips.push({ label: "CTA", tone: "amber" })
    if (r.closed_to_departure) chips.push({ label: "CTD", tone: "amber" })
    if (!chips.length) {
      return <span className="text-xs text-zinc-400">{t("Open")}</span>
    }
    return (
      <div className="flex flex-wrap gap-1">
        {chips.map((c) => (
          <Badge key={c.label} tone={c.tone}>{c.label}</Badge>
        ))}
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-zinc-900">{t("Channel mapping & parity health")}</h1>
          <p className="text-sm text-zinc-500">
            {t("Monitor channel connections, ARI outbox sync status, stay restrictions, and direct vs OTA rate parity.")}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Link
            to="/rate-calendar"
            className="inline-flex min-h-11 items-center rounded-lg border border-zinc-300 bg-white px-3 text-sm font-medium text-zinc-700 hover:bg-zinc-50"
          >
            {t("Rate Calendar")}
          </Link>
          <Link
            to="/ota-mappings"
            className="inline-flex min-h-11 items-center rounded-lg border border-zinc-300 bg-white px-3 text-sm font-medium text-zinc-700 hover:bg-zinc-50"
          >
            {t("OTA Room Mappings")}
          </Link>
          <Button variant="outline" className="min-h-11" disabled={loading} onClick={() => { void load() }}>
            <RefreshCw className={cn("me-1.5 size-4", loading && "animate-spin")} aria-hidden />
            {t("Refresh")}
          </Button>
        </div>
      </div>

      {err && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {translate(err)}
        </div>
      )}
      {notice && (
        <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
          {notice}
        </div>
      )}

      {data && (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Card>
              <CardContent className="p-4">
                <div className="text-xs font-medium uppercase tracking-wider text-zinc-500">{t("Active connections")}</div>
                <div className="mt-1 text-2xl font-semibold tabular-nums text-zinc-900">
                  {data.summary.connections_active} / {data.summary.connections_total}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <div className="text-xs font-medium uppercase tracking-wider text-zinc-500">{t("Outbox queue")}</div>
                <div className="mt-1 flex items-baseline gap-2 text-2xl font-semibold tabular-nums">
                  <span className="text-zinc-900">{data.summary.outbox_pending}</span>
                  {data.summary.outbox_failed > 0 && (
                    <span className="text-sm font-medium text-rose-600">
                      {t("{n} failed", { n: data.summary.outbox_failed })}
                    </span>
                  )}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <div className="text-xs font-medium uppercase tracking-wider text-zinc-500">{t("Mapped rate plans")}</div>
                <div className="mt-1 text-2xl font-semibold tabular-nums text-zinc-900">
                  {data.summary.mappings_total}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <div className="text-xs font-medium uppercase tracking-wider text-zinc-500">{t("Rate parity")}</div>
                <div className="mt-1 flex items-center gap-2 text-2xl font-semibold tabular-nums">
                  {data.summary.parity_issues === 0 ? (
                    <span className="inline-flex items-center gap-1.5 text-emerald-700">
                      <CheckCircle2 className="size-5" aria-hidden />
                      {t("OK")}
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1.5 text-rose-700">
                      <AlertTriangle className="size-5" aria-hidden />
                      {t("{n} issue(s)", { n: data.summary.parity_issues })}
                    </span>
                  )}
                </div>
              </CardContent>
            </Card>
          </div>

          {data.summary.unmapped_room_types.length > 0 && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
              <div className="font-medium">{t("Unmapped sellable room types")}</div>
              <p className="mt-0.5 text-xs text-amber-800">
                {data.summary.unmapped_room_types.map((rt) => `${rt.room_type_name} (${rt.room_count})`).join(", ")}
              </p>
            </div>
          )}

          {/* Connection Health Cards */}
          <div className="grid gap-3 md:grid-cols-2">
            {data.connections.length === 0 ? (
              <Card className="md:col-span-2">
                <CardContent className="p-6 text-center text-sm text-zinc-500">
                  {t("No channel connections configured for this property yet.")}{" "}
                  <Link to="/channel-manager" className="font-medium text-brand-700 underline">
                    {t("Set up Channel Manager")}
                  </Link>
                </CardContent>
              </Card>
            ) : (
              data.connections.map((conn) => (
                <Card key={conn.name}>
                  <CardHeader className="flex flex-row items-start justify-between gap-2 pb-2">
                    <div>
                      <CardTitle className="text-base">{conn.provider}</CardTitle>
                      <p className="text-xs text-zinc-500">
                        {conn.name} · {conn.external_property_id || t("No external ID")}
                      </p>
                    </div>
                    <Badge tone={healthTone(conn.health_status)}>{t(conn.health_status)}</Badge>
                  </CardHeader>
                  <CardContent className="space-y-3 text-sm">
                    <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                      <dt className="text-zinc-500">{t("Mappings")}</dt>
                      <dd className="text-end tabular-nums text-zinc-900">{conn.mappings_count}</dd>
                      <dt className="text-zinc-500">{t("Outbox pending / failed")}</dt>
                      <dd className="text-end tabular-nums">
                        <span className="text-zinc-900">{conn.outbox_counts.pending + conn.outbox_counts.sending}</span>
                        {" / "}
                        <span className={conn.outbox_counts.failed ? "font-semibold text-rose-600" : "text-zinc-500"}>
                          {conn.outbox_counts.failed}
                        </span>
                      </dd>
                      <dt className="text-zinc-500">{t("Rate batch / limit")}</dt>
                      <dd className="text-end tabular-nums text-zinc-900">
                        {conn.ari_batch_seconds}s · {conn.ari_calls_per_minute}/min
                      </dd>
                      <dt className="text-zinc-500">{t("Last push")}</dt>
                      <dd className="text-end text-zinc-900">{moment(conn.last_push)}</dd>
                    </dl>
                    {conn.last_push_status && (
                      <p className="truncate rounded bg-zinc-50 px-2 py-1 text-xs text-zinc-600" title={conn.last_push_status}>
                        {conn.last_push_status}
                      </p>
                    )}
                    {canManage && (
                      <div className="space-y-2 pt-1">
                        <div className="flex flex-wrap gap-2">
                          <Button
                            type="button"
                            variant="outline"
                            className="min-h-11"
                            disabled={busyConn === conn.name || !conn.active}
                            onClick={() => { void pushFullAri(conn, 0) }}
                          >
                            <Send className="me-1.5 size-4" aria-hidden />
                            {t("Push full ARI")}
                          </Button>
                          {conn.outbox_counts.failed > 0 && (
                            <Button
                              type="button"
                              variant="outline"
                              className="min-h-11 border-rose-300 text-rose-700 hover:bg-rose-50"
                              disabled={busyConn === conn.name}
                              onClick={() => { void retryFailed(conn.name) }}
                            >
                              <RotateCcw className="me-1.5 size-4" aria-hidden />
                              {t("Retry failed outbox")}
                            </Button>
                          )}
                        </div>
                        {confirmSync?.conn.name === conn.name && (
                          <div
                            role="alertdialog"
                            className="space-y-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900"
                          >
                            <p>
                              {t(
                                "The last full sync went out on {date} at {time}. The channel manager admits one a day: send another anyway?",
                                momentParts(confirmSync.since, lang) ?? { date: confirmSync.since, time: "" },
                              )}
                            </p>
                            <div className="flex flex-wrap gap-2">
                              <Button
                                type="button"
                                className="min-h-11"
                                disabled={busyConn === conn.name}
                                onClick={() => { void pushFullAri(conn, 1) }}
                              >
                                {t("Send it")}
                              </Button>
                              <Button
                                type="button"
                                variant="ghost"
                                className="min-h-11"
                                disabled={busyConn === conn.name}
                                onClick={() => setConfirmSync(null)}
                              >
                                {t("Cancel")}
                              </Button>
                            </div>
                          </div>
                        )}
                      </div>
                    )}
                  </CardContent>
                </Card>
              ))
            )}
          </div>

          {/* Rate Parity & Restrictions Sync Monitor */}
          <Card>
            <CardHeader className="flex flex-wrap items-center justify-between gap-2">
              <CardTitle className="text-base">{t("Rate parity & restrictions monitor")}</CardTitle>
              <div className="flex items-center gap-1 rounded-lg border border-zinc-200 bg-zinc-50 p-0.5 text-xs">
                <button
                  type="button"
                  onClick={() => setParityFilter("all")}
                  className={cn(
                    "min-h-9 rounded-md px-3 font-medium",
                    parityFilter === "all" ? "bg-white text-zinc-900 shadow-xs" : "text-zinc-600",
                  )}
                >
                  {t("All")} ({data.parity.length})
                </button>
                <button
                  type="button"
                  onClick={() => setParityFilter("issues")}
                  className={cn(
                    "min-h-9 rounded-md px-3 font-medium",
                    parityFilter === "issues" ? "bg-white text-zinc-900 shadow-xs" : "text-zinc-600",
                  )}
                >
                  {t("Issues")} ({data.summary.parity_issues})
                </button>
              </div>
            </CardHeader>
            <CardContent className="p-0">
              {filteredParity.length === 0 ? (
                <p className="p-6 text-center text-sm text-zinc-500">
                  {t("No channel mappings match this filter.")}
                </p>
              ) : (
                <>
                  {/* Desktop & Tablet Table */}
                  <div className="hidden overflow-x-auto md:block">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="border-b border-zinc-200 text-xs text-zinc-500">
                          <th className="px-4 py-2.5 text-start font-medium">{t("Room type & plan")}</th>
                          <th className="px-3 py-2.5 text-start font-medium">{t("External code")}</th>
                          <th className="px-3 py-2.5 text-end font-medium">{t("Direct rate")}</th>
                          <th className="px-3 py-2.5 text-end font-medium">{t("Last OTA rate")}</th>
                          <th className="px-3 py-2.5 text-center font-medium">{t("Parity")}</th>
                          <th className="px-3 py-2.5 text-start font-medium">{t("Restrictions synced")}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {filteredParity.map((row) => {
                          const open = expandedMapping === row.mapping_name
                          return (
                            <Fragment key={row.mapping_name}>
                              <tr className="border-b border-zinc-100 align-top">
                                <td className="px-4 py-3">
                                  <button
                                    type="button"
                                    aria-expanded={open}
                                    onClick={() => setExpandedMapping(open ? null : row.mapping_name)}
                                    className="text-start font-medium text-zinc-900 hover:text-brand-700"
                                  >
                                    {row.room_type_name}
                                  </button>
                                  <div className="text-xs text-zinc-500">
                                    {row.rate_plan_label} · {row.provider}
                                  </div>
                                  {row.last_error && (
                                    <div className="mt-1 text-xs text-rose-600">{row.last_error}</div>
                                  )}
                                </td>
                                <td className="px-3 py-3 text-xs tabular-nums text-zinc-600">
                                  <div>{row.external_rate_id || row.external_room_id}</div>
                                  <div className="text-zinc-400">{row.currency}</div>
                                </td>
                                <td className="px-3 py-3 text-end tabular-nums text-zinc-900">
                                  {formatRate(row.direct_rate, row.currency)}
                                </td>
                                <td className="px-3 py-3 text-end tabular-nums text-zinc-900">
                                  {formatRate(row.ota_rate, row.currency)}
                                </td>
                                <td className="px-3 py-3 text-center">
                                  <Badge tone={parityTone(row.parity_status)}>{t(row.parity_status)}</Badge>
                                  {row.parity_delta !== null && row.parity_delta !== 0 && (
                                    <div className="mt-1 text-xs tabular-nums text-zinc-500">
                                      {formatDelta(row.parity_delta, row.currency)}
                                    </div>
                                  )}
                                </td>
                                <td className="px-3 py-3">{renderRestrictionsChips(row)}</td>
                              </tr>
                              {open && row.days.length > 0 && (
                                <tr className="border-b border-zinc-200 bg-zinc-50/70">
                                  <td colSpan={6} className="px-4 py-3">
                                    <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7">
                                      {row.days.map((d) => (
                                        <div key={d.date} className="rounded-md border border-zinc-200 bg-white p-2 text-xs">
                                          <div className="flex items-center justify-between gap-1">
                                            <span className="font-medium tabular-nums text-zinc-700">{d.date.slice(5)}</span>
                                            <Badge tone={parityTone(d.status)}>{t(d.status)}</Badge>
                                          </div>
                                          <div className="mt-1 space-y-0.5 tabular-nums text-zinc-600">
                                            <div>{t("Direct rate")}: <span className="font-medium text-zinc-900">{formatRate(d.direct_rate, row.currency)}</span></div>
                                            <div>{t("Last OTA rate")}: <span className="font-medium text-zinc-900">{formatRate(d.ota_rate, row.currency)}</span></div>
                                          </div>
                                          {(d.stop_sell || d.cta || d.ctd || d.min_los > 0 || d.max_los > 0) && (
                                            <div className="mt-1 flex flex-wrap gap-1">
                                              {d.stop_sell && <Badge tone="rose">SS</Badge>}
                                              {d.cta && <Badge tone="amber">CTA</Badge>}
                                              {d.ctd && <Badge tone="amber">CTD</Badge>}
                                              {d.min_los > 0 && <Badge tone="sky">{d.min_los}+</Badge>}
                                              {d.max_los > 0 && <Badge tone="sky">≤{d.max_los}</Badge>}
                                            </div>
                                          )}
                                        </div>
                                      ))}
                                    </div>
                                  </td>
                                </tr>
                              )}
                            </Fragment>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>

                  {/* Mobile Card List (zero horizontal overflow at 390px) */}
                  <div className="divide-y divide-zinc-100 md:hidden">
                    {filteredParity.map((row) => {
                      const open = expandedMapping === row.mapping_name
                      return (
                        <div key={row.mapping_name} className="space-y-2 p-4 text-sm">
                          <div className="flex items-start justify-between gap-2">
                            <div className="min-w-0">
                              <div className="font-medium text-zinc-900">{row.room_type_name}</div>
                              <div className="break-words text-xs text-zinc-500">
                                {row.rate_plan_label} · {row.provider} · {row.external_rate_id || row.external_room_id}
                              </div>
                            </div>
                            <Badge tone={parityTone(row.parity_status)}>{t(row.parity_status)}</Badge>
                          </div>
                          <div className="grid grid-cols-2 gap-2 rounded-lg bg-zinc-50 p-2.5 text-xs">
                            <div className="min-w-0">
                              <div className="text-zinc-500">{t("Direct rate")}</div>
                              <div className="font-medium tabular-nums text-zinc-900">
                                {formatRate(row.direct_rate, row.currency)}
                              </div>
                            </div>
                            <div className="min-w-0 text-end">
                              <div className="text-zinc-500">{t("Last OTA rate")}</div>
                              <div className="font-medium tabular-nums text-zinc-900">
                                {formatRate(row.ota_rate, row.currency)}
                              </div>
                            </div>
                          </div>
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            {renderRestrictionsChips(row)}
                            {row.parity_delta !== null && row.parity_delta !== 0 && (
                              <span className="text-xs font-medium tabular-nums text-zinc-600">
                                {formatDelta(row.parity_delta, row.currency)}
                              </span>
                            )}
                          </div>
                          {row.last_error && <p className="break-words text-xs text-rose-600">{row.last_error}</p>}
                          {row.days.length > 0 && (
                            <div className="pt-1">
                              <button
                                type="button"
                                aria-expanded={open}
                                data-mobile-parity-toggle={row.mapping_name}
                                onClick={() => setExpandedMapping(open ? null : row.mapping_name)}
                                className="inline-flex min-h-11 items-center rounded-lg border border-zinc-200 bg-white px-3 text-xs font-medium text-brand-700 hover:bg-zinc-50"
                              >
                                {open ? t("Hide daily breakdown") : t("Daily breakdown")}
                              </button>
                              {open && (
                                <div className="mt-2 grid grid-cols-2 gap-2">
                                  {row.days.map((d) => (
                                    <div key={d.date} className="min-w-0 rounded-md border border-zinc-200 bg-zinc-50/70 p-2 text-xs">
                                      <div className="flex items-center justify-between gap-1">
                                        <span className="font-medium tabular-nums text-zinc-700">{d.date.slice(5)}</span>
                                        <Badge tone={parityTone(d.status)}>{t(d.status)}</Badge>
                                      </div>
                                      <div className="mt-1.5 space-y-1 tabular-nums text-zinc-600">
                                        <div>
                                          <div className="text-[11px] text-zinc-500">{t("Direct rate")}</div>
                                          <div className="font-medium text-zinc-900">{formatRate(d.direct_rate, row.currency)}</div>
                                        </div>
                                        <div>
                                          <div className="text-[11px] text-zinc-500">{t("Last OTA rate")}</div>
                                          <div className="font-medium text-zinc-900">{formatRate(d.ota_rate, row.currency)}</div>
                                        </div>
                                      </div>
                                      {(d.stop_sell || d.cta || d.ctd || d.min_los > 0 || d.max_los > 0) && (
                                        <div className="mt-1 flex flex-wrap gap-1">
                                          {d.stop_sell && <Badge tone="rose">SS</Badge>}
                                          {d.cta && <Badge tone="amber">CTA</Badge>}
                                          {d.ctd && <Badge tone="amber">CTD</Badge>}
                                          {d.min_los > 0 && <Badge tone="sky">{d.min_los}+</Badge>}
                                          {d.max_los > 0 && <Badge tone="sky">≤{d.max_los}</Badge>}
                                        </div>
                                      )}
                                    </div>
                                  ))}
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      )
                    })}
                  </div>
                </>
              )}
            </CardContent>
          </Card>

          {/* Outbox Live Queue */}
          <Card>
            <CardHeader className="flex flex-wrap items-center justify-between gap-2">
              <CardTitle className="text-base">{t("ARI outbox queue")}</CardTitle>
              <div className="flex flex-wrap items-center gap-1 rounded-lg border border-zinc-200 bg-zinc-50 p-0.5 text-xs">
                {(["all", "Pending", "Failed", "Sent"] as const).map((st) => (
                  <button
                    key={st}
                    type="button"
                    onClick={() => setOutboxFilter(st)}
                    className={cn(
                      "min-h-9 rounded-md px-2.5 font-medium",
                      outboxFilter === st ? "bg-white text-zinc-900 shadow-xs" : "text-zinc-600",
                    )}
                  >
                    {st === "all" ? t("All") : t(st)}
                  </button>
                ))}
              </div>
            </CardHeader>
            <CardContent className="p-0">
              {filteredOutbox.length === 0 ? (
                <p className="p-6 text-center text-sm text-zinc-500">
                  {t("No outbox items in this view.")}
                </p>
              ) : (
                <>
                  <div className="hidden overflow-x-auto md:block">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="border-b border-zinc-200 text-xs text-zinc-500">
                          <th className="px-4 py-2.5 text-start font-medium">{t("Connection")}</th>
                          <th className="px-3 py-2.5 text-start font-medium">{t("Dates")}</th>
                          <th className="px-3 py-2.5 text-start font-medium">{t("Kind")}</th>
                          <th className="px-3 py-2.5 text-center font-medium">{t("Status")}</th>
                          <th className="px-3 py-2.5 text-end font-medium">{t("Attempts")}</th>
                          <th className="px-3 py-2.5 text-start font-medium">{t("Diagnostic")}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {filteredOutbox.map((item) => (
                          <tr key={item.name} className="border-b border-zinc-100">
                            <td className="px-4 py-2.5 text-xs">
                              <div className="font-medium text-zinc-900">{item.provider}</div>
                              <div className="text-zinc-500">{item.connection}</div>
                            </td>
                            <td className="px-3 py-2.5 text-xs tabular-nums text-zinc-700">
                              {item.date_from} → {item.date_to}
                            </td>
                            <td className="px-3 py-2.5 text-xs text-zinc-700">{item.kinds}</td>
                            <td className="px-3 py-2.5 text-center">
                              <Badge
                                tone={
                                  item.status === "Sent" ? "green"
                                    : item.status === "Failed" ? "rose"
                                    : "amber"
                                }
                              >
                                {t(item.status)}
                              </Badge>
                            </td>
                            <td className="px-3 py-2.5 text-end text-xs tabular-nums text-zinc-700">
                              {item.attempts}
                            </td>
                            <td className="max-w-xs px-3 py-2.5 text-xs text-zinc-600">
                              <div className="truncate font-medium" title={item.reason}>{item.reason}</div>
                              {item.detail && (
                                <div className="mt-0.5 break-words text-rose-600">{item.detail}</div>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>

                  <div className="divide-y divide-zinc-100 md:hidden">
                    {filteredOutbox.map((item) => (
                      <div key={item.name} className="space-y-1.5 p-4 text-xs">
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-medium text-zinc-900">
                            {item.provider} · {item.kinds}
                          </span>
                          <Badge
                            tone={
                              item.status === "Sent" ? "green"
                                : item.status === "Failed" ? "rose"
                                : "amber"
                            }
                          >
                            {t(item.status)}
                          </Badge>
                        </div>
                        <div className="tabular-nums text-zinc-500">
                          {item.date_from} → {item.date_to} · {t("Attempts")}: {item.attempts}
                        </div>
                        {item.reason && <div className="text-zinc-700">{item.reason}</div>}
                        {item.detail && <div className="break-words text-rose-600">{item.detail}</div>}
                      </div>
                    ))}
                  </div>
                </>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}
