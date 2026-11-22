import { product, upstreamDocs, whiteLabel } from "../lib/brand"
import { useCallback, useEffect, useState } from "react"
import {
  AlertTriangle,
  CheckCircle2,
  ExternalLink,
  HeartPulse,
  Info,
  RefreshCw,
  XCircle,
} from "lucide-react"
import { call } from "../lib/api"
import { useT } from "../lib/i18n"
import { formatDate } from "../lib/utils"
import { Button } from "../components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card"


type CheckStatus = "passed" | "attention" | "failed" | "info"

type HealthCheck = {
  id: string
  title: string
  status: CheckStatus
  detail: string
  link?: string | null
}

type LegacyHealthPayload = {
  overall: CheckStatus
  summary: Record<CheckStatus, number>
  installed: { kamra: string; frappe: string | null; site: string }
  latest: {
    ok: boolean
    tag: string | null
    name: string | null
    url: string
    published_at: string | null
    error?: string | null
    body_preview?: string
  }
  upgrade: {
    docs: string
    releases: string
    docker_latest: string
    docker_nightly: string
    bench: string
    note: string
  }
  checks: HealthCheck[]
}

type PropertyHealth = {
  property: string
  status: CheckStatus
  business_date: string
  calendar_date: string
  night_audit: {
    last_business_date: string | null
    status: string | null
    completed_at: string | null
    next_due: string | null
  }
}

type OperationalHealthPayload = {
  product: string
  overall: CheckStatus
  checked_at: string
  properties: PropertyHealth[]
  checks: Omit<HealthCheck, "link">[]
}

type HealthPayload = LegacyHealthPayload | OperationalHealthPayload

function isOperational(data: HealthPayload): data is OperationalHealthPayload {
  return "properties" in data && Array.isArray(data.properties)
}

const STATUS_STYLE: Record<
  CheckStatus,
  { label: string; dot: string; text: string; Icon: typeof CheckCircle2 }
> = {
  passed: {
    label: "Passed",
    dot: "bg-emerald-500",
    text: "text-emerald-700",
    Icon: CheckCircle2,
  },
  attention: {
    label: "Needs attention",
    dot: "bg-rose-500",
    text: "text-rose-700",
    Icon: AlertTriangle,
  },
  failed: {
    label: "Failed",
    dot: "bg-rose-600",
    text: "text-rose-800",
    Icon: XCircle,
  },
  info: {
    label: "Info",
    dot: "bg-sky-500",
    text: "text-sky-700",
    Icon: Info,
  },
}

function StatusPill({ status }: { status: CheckStatus }) {
  const { t } = useT()
  const s = STATUS_STYLE[status]
  return (
    <span className={`inline-flex items-center gap-1.5 text-sm font-medium ${s.text}`}>
      <span className={`size-2 rounded-full ${s.dot}`} aria-hidden />
      {t(s.label)}
    </span>
  )
}

function PropertyTime({ value }: { value: string | null }) {
  // Preserve the property's civil clock rather than converting to browser time.
  return value ? <time dateTime={value}>{formatDate(value.slice(0, 10))} {value.slice(11, 16)}</time> : "–"
}

function PropertyCard({ property }: { property: PropertyHealth }) {
  const { t } = useT()
  const audit = property.night_audit
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-3">
        <CardTitle>{property.property}</CardTitle>
        <StatusPill status={property.status} />
      </CardHeader>
      <CardContent className="space-y-3 text-sm text-zinc-600">
        <dl className="grid grid-cols-2 gap-3">
          <div><dt>{t("Business date")}</dt><dd>{formatDate(property.business_date)}</dd></div>
          <div><dt>{t("Calendar date")}</dt><dd>{formatDate(property.calendar_date)}</dd></div>
        </dl>
        <p className="font-medium text-zinc-800">{t("Night audit")}: {audit.status ? t(audit.status) : t("Not run yet")}</p>
        <dl className="grid gap-3 sm:grid-cols-3">
          <div><dt>{t("Last audited business date")}</dt><dd>{formatDate(audit.last_business_date)}</dd></div>
          <div><dt>{t("Completed at")}</dt><dd><PropertyTime value={audit.completed_at} /></dd></div>
          <div><dt>{t("Next scheduled audit")}</dt><dd>{audit.next_due ? <PropertyTime value={audit.next_due} /> : t("Not scheduled")}</dd></div>
        </dl>
      </CardContent>
    </Card>
  )
}

