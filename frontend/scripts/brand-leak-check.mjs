import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import ts from "typescript"
import { decodeHTMLStrict } from "entities"

// A letter or digit, not \b, bounds a name: "_" and "-" join words in
// identifiers, so kamra_mcp_disabled or kamra-theme in prose still leaks.
export const forbidden = /(?<![a-z0-9])(?:Kamra|Frappe|ERPNext|URY|Tridz|BuildSuite)(?![a-z0-9])|kamra-mark|kamrapms\.com|github\.com\/Kamra|HeyKoala|IndianRupee|BadgeIndianRupee|CurrencyRupee|\u20b9/i

const forbiddenIcons = new Set(["IndianRupee", "BadgeIndianRupee", "CurrencyRupee"])

const root = fileURLToPath(new URL("../", import.meta.url))

// Machine identifiers only. Whole-value matches keep prose containing an
// identifier (an error, link caption, etc.) in scope.
const exceptions = new Map(Object.entries({
  "src/lib/brand.ts": ["Kamra Agent", "kamra-mark.svg", "https://kamrapms.com",
    "https://github.com/Kamra-PMS/kamra-pms", "Frappe Desk", "ERPNext", "HeyKoala"],
  "src/lib/api.ts": ["kamra-session", "Kamra Demo Palace", "kamra.banquet."],
  "src/lib/dir.ts": ["kamra-guest-lang", "kamra-lang"],
  // The one place the SPA's mount is named; links go through toFullPath().
  "src/lib/routing.ts": ["/kamra"],
  "src/lib/theme.ts": ["kamra-theme"],
  "src/lib/thermal.ts": ["iframe[data-kamra-print]", "data-kamra-print"],
  "src/components/InfoTip.tsx": ["why-kamra"],
  "src/components/NotificationBell.tsx": ["Kamra Notification"],
  "src/components/SetupNag.tsx": ["kamra.setupnag."],
  "src/components/ui/data-table.tsx": ["kamra.table."],
  "src/screens/AccountingExport.tsx": ["erpnext"],
  "src/screens/Developers.tsx": ["claude mcp add --transport http kamra"],
  "src/screens/settings/cards.tsx": ["claude mcp add --transport http kamra", "kamra.connect.client."],
  "src/screens/settings/WebhooksPage.tsx": ["kamra.webhooks."],
  "src/screens/settings/AccessPages.tsx": ["kamra-job-titles"],
  "src/screens/Inventory.tsx": ["kamra.inventory."],
  "src/screens/Kitchen.tsx": ["kamra.pos."],
  "src/screens/PublicBooking.tsx": ["kamra-jsonld"],
  "src/screens/Tickets.tsx": ["kamra.tickets."],
}))

