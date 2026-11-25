import { useRef, useState } from "react"
import { Upload } from "lucide-react"
import { uploadFile } from "../lib/api"
import { t as translate, useUILanguage } from "../lib/i18n"

/** An image input that accepts an upload OR a pasted URL, with the
 * recommended size/format spelled out so galleries stay tidy. */
export default function ImageField(props: {
  label?: string
  hint: string
  value: string
  onChange: (url: string) => void
  /** Fired after a successful file upload (not paste). Use to auto-save. */
  onUploaded?: (url: string) => void
  /** Attach the File to this document field (needed for Attach Image). */
  attach?: { doctype: string; docname: string; fieldname: string }
  accept?: string
  placeholder?: string
}) {
  useUILanguage()
  const fileRef = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const pick = async (f: File | undefined) => {
    if (!f) return
    setBusy(true)
    setErr(null)
    try {
      const url = await uploadFile(f, props.attach)
      props.onChange(url)
      props.onUploaded?.(url)
    } catch (e) {
      setErr(
        e instanceof Error && e.message
          ? translate(e.message)
          : translate("Upload failed - try a smaller file (under 2 MB)."),
      )
    } finally {
      setBusy(false)
      if (fileRef.current) fileRef.current.value = ""
    }
  }

  return (
    <div className="block">
      {props.label && (
        <span className="mb-1 block text-sm font-medium text-secondary-ink">
          {translate(props.label)}
        </span>
      )}
      <div className="flex items-center gap-2">
        {props.value ? (
          <img
            src={props.value}
            alt=""
            className="size-9 shrink-0 rounded-md border border-line bg-canvas object-cover"
          />
        ) : null}
        <input
          className="min-w-0 flex-1 rounded-lg border border-control bg-surface px-3 py-1.5 text-sm focus:outline-2 focus:outline-offset-1 focus:outline-focus"
          placeholder={(props.placeholder ? translate(props.placeholder) : translate("Upload, or paste an image URL"))}
          value={props.value}
          onChange={(e) => props.onChange(e.target.value)}
        />
        <button
          type="button"
          className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-control bg-surface px-3 py-1.5 text-sm text-secondary-ink hover:border-accent-control disabled:opacity-50"
          disabled={busy}
          onClick={() => fileRef.current?.click()}
        >
          <Upload className="size-3.5" aria-hidden />
          {busy ? translate("Uploading…") : translate("Upload")}
        </button>
        <input
          ref={fileRef}
          type="file"
          className="hidden"
          accept={props.accept ?? "image/png,image/jpeg,image/webp"}
          onChange={(e) => pick(e.target.files?.[0])}
        />
      </div>
      {props.hint && (
        <p className="mt-1 text-xs text-muted-ink">{translate(props.hint)}</p>
      )}
      {err && <p className="mt-1 text-xs text-danger-text">{translate(err)}</p>}
    </div>
  )
}