export default function SystemHealth() {
  const { t } = useT()
  const [data, setData] = useState<HealthPayload | null>(null)
  const [error, setError] = useState(false)
  const [busy, setBusy] = useState(false)
  const operational = data && isOperational(data) ? data : null
  const legacy = data && !isOperational(data) && !whiteLabel() ? data : null
  const checks = legacy?.checks ?? operational?.checks

  const load = useCallback(async (refresh = false) => {
    setBusy(true)
    setError(false)
    try {
      const out = await call<HealthPayload>("kamra.health.system_health", {
        refresh: refresh ? 1 : 0,
      })
      if (!out || (!isOperational(out) && (whiteLabel() || !out.installed || !out.latest || !out.upgrade))) {
        setData(null)
        setError(true)
        return
      }
      setData(out)
    } catch {
      // Raw transport or server errors may name the underlying stack.
      setError(true)
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => {
    void load(false)
  }, [load])

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-lg font-semibold text-zinc-900">
            <HeartPulse className="size-5 text-brand-700" aria-hidden />
            {t("System Health")}
          </h1>
          <p className="mt-0.5 text-sm text-zinc-500">
            {legacy
              ? t("Check the status of this {product} site and whether a newer open-source release is available.", { product: product() })
              : t("Check the operational status of your properties.")}
          </p>
        </div>
        {operational && <StatusPill status={operational.overall} />}
        <Button
          variant="outline"
          disabled={busy}
          onClick={() => void load(true)}
        >
          <RefreshCw className={`size-4 ${busy ? "animate-spin" : ""}`} aria-hidden />
          {t("Refresh")}
        </Button>
      </div>

      {error && (
        <p className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          {t("Could not load system health.")}
        </p>
      )}

      {!data && !error && (
        <p className="py-10 text-center text-sm text-zinc-400">{t("Loading…")}</p>
      )}

      {operational?.properties.map((property) => (
        <PropertyCard key={property.property} property={property} />
      ))}

      {legacy && (
          <Card>
            <CardHeader className="flex flex-row items-center justify-between gap-3">
              <CardTitle>{t("Overview")}</CardTitle>
              <StatusPill status={legacy.overall === "info" ? "passed" : legacy.overall} />
            </CardHeader>
            <CardContent className="grid gap-3 sm:grid-cols-3">
              <div className="rounded-lg border border-zinc-100 bg-zinc-50 px-3 py-2">
                <p className="text-[11px] font-medium uppercase tracking-wider text-zinc-400">
                  {t("Installed")}
                </p>
                <p className="mt-0.5 font-mono text-sm font-semibold">
                  {product()} {legacy.installed.kamra}
                </p>
                <p className="text-xs text-zinc-500">
                  {t("Platform")} {legacy.installed.frappe ?? "-"}
                </p>
              </div>
              <div className="rounded-lg border border-zinc-100 bg-zinc-50 px-3 py-2">
                <p className="text-[11px] font-medium uppercase tracking-wider text-zinc-400">
                  {t("Latest on GitHub")}
                </p>
                <p className="mt-0.5 font-mono text-sm font-semibold">
                  {legacy.latest.tag ?? t("Unavailable")}
                </p>
                {legacy.latest.url && (
                  <a
                    href={legacy.latest.url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 text-xs font-medium text-brand-700 hover:underline"
                  >
                    {t("Release notes")}
                    <ExternalLink className="size-3" aria-hidden />
                  </a>
                )}
              </div>
              <div className="rounded-lg border border-zinc-100 bg-zinc-50 px-3 py-2">
                <p className="text-[11px] font-medium uppercase tracking-wider text-zinc-400">
                  {t("Site")}
                </p>
                <p className="mt-0.5 truncate font-mono text-sm font-semibold">
                  {legacy.installed.site}
                </p>
              </div>
            </CardContent>
          </Card>
      )}

      {checks && (
          <Card>
            <CardHeader>
              <CardTitle>{t("Checks")}</CardTitle>
            </CardHeader>
            <CardContent className="divide-y divide-zinc-100">
              {checks.map((c: HealthCheck) => {
                const style = STATUS_STYLE[c.status]
                return (
                  <div
                    key={c.id}
                    className="flex flex-wrap items-start justify-between gap-3 py-3 first:pt-0 last:pb-0"
                  >
                    <div className="min-w-0 flex-1">
                      <p className="flex items-center gap-1.5 text-sm font-medium text-zinc-800">
                        <style.Icon className={`size-4 shrink-0 ${style.text}`} aria-hidden />
                        {t(c.title)}
                        {legacy && c.link && (
                          <a
                            href={c.link}
                            target={c.link.startsWith("http") ? "_blank" : undefined}
                            rel="noreferrer"
                            className="text-zinc-400 hover:text-brand-700"
                            aria-label={t("Open details")}
                          >
                            <ExternalLink className="size-3.5" />
                          </a>
                        )}
                      </p>
                      <p className="mt-0.5 text-sm text-zinc-500">{c.detail}</p>
                    </div>
                    <StatusPill status={c.status} />
                  </div>
                )
              })}
            </CardContent>
          </Card>
      )}

      {legacy && (
          <Card>
            <CardHeader>
              <CardTitle>{t("How to upgrade")}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-sm text-zinc-600">
              <p>{t(legacy.upgrade.note)}</p>
              <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2">
                <p className="text-xs font-medium uppercase tracking-wider text-zinc-400">
                  {t("Docker (self-host)")}
                </p>
                <code className="mt-1 block break-all font-mono text-xs text-zinc-800">
                  docker pull {legacy.upgrade.docker_latest}
                </code>
                <p className="mt-1 text-xs text-zinc-500">
                  {t("Then recreate the stack / run migrate. Nightly channel")}:{" "}
                  <code className="font-mono">{legacy.upgrade.docker_nightly}</code>
                </p>
              </div>
              <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2">
                <p className="text-xs font-medium uppercase tracking-wider text-zinc-400">
                  {t("Bench")}
                </p>
                <code className="mt-1 block whitespace-pre-wrap break-all font-mono text-xs text-zinc-800">
                  {legacy.upgrade.bench}
                </code>
              </div>
              <div className="flex flex-wrap gap-3">
                <a
                  href={legacy.upgrade.releases}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 text-sm font-medium text-brand-700 hover:underline"
                >
                  {t("GitHub releases")}
                  <ExternalLink className="size-3.5" aria-hidden />
                </a>
                <a
                  href={legacy.upgrade.docs}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 text-sm font-medium text-brand-700 hover:underline"
                >
                  {t("Self-hosting docs")}
                  <ExternalLink className="size-3.5" aria-hidden />
                </a>
                <a
                  href={`${upstreamDocs()}/quickstart#going-back-downgrade`}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 text-sm font-medium text-brand-700 hover:underline"
                >
                  {t("Going back (downgrade)")}
                  <ExternalLink className="size-3.5" aria-hidden />
                </a>
              </div>
            </CardContent>
          </Card>
      )}
    </div>
  )
}