function identifier(value, node, filename) {
  if (!node || ts.isJsxText(node)) return false
  if (exceptions.get(filename)?.includes(value)) return true
  if (/^(?:kamra|frappe|erpnext)(?:[.:_]\w[\w:-]*)+$/.test(value)) return true
  // Asset and API paths only. A screen path such as "/kamra/hk" is a link a
  // person follows and sees, so it is built with toFullPath() instead.
  if (/^\/(?:assets\/kamra\/|api\/method\/(?:kamra|frappe)\.)[\w./?=%&#:-]*$/.test(value)) return true
  if (/^x-(?:frappe-csrf-token|kamra-(?:event(?:-id)?|timestamp|signature|property))$/i.test(value)) return true
  if (filename === "src/screens/Login.tsx" && /^(?:admin|gm|frontdesk|revenue|finance|hk)@kamra\.local$/.test(value)) return true
  if (ts.isPropertyAssignment(node.parent) && node.parent.name.getText() === "doctype" &&
      value === "Kamra Notification") return true
  return false
}

export function csvRows(text) {
  const rows = []
  let row = [], field = "", quoted = false
  for (let i = 0; i < text.length; i++) {
    const c = text[i]
    if (c === '"') {
      if (quoted && text[i + 1] === '"') { field += '"'; i++ }
      else quoted = !quoted
    } else if (!quoted && (c === "," || c === "\n")) {
      row.push(field); field = ""
      if (c === "\n") { rows.push(row); row = [] }
    } else if (quoted || c !== "\r") field += c
  }
  if (row.length || field) rows.push([...row, field])
  if (quoted) throw new Error("Unterminated quoted CSV value")
  return rows
}

// Sound prefilter (no lookbehind) before expensive TypeScript AST parsing:
// skips files that cannot contain any forbidden token or hex/unicode escape.
const rawCandidate = /kamra|frappe|erpnext|ury|tridz|buildsuite|heykoala|indianrupee|badgeindianrupee|currencyrupee|\u20b9|\\[ux]/i

export function scanTree(base = root) {
  let sourceCount = 0
  const findings = []
  const scan = (value, location, node, filename) => {
    if (forbidden.test(value.trim()) && !identifier(value.trim(), node, filename)) findings.push(`${location}: ${value.trim()}`)
  }
  function walk(dir) {
    for (const item of fs.readdirSync(dir, { withFileTypes: true })) {
      const name = path.join(dir, item.name)
      if (item.isDirectory()) walk(name)
      else if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name)) {
        const text = fs.readFileSync(name, "utf8")
        if (!rawCandidate.test(text)) continue
        const source = ts.createSourceFile(name, text, ts.ScriptTarget.Latest, true)
        const filename = path.relative(base, name)
        function visit(node) {
          if (ts.isStringLiteralLike(node) || ts.isTemplateLiteralToken(node) || ts.isJsxText(node)) {
            const before = findings.length
            scan(node.text, `${filename}:${source.getLineAndCharacterOfPosition(node.getStart()).line + 1}`, node, filename)
            sourceCount += findings.length - before
          } else if (ts.isImportSpecifier(node)) {
            const imported = (node.propertyName ?? node.name).text
            if (forbiddenIcons.has(imported)) {
              findings.push(`${filename}:${source.getLineAndCharacterOfPosition(node.getStart()).line + 1}: ${imported}`)
              sourceCount++
            }
          } else if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
            const tag = node.tagName
            if (ts.isIdentifier(tag) && forbiddenIcons.has(tag.text)) {
              findings.push(`${filename}:${source.getLineAndCharacterOfPosition(tag.getStart()).line + 1}: <${tag.text} />`)
              sourceCount++
            }
          }
          ts.forEachChild(node, visit)
        }
        visit(source)
      }
    }
  }
  walk(path.join(base, "src"))
  const locales = path.join(base, "src/i18n/locales")
  for (const file of fs.readdirSync(locales).filter((f) => f.endsWith(".json"))) {
    for (const [key, value] of Object.entries(JSON.parse(fs.readFileSync(path.join(locales, file), "utf8")))) {
      scan(key, file); scan(value, `${file} (${key})`)
    }
  }
  const [header, ...rows] = csvRows(fs.readFileSync(path.join(base, "src/i18n/catalog.csv"), "utf8"))
  const valuesEnd = header?.indexOf("sources")
  if (valuesEnd < 6 || !rows.length) throw new Error("Unexpected catalog.csv schema")
  for (const [i, row] of rows.entries()) {
    if (row.length !== header.length) throw new Error(`catalog.csv:${i + 2}: unexpected column count`)
    for (let col = 0; col < valuesEnd; col++) scan(row[col], `catalog.csv:${i + 2}`)
  }
  const html = fs.readFileSync(path.join(base, "index.html"), "utf8")
  for (const match of html.matchAll(/<title>([^<]*)<\/title>|\bcontent="([^"]*)"/g)) {
    scan(match[1] ?? match[2], "index.html")
  }
  return { findings, sourceCount }
}

