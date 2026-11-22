export function cn(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ")
}

/** Plain text of a server message that may carry HTML. A single
 *  /<[^>]+>/ pass can leave a tag behind from nested input ("<scr<b>ipt"),
 *  so strip until nothing changes, then drop any stray angle bracket - the
 *  result can never contain markup. It is only ever rendered as text. */
export function htmlToText(s: string): string {
  let out = s
  let prev: string
  do {
    prev = out
    out = out.replace(/<[^<>]*>/g, "")
  } while (out !== prev)
  return out.replace(/[<>]/g, "")
}

/** Format a civil date without converting it through a timezone. */
export function formatDate(iso: unknown): string {
  const parts = /^(\d{4})-(\d\d)-(\d\d)$/.exec(String(iso ?? ""))
  if (!parts) return "–"
  const format = (window as Window & { kamra_date_format?: string }).kamra_date_format || "yyyy-mm-dd"
  return format.toLowerCase().replace(/yyyy|mm|dd/g, token => parts[{yyyy: 1, mm: 2, dd: 3}[token]!])
}

export function roomLabel(row: { room_number?: string | null }): string {
  return row.room_number || "–"
}
