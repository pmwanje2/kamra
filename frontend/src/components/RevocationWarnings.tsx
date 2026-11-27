import { useState } from "react"
import { call } from "../lib/api"
import { useT } from "../lib/i18n"
import { serverError } from "../lib/resource"
import { Button } from "./ui/button"

export interface RevocationWarning {
  reservation: string
  operation_id: string
  room: string
  message?: string
  status?: string
  issuer_target?: string | null
  retryable?: boolean
}

// Frappe serializes the response before running its commit hooks. Resolve
// the committed outcome once per stay, retaining the warning if offline.
export async function outstandingWarnings(warnings: RevocationWarning[] = []) {
  const reservations = new Set(warnings.filter(w => w.status === "Queued").map(w => w.reservation))
  const resolved = new Map<string, { status: string; retryable: boolean }>()
  await Promise.all(Array.from(reservations, async reservation => {
    try {
      const rows = await call<{ name: string; status: string; retryable: boolean }[]>(
        "kamra.checkin.door_key_audit", { reservation })
      for (const row of rows) resolved.set(row.name, row)
    } catch { /* The desk can still retry using the durable operation ID. */ }
  }))
  return warnings.flatMap(w => {
    const row = resolved.get(w.operation_id)
    if (row?.status === "Accepted" || row?.status === "Manual") return []
    return [row ? { ...w, status: row.status, retryable: row.retryable } : w]
  })
}

export default function RevocationWarnings({ warnings }: { warnings: RevocationWarning[] }) {
  return <>{warnings.map(warning => <RetryRevocation key={warning.operation_id} warning={warning} />)}</>
}

function RetryRevocation({ warning }: { warning: RevocationWarning }) {
  const { t } = useT()
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState(false)
  const [error, setError] = useState(warning.message || "")
  async function retry() {
    setBusy(true)
    try {
      const result = await call<{ ok: boolean; manual: boolean; message: string }>("kamra.checkin.retry_revocation", {
        reservation: warning.reservation, operation_id: warning.operation_id,
      })
      setDone(result.ok && !result.manual)
      setError(result.message)
    } catch (err) {
      setError(serverError(err))
    } finally {
      setBusy(false)
    }
  }
  if (done) return <p role="status">{t("Keys revoked for room {room}.", { room: warning.room })}</p>
  return <div className="w-full space-y-2 rounded-md bg-warning-subtle p-3 text-sm text-warning-ink" role="alert">
    <p>{t("Keys may still open room {room}.", { room: warning.room })} {t(error)} {warning.issuer_target && !error && <bdi>{warning.issuer_target.slice(0, 12)}</bdi>}</p>
    {warning.retryable !== false && <Button type="button" variant="outline" disabled={busy} onClick={retry}>{t("Retry revocation")}</Button>}
  </div>
}