// Keep source line numbers when removing container prefixes. A fence belongs
// to its list item or quote; its closing delimiter cannot escape that block.
const quotePrefix = /^ {0,3}>[ \t]?/
const blankLine = /^[ \t]*$/
const listPrefix = /^( {0,3})([*+-]|\d{1,9}[.)])(?= |$)( *)/
const fenceOpening = /^ {0,3}(`{3,}(?=[^`]*$)|~{3,})/
const atxHeading = /^ {0,3}#{1,6}(?:[ \t]|$)/
const thematicBreak = /^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$/
const htmlBlockTag = /^ {0,3}<\/?(?:address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|nav|noframes|ol|optgroup|option|p|param|pre|search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul)(?=\s|\/?>|$)/i

const htmlTag = /^(?:<\/[a-z][a-z\d-]*\s*>|<[a-z][a-z\d-]*(?:\s+[a-z_:][a-z\d_.:-]*(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*\s*\/?>)/i

function htmlBlockEnd(line, canInterrupt = true) {
  if (/^ {0,3}<(?:script|pre|style|textarea)(?=[\s>]|$)/i.test(line)) return /<\/(?:script|pre|style|textarea)>/i
  if (/^ {0,3}<!--/.test(line)) return /-->/
  if (/^ {0,3}<\?/.test(line)) return /\?>/
  if (/^ {0,3}<!\[CDATA\[/.test(line)) return /\]\]>/
  if (/^ {0,3}<![A-Z]/.test(line)) return />/
  if (htmlBlockTag.test(line)) return /^[ \t]*$/
  if (!canInterrupt && !line.startsWith("    ")) {
    const tagLine = line.replace(/^ {0,3}|[ \t]+$/g, "")
    if (htmlTag.exec(tagLine)?.[0] === tagLine) return /^[ \t]*$/
  }
}

// Tabs advance to the next four-column stop when measuring indentation.
function expandIndent(line) {
  return line.replace(/^[ \t]+/, indent => {
    let columns = 0
    for (const ch of indent) columns += ch === "\t" ? 4 - columns % 4 : 1
    return " ".repeat(columns)
  })
}

function* markdownBlocks(rows, quoteDepth = 0) {
  let paragraph = []
  for (let i = 0; i < rows.length;) {
    if (rows[i].lazy) {
      paragraph.push(rows[i++])
      continue
    }
    const { text } = rows[i]
    const listLine = text.replace(/^( {0,3}(?:[*+-]|\d{1,9}[.)]))([ \t]+)/, (_, prefix, padding) => {
      let width = prefix.length
      for (const ch of padding) width += ch === "\t" ? 4 - width % 4 : 1
      return prefix.padEnd(width)
    })
    const marker = listLine.match(listPrefix)
    // An indented line continues an open paragraph. An empty list item or
    // an ordered list starting after 1 cannot interrupt a paragraph.
    const list = marker && (!paragraph.length || (listLine.slice(marker[0].length) &&
      (marker[2].length === 1 || Number.parseInt(marker[2], 10) === 1)))
    const endHTML = htmlBlockEnd(text, paragraph.length > 0)
    const fence = text.match(fenceOpening)?.[1]
    if (paragraph.length && /^ {0,3}(?:=+|-+)[ \t]*$/.test(text)) {
      yield { type: "setext", rows: paragraph }
      paragraph = []
      i++
      continue
    }
    if (paragraph.length && (blankLine.test(text) || quotePrefix.test(text) || list ||
        fence || endHTML || atxHeading.test(text) || thematicBreak.test(text))) {
      yield { type: "paragraph", rows: paragraph, quoteDepth }
      paragraph = []
    }
    if (blankLine.test(text) || thematicBreak.test(text)) { i++; continue }
    if (fence) {
      const start = i++
      while (i < rows.length) {
        const close = rows[i++].text.match(/^ {0,3}(`{3,}|~{3,})[ \t]*$/)
        if (close?.[1].startsWith(fence)) break
      }
      yield { type: "fence", rows: rows.slice(start, i) }
    } else if (!paragraph.length && text.startsWith("    ")) {
      const start = i++
      while (i < rows.length && (blankLine.test(rows[i].text) || rows[i].text.startsWith("    "))) i++
      yield { type: "code_block", rows: rows.slice(start, i) }
    } else if (list || quotePrefix.test(text)) {
      const first = rows[i++], padding = marker?.[3].length ?? 0
      const indent = list ? marker[1].length + marker[2].length + (padding > 0 && padding <= 4 ? padding : 1) : 0
      const strip = list
        ? line => blankLine.test(line) || line.startsWith(" ".repeat(indent)) ? line.slice(indent) : undefined
        : line => /^ *>/.test(line) ? expandIndent(line.replace(/^ *>[ \t]?/, "")) : undefined
      const children = [{ ...first, text: list ? listLine.slice(indent) : strip(text) }]
      // Two blank lines at the start cannot belong to an empty list item.
      if (list && blankLine.test(children[0].text) && i < rows.length && blankLine.test(rows[i].text)) continue
      const childQuoteDepth = quoteDepth + (list ? 0 : 1)
      let parsed
      while (i < rows.length) {
        let child
        while (i < rows.length && !rows[i].lazy && (child = strip(rows[i].text)) !== undefined) {
          children.push({ ...rows[i++], text: child })
        }
        parsed = [...markdownBlocks(children, childQuoteDepth)]
        const next = lazyContinuation(rows, i, children, parsed.at(-1), list, childQuoteDepth)
        if (next === i) break
        i = next
        parsed = undefined
      }
      yield* parsed ?? markdownBlocks(children, childQuoteDepth)
    } else if (endHTML) {
      const start = i++
      while (!endHTML.test(rows[i - 1].text) && i < rows.length) i++
      yield { type: "html_block", rows: rows.slice(start, i) }
    } else if (atxHeading.test(text)) {
      yield { type: "inline", rows: [rows[i++]] }
    } else {
      paragraph.push(rows[i++])
    }
  }
  if (paragraph.length) yield { type: "paragraph", rows: paragraph, quoteDepth }
}

function lazyContinuation(rows, i, children, leaf, list, quoteDepth) {
  if (blankLine.test(children.at(-1).text) || leaf?.type !== "paragraph" ||
      leaf.rows.at(-1).index !== children.at(-1).index) return i
  while (i < rows.length) {
    const row = rows[i]
    const nestedQuote = leaf.quoteDepth > quoteDepth
    const text = nestedQuote || (list && !row.lazy) ? row.text.replace(/^[ \t]+/, "") : row.text
    const listInterrupts = list && !row.lazy && row.text.startsWith("    ") ? false : listPrefix.test(text)
    if (blankLine.test(text) || quotePrefix.test(text) || atxHeading.test(text) || thematicBreak.test(text) ||
        fenceOpening.test(text) || listInterrupts || htmlBlockEnd(text)) break
    // A lazy continuation is part of the open paragraph, even if indented.
    children.push({ ...row, lazy: true })
    i++
  }
  return i
}

// Parse the destination boundaries separately from the optional link title.
function linkDestination(source, start) {
  let i = start
  if (source[i] === "<") {
    while (++i < source.length && source[i] !== ">") {
      if (source[i] === "\n" || source[i] === "<") return
      if (source[i] === "\\" && source[i + 1]) i++
    }
    if (source[i] !== ">") return
    i++
  } else {
    let depth = 0
    while (i < source.length && !/[\x00-\x20]/.test(source[i])) {
      if (source[i] === ")" && !depth) break
      if (source[i] === "(") depth++
      if (source[i] === ")") depth--
      i += source[i] === "\\" && /[!-/:-@\[-`{-~]/.test(source.charAt(i + 1)) ? 2 : 1
    }
    if (depth) return
  }
  return i
}

function skipWhitespace(source, i) {
  while (i < source.length && /[ \t\n]/.test(source[i])) i++
  return i
}

function linkTitle(source, start) {
  if (!['"', "'", "("].includes(source[start])) return
  const delimiter = source[start] === "(" ? ")" : source[start]
  for (let i = start + 1; i < source.length; i++) {
    if (source[i] === delimiter) return { start: start + 1, end: i }
    if (delimiter === ")" && source[i] === "(") return
    if (source[i] === "\\") i++
  }
}

function inlineDestination(source, start) {
  const urlStart = skipWhitespace(source, start + 1)
  const afterURL = linkDestination(source, urlStart)
  if (afterURL === undefined) return
  const url = decodeHTMLStrict(source.slice(urlStart, afterURL).replace(/^<|>$/g, ""))
  if (/^(?:file|javascript|vbscript|data):/i.test(url) && !/^data:image\/(?:gif|png|jpeg|webp);/i.test(url)) return
  let i = skipWhitespace(source, afterURL)
  const title = i > afterURL ? linkTitle(source, i) : undefined
  if (title) i = skipWhitespace(source, title.end + 1)
  if (source[i] === ")") return { end: i + 1, destination: { start: urlStart, end: afterURL } }
}

function labelAt(source, start) {
  if (source[start] !== "[") return
  for (let i = start + 1; i < source.length && i - start <= 1000; i++) {
    if (source[i] === "]") return { end: i + 1, key: labelKey(source.slice(start + 1, i)) }
    if (source[i] === "[") return
    if (source[i] === "\\") i++
  }
}

function labelKey(text) {
  return text.trim().replace(/\s+/g, " ").toLowerCase().toUpperCase()
}

// Keep every definition for inspection, including unused and duplicate entries.
function takeDefinitions(block, references, blocks) {
  let rows = block.rows
  while (rows.length) {
    const source = rows.map(row => row.text).join("\n")
    const label = labelAt(source, source.match(/^ {0,3}/)[0].length)
    if (!label?.key || source[label.end] !== ":") break
    const start = skipWhitespace(source, label.end + 1)
    const end = linkDestination(source, start)
    if (end === undefined || end <= start) break
    const startTitle = skipWhitespace(source, end)
    let title = startTitle > end ? linkTitle(source, startTitle) : undefined
    let rest = title ? title.end + 1 : end
    if (!/^[ \t]*(?:\n|$)/.test(source.slice(rest))) {
      if (!title || !source.slice(end, startTitle).includes("\n")) break
      title = undefined
      rest = end
    }
    references.add(label.key)
    const count = source.slice(0, rest).split("\n").length
    blocks.push({ type: "definition", rows: rows.slice(0, count), destination: { start, end } })
    rows = rows.slice(count)
  }
  return { ...block, rows }
}

function* markdownInlines(source, references, markup) {
  const ticks = /`+/y
  const lastPossibleCloser = new Map()
  let searchedToEnd = false
  function codeSpan(start) {
    ticks.lastIndex = start
    const marker = ticks.exec(source)[0]
    const end = start + marker.length
    if (!searchedToEnd || (lastPossibleCloser.get(marker.length) ?? 0) > start) {
      let next = source.indexOf("`", end)
      while (next >= 0) {
        ticks.lastIndex = next
        const closer = ticks.exec(source)[0]
        if (closer.length === marker.length) {
          const raw = source.slice(end, next).replace(/\n/g, " ")
          const padded = raw.startsWith(" ") && raw.endsWith(" ") && /[^ ]/.test(raw)
          return { type: "code_inline", start, end: ticks.lastIndex, content: padded ? raw.slice(1, -1) : raw }
        }
        lastPossibleCloser.set(closer.length, next)
        next = source.indexOf("`", ticks.lastIndex)
      }
      searchedToEnd = true
    }
    return { type: "text", start, end }
  }

  // Link-label lookahead skips whole inline atoms, including code spans. Use
  // the same delimiter cache in lookahead and in the final inline pass, as
  // the docs renderer does for labels that turn out to be literal brackets.
  const labelEnds = new Map()
  function findLabel(start) {
    if (labelEnds.has(start)) return labelEnds.get(start)
    const opens = [start]
    for (let pos = start + 1; pos < source.length;) {
      const atom = inlineAtom(pos)
      if (atom) { pos = atom.end; continue }
      if (source[pos] === "[") opens.push(pos)
      if (source[pos] === "]") {
        labelEnds.set(opens.pop(), pos)
        if (!opens.length) return pos
      }
      pos++
    }
    for (const open of opens) labelEnds.set(open, -1)
    return -1
  }

  function inlineAtom(i) {
    const ch = source[i]
    if (ch === "\\" && /[!-/:-@\[-`{-~]/.test(source.charAt(i + 1))) return { type: "text", start: i + 1, end: i + 2 }
    if (ch === "`") return codeSpan(i)
    if (ch === "<") {
      const rest = source.slice(i)
      const autolink = /^<(?:[a-z][a-z0-9+.-]{1,31}:[^ <>\x00-\x20]*|[^\s<>@]+@[a-z0-9.-]+)>/i.exec(rest)
      const html = htmlTag.exec(rest) ?? /^(?:<!--[\s\S]*?-->|<\?[\s\S]*?\?>|<!\[CDATA\[[\s\S]*?\]\]>|<![A-Z][^>]*>)/.exec(rest)
      if (autolink) return { type: "text", start: i, end: i + autolink[0].length }
      if (html) return { type: "html_inline", start: i, end: i + html[0].length }
    }
  }
  const brackets = []
  for (let i = 0; i < source.length;) {
    const ch = source[i]
    const token = inlineAtom(i)
    if (!token && ch === "[" && findLabel(i) >= 0) {
      brackets.push({ pos: i, active: true, image: source[i - 1] === "!" })
    } else if (!token && ch === "]" && brackets.length) {
      const open = brackets.pop()
      if (!open.active) { i++; continue }
      function finishLink(end) {
        markup.push([open.pos, open.pos + 1, ""], [i, end, ""])
        if (open.image) return
        for (let n = brackets.length - 1; n >= 0 && !brackets[n].image; n--) brackets[n].active = false
      }
      if (source[i + 1] === "(") {
        const link = inlineDestination(source, i + 1)
        if (link) {
          finishLink(link.end)
          yield { type: "destination", ...link.destination }
          i = link.end
          continue
        }
      }
      const label = labelAt(source, i + 1)
      const key = label?.key || labelKey(source.slice(open.pos + 1, i))
      if (references.has(key)) {
        finishLink(label?.end ?? i + 1)
        i = label?.end ?? i + 1
        continue
      }
    }
    if (token) {
      if (token.type === "code_inline") yield token
      if (token.type === "html_inline") {
        markup.push([token.start, token.end, tagSeparator(source.slice(token.start, token.end))])
      }
      i = token.end
    } else i++
  }
}

// Keep all source prose and destinations except exact wire exceptions. Retain
// line breaks when masking a multiline span so findings point to the source.
function checkedText(source, tokens, wireMethods, destinations) {
  const parts = []
  let start = 0
  for (const token of tokens) {
    const raw = source.slice(token.start, token.end)
    const allowed = token.type === "destination"
      ? destinations.has(raw.startsWith("<") && raw.endsWith(">") ? raw.slice(1, -1) : raw)
      : wireMethods.has(token.content) || /^X-Frappe-CSRF-Token$/i.test(token.content)
    if (allowed) {
      parts.push(source.slice(start, token.start), raw.replace(/[^\n]/g, " "))
      start = token.end
    }
  }
  return parts.join("") + source.slice(start)
}

const inlineTags = new RegExp(htmlTag.source.slice(1), "gi")
const htmlMarkup = new RegExp("<!--[\\s\\S]*?-->|" + inlineTags.source, "gi")
const tagSeparator = tag => (/^<br(?=[\s/>])/i.test(tag) || htmlBlockTag.test(tag)) ? " " : ""
const stripFormatting = text => text.replace(/[\\*_~]/g, "")

function leaksProse(line) {
  const text = decodeHTMLStrict(line)
  const plain = stripFormatting(text)
  return forbidden.test(text) || forbidden.test(plain) || forbidden.test(plain.replace(inlineTags, tagSeparator))
}

// Project parsed link wrappers and HTML onto visible text. The source view
// still checks destinations, titles and attributes. Keep source row offsets
// even when a multiline comment or link destination disappears completely.
function scanRendered(text, rows, markup, reported) {
  if (!markup.length) return
  const visible = text.split("")
  for (const [start, end, replacement] of markup) {
    visible.fill("", start, end)
    visible[start] = replacement
  }
  let pos = 0
  let rendered = ""
  const offsets = []
  for (const row of rows) {
    offsets.push(rendered.length)
    const end = pos + row.text.length + 1
    rendered += stripFormatting(decodeHTMLStrict(visible.slice(pos, end).join("")))
    pos = end
  }
  let row = 0
  for (const match of rendered.matchAll(new RegExp(forbidden.source, "gi"))) {
    while (offsets[row + 1] <= match.index) row++
    reported.add(rows[row].index)
  }
}

function scanReference(markdown, wireMethods, findings) {
  const lines = markdown.split(/\r\n?|\n/)
  const rows = lines.map((text, index) => ({ text: expandIndent(text), index }))
  const reported = new Set()
  const references = new Set()
  const destinations = new Set(["/kamra.postman_collection.json"])
  for (const method of wireMethods) {
    destinations.add(`/api/method/${method}`)
    destinations.add(`#${method.replace(/[._]/g, "-").toLowerCase()}`)
  }
  const blocks = []
  for (const block of markdownBlocks(rows)) {
    const rest = ["paragraph", "setext"].includes(block.type) ? takeDefinitions(block, references, blocks) : block
    if (rest.rows === block.rows) blocks.push(block)
    else blocks.push(...markdownBlocks(rest.rows))
  }
  for (const block of blocks) {
    if (block.type === "fence" || block.type === "code_block") continue
    const source = block.rows.map(row => row.text).join("\n")
    const markup = []
    let tokens = []
    if (block.type === "definition") tokens = [{ type: "destination", ...block.destination }]
    else if (block.type === "html_block") {
      for (const match of source.matchAll(htmlMarkup)) {
        markup.push([match.index, match.index + match[0].length, tagSeparator(match[0])])
      }
    } else tokens = markdownInlines(source, references, markup)
    const text = checkedText(source, tokens, wireMethods, destinations)
    for (const [line, content] of text.split("\n").entries()) {
      if (leaksProse(content)) reported.add(block.rows[line].index)
    }
    scanRendered(text, block.rows, markup, reported)
  }
  for (const index of [...reported].sort((a, b) => a - b)) findings.push(`api-reference.md:${index + 1}: ${lines[index]}`)
}

export function scanPublishedApi(reference, collection) {
  const findings = []
  const wireMethods = new Set()
  function scanPostman(node, location) {
    if (!node || typeof node !== "object") return
    const urlPath = node.request?.url?.path
    if (urlPath?.length === 3 && urlPath[0] === "api" && urlPath[1] === "method") {
      wireMethods.add(urlPath[2])
    }
    for (const [key, value] of Object.entries(node)) {
      const field = `${location}.${key}`
      if (key === "name" || key === "description") {
        // Postman accepts either plain text or a description with a content field.
        const prose = typeof value === "string" ? value : value?.content
        if (typeof prose !== "string" || forbidden.test(prose)) findings.push(`${field}: ${prose}`)
      }
      scanPostman(value, field)
    }
  }
  scanPostman(collection, "Postman collection")
  scanReference(reference, wireMethods, findings)
  return findings
}

export function generatedApiFindings(docs) {
  const collection = JSON.parse(fs.readFileSync(path.join(docs, "public/kamra.postman_collection.json"), "utf8"))
  const findings = scanPublishedApi(fs.readFileSync(path.join(docs, "api-reference.md"), "utf8"), collection)
  for (const field of ["name", "description"]) {
    if (!collection.info?.[field]?.trim()) findings.push(`Missing Postman collection ${field}`)
  }
  return findings
}

function scanGeneratedDocs() {
  const docs = path.resolve(root, "../docs-site")
  const findings = generatedApiFindings(docs)
  // Guides include installation commands and wire examples. Inspect their prose
  // too, allowing technical spellings only in code and actual link destinations.
  function checkGuide(file) {
    let fence = ""
    function code(text) {
      return text.replace(/https?:\/\/[^\s)\x60]+|ghcr\.io\/kamra-pms\/kamra|Kamra-PMS\/kamra-pms/g, "")
        .replace(/(?:[\w./@-]+\/|@(?:service\.)?)?kamra(?:[./_*:-][\w./*<>-]+)+|\/kamra\b|\bKAMRA_[A-Z_]+|\bX-Kamra-[\w-]+/g, "")
        .replace(/((?:get-app|install-app|--project-name|--transport http)\s+)kamra\b/g, "$1")
    }
    for (const [i, line] of fs.readFileSync(file, "utf8").split("\n").entries()) {
      const marker = line.trimStart().match(/^(\x60{3,}|~{3,})/)
      if (marker) {
        if (!fence || marker[1].startsWith(fence)) fence = fence ? "" : marker[1]
        continue
      }
      const prose = fence ? code(line) : line.replace(/\]\((?:[^()\s]+)\)/g, "]()")
        .replace(/\b(?:href|id)="[^"]*"/g, "")
        .replace(/\x60([^\x60]+)\x60/g, (_, value) => code(value))
      if (/\b(?:kamrapms\.com|heykoala\.ai)\b/i.test(line) || /kamrapms|(?<![a-z0-9])kamra(?![a-z0-9])|كامرا/i.test(prose))
        findings.push(path.relative(docs, file) + ":" + (i + 1) + ": " + line)
    }
  }
  function visitGuides(dir) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const file = path.join(dir, entry.name)
      if (entry.isDirectory()) {
        if (!["node_modules", "dist", "cache", ".git"].includes(entry.name)) visitGuides(file)
      } else if (entry.name.endsWith(".md") && file !== path.join(docs, "api-reference.md")) {
        checkGuide(file)
      }
    }
  }
  visitGuides(docs)
  // Check the strings that build the published title, metadata, navigation and footer.
  const config = ts.createSourceFile("config.mts", fs.readFileSync(path.join(docs, ".vitepress/config.mts"), "utf8"),
    ts.ScriptTarget.Latest, true)
  const docPaths = new Set(["why-kamra", "self-hosting/frappe-cloud", "self-hosting/erpnext-hr", "/kamra.postman_collection.json"])
  function visitConfig(node) {
    if ((ts.isStringLiteralLike(node) || ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node))
        && !docPaths.has(node.text) && forbidden.test(node.text)) findings.push("VitePress config: " + node.text)
    ts.forEachChild(node, visitConfig)
  }
  visitConfig(config)
  return findings
}

export function checkSource() {
  const { findings, sourceCount } = scanTree()
  findings.push(...scanGeneratedDocs())
  if (findings.length) {
    console.error(findings.join("\n"))
    throw new Error(`${sourceCount} UI source findings; ${findings.length} total branding findings`)
  }
  console.log("Brand leak check passed: source, locales, catalog, shell and public API documentation")
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) checkSource()
