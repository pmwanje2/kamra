import { useEffect, useRef, useState } from "react"
import { HelpCircle, Loader2, Send, X } from "lucide-react"

import { call, getCurrentProperty, postRaw } from "../lib/api"
import { cn } from "../lib/utils"
import { Markdown } from "../lib/markdown"
import { t as translate, useUILanguage } from "../lib/i18n"

/** How-to help assistant - explains how to use the hotel PMS (it never acts on
 *  data; that's the AI assistant). Streams answers, grounded in the app. */

interface Msg {
  role: "user" | "assistant"
  content: string
}

const SUGGESTIONS = [
  "How do I check a guest in?",
  "How do I add photos to a room type?",
  "How do I waitlist a booking?",
  "What is RevPAX?",
  "Where do I manage users?",
]

export default function HelpPanel() {
  useUILanguage()
  const [enabled, setEnabled] = useState(false)
  const [open, setOpen] = useState(false)
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    call<{ enabled: boolean }>("kamra.assistant.assistant_status", {
      property: getCurrentProperty(),
    })
      .then((s) => setEnabled(s.enabled))
      .catch(() => setEnabled(false))
  }, [])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [msgs, busy])

  if (!enabled) return null

  async function send(text: string) {
    const q = text.trim()
    if (!q || busy) return
    setInput("")
    setError(null)
    const history = [...msgs, { role: "user" as const, content: q }]
    const aIdx = history.length
    setMsgs([...history, { role: "assistant", content: "" }])
    setBusy(true)
    const patch = (fn: (m: Msg) => Msg) =>
      setMsgs((ms) => ms.map((m, i) => (i === aIdx ? fn(m) : m)))
    const payload = {
      property: getCurrentProperty(),
      messages: history.map(({ role, content }) => ({ role, content })),
    }
    try {
      const res = await postRaw("/api/method/kamra.assistant.help_ask", payload)
      if (!res.ok || !res.body) throw new Error(`stream ${res.status}`)
      const reader = res.body.getReader()
      const dec = new TextDecoder()
      let buf = ""
      let gotAny = false
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        let sep: number
        while ((sep = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, sep)
          buf = buf.slice(sep + 2)
          let event = ""
          let data = ""
          for (const line of block.split("\n")) {
            if (line.startsWith("event:")) event = line.slice(6).trim()
            else if (line.startsWith("data:")) data = line.slice(5).trim()
          }
          if (!event || !data) continue
          const d = JSON.parse(data)
          if (event === "token") {
            gotAny = true
            patch((m) => ({ ...m, content: m.content + d.text }))
          } else if (event === "error") {
            setError(d.message)
          }
        }
      }
      if (!gotAny) throw new Error("empty")
    } catch {
      setError(
        translate("Couldn't reach the help assistant - check the AI key in Settings."),
      )
      setMsgs((ms) => ms.filter((_, i) => i !== aIdx))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <button
        aria-label={translate("Open help")}
        onClick={() => setOpen((o) => !o)}
        className="fixed bottom-5 end-5 z-40 flex size-12 items-center justify-center rounded-full border border-line bg-surface text-secondary-ink shadow-lg hover:text-accent-ink print:hidden"
      >
        {open ? <X className="size-5" /> : <HelpCircle className="size-5" />}
      </button>

      {open && (
        <div className="fixed bottom-20 end-5 z-40 flex h-[520px] w-[360px] max-w-[calc(100vw-2.5rem)] flex-col overflow-hidden rounded-2xl border border-line bg-surface shadow-2xl print:hidden">
          <div className="flex items-center gap-2 border-b border-line-subtle px-4 py-3">
            <HelpCircle className="size-4 text-accent-text" />
            <span className="text-sm font-semibold">{translate("How-to help")}</span>
            <span className="ms-auto text-[10px] uppercase tracking-wider text-muted-ink">{translate("explains, doesn't act")}</span>
          </div>

          <div className="flex-1 space-y-3 overflow-y-auto px-4 py-3">
            {msgs.length === 0 && (
              <div className="space-y-2">
                <p className="text-sm text-secondary-ink">{translate("Ask how to do anything in {product} - I'll walk you through it.")}</p>
                {SUGGESTIONS.map((s) => (
                  <button
                    key={s}
                    className="block w-full rounded-lg border border-line px-3 py-1.5 text-start text-sm text-secondary-ink hover:border-accent-control"
                    onClick={() => send(translate(s))}
                  >
                    {translate(s)}
                  </button>
                ))}
              </div>
            )}
            {msgs.map((m, i) => (
              <div
                key={i}
                className={cn(
                  "max-w-[88%] rounded-2xl px-3.5 py-2 text-sm",
                  m.role === "user"
                    ? "ms-auto whitespace-pre-wrap bg-accent-fill text-on-solid"
                    : "space-y-1.5 bg-subtle text-ink",
                )}
              >
                {m.role === "assistant" ? <Markdown text={m.content} /> : m.content}
              </div>
            ))}
            {busy && (
              <Loader2 className="size-4 animate-spin text-muted-ink" />
            )}
            {error && (
              <p className="rounded-lg bg-danger-subtle px-3 py-2 text-xs text-danger-text">
                {translate(error)}
              </p>
            )}
            <div ref={endRef} />
          </div>

          <form
            className="flex items-center gap-2 border-t border-line-subtle px-3 py-2.5"
            onSubmit={(e) => {
              e.preventDefault()
              send(input)
            }}
          >
            <input
              className="flex-1 rounded-lg border border-control bg-surface px-3 py-1.5 text-sm focus:outline-2 focus:outline-offset-1 focus:outline-focus"
              placeholder={translate("How do I…?")}
              value={input}
              onChange={(e) => setInput(e.target.value)}
            />
            <button
              type="submit"
              aria-label={translate("Send")}
              disabled={busy || !input.trim()}
              className="rounded-lg bg-accent-fill p-2 text-on-solid disabled:opacity-40"
            >
              <Send className="size-4" />
            </button>
          </form>
        </div>
      )}
    </>
  )
}
