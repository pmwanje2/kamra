import { useState } from "react"
import { call } from "../lib/api"
import { useT } from "../lib/i18n"
import { serverError } from "../lib/resource"
import { Button } from "./ui/button"

import RevocationWarnings from "./RevocationWarnings"

const operations = { issue: "Issue key", duplicate: "Duplicate key", rekey: "Re-key", revoke: "Revoke keys", mobile: "Mobile key" }

const auditLabels: Record<string, string> = {
  ...operations, room_move_revoke: "Revoke after room move", checkout_revoke: "Revoke at checkout",
  reverse_checkin_revoke: "Revoke after reversed check-in",
}

export default function DoorKeyControls({ reservation, auditOnly = false }: { reservation: string; auditOnly?: boolean }) {
  const { t } = useT()
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState("")
  const [history, setHistory] = useState<{ name: string; creation: string; operation: string; status: string; room: string; actor: string; retryable?: boolean }[] | null>(null)
  async function loadHistory() {
    try {
      setHistory(await call("kamra.checkin.door_key_audit", { reservation }))
    } catch (err) { setMessage(serverError(err)) }
  }
  async function run(operation: string) {
    setBusy(true)
    try {
      const result = await call<{ message: string }>("kamra.checkin.key_operation", { reservation, operation })
      setMessage(t(result.message))
      if (history) await loadHistory()
    } catch (err) {
      setMessage(serverError(err))
    } finally {
      setBusy(false)
    }
  }
  return <div className="flex flex-wrap gap-2">
    {!auditOnly && Object.entries(operations).map(([operation, label]) =>
      <Button type="button" key={operation} variant="outline" disabled={busy} onClick={() => run(operation)}>{t(label)}</Button>)}
    <Button type="button" variant="outline" onClick={loadHistory}>{t("Door key audit")}</Button>
    {history && <ul className="w-full text-sm" aria-live="polite">
      {history.length === 0 && <li>{t("No key operations recorded.")}</li>}
      {history.map(row => <li key={row.name}>{row.creation} · {t(auditLabels[row.operation] || "Key operation")} · {row.room} · {t(row.status)} · {row.actor}
        {row.retryable && <RevocationWarnings warnings={[{ reservation, operation_id: row.name, room: row.room }]} />}
      </li>)}
    </ul>}
    {message && <p className="w-full text-sm" role="status">{message}</p>}
  </div>
}
