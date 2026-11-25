import { useEffect, useState } from "react"

import { login, requestPasswordReset, serverError } from "../lib/api"
import BrandMark from "../components/BrandMark"
import { product, whiteLabel } from "../lib/brand"
import { Button } from "../components/ui/button"
import { getSiteInfo, versionLabel } from "../lib/siteInfo"
import { getLang, setLang, type Lang } from "../lib/dir"
import { useOfferedLanguages, useT, t as translate } from "../lib/i18n"


const inputCls =
  "w-full rounded border border-control bg-surface px-3 py-2 text-sm " +
  "text-ink placeholder:text-muted-ink " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-focus"

const DEMO_ACCOUNTS = [
  { label: "System Admin", usr: "admin@kamra.local", pwd: "KamraAdmin1!" },
  { label: "Hotel Admin (GM)", usr: "gm@kamra.local", pwd: "KamraGM1!" },
  { label: "Front Desk", usr: "frontdesk@kamra.local", pwd: "KamraDesk1!" },
  { label: "Revenue", usr: "revenue@kamra.local", pwd: "KamraRev1!" },
  { label: "Finance", usr: "finance@kamra.local", pwd: "KamraFin1!" },
  { label: "Housekeeping", usr: "hk@kamra.local", pwd: "KamraHK1!" },
]

export default function Login(props: { onSuccess: () => void }) {
  const { t, lang } = useT()
  const langs = useOfferedLanguages()
  const [usr, setUsr] = useState("")
  const [pwd, setPwd] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [mode, setMode] = useState<"sign-in" | "forgot">("sign-in")
  const [resetSent, setResetSent] = useState(false)
  // Demo accounts only exist on the seeded demo site; hide them elsewhere.
  const [demoMode, setDemoMode] = useState(false)
  const [version, setVersion] = useState<string | null>(null)
  const [langState, setLangState] = useState<Lang>(lang || getLang())

  useEffect(() => {
    setLangState(lang)
  }, [lang])

  useEffect(() => {
    getSiteInfo().then((info) => {
      setDemoMode(Boolean(info.demo_mode))
      setVersion(versionLabel(info))
    })
  }, [])

  async function submit(u = usr, p = pwd) {
    setBusy(true)
    setError(null)
    try {
      await login(u, p)
      // Navigation + the production CSRF re-boot are handled by the /login
      // route (LoginPage.onSuccess).
      sessionStorage.removeItem("kamra_session_ended")
      props.onSuccess()
    } catch {
      setError(t("Wrong email, username, or password."))
    } finally {
      setBusy(false)
    }
  }

  async function sendReset() {
    if (!usr.trim()) return
    setBusy(true)
    setError(null)
    try {
      await requestPasswordReset(usr.trim())
      setResetSent(true)
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  const sessionEnded = sessionStorage.getItem("kamra_session_ended") === "1"

  return (
    <div
      className="flex min-h-[100dvh] flex-col items-center justify-center bg-canvas px-4"
      style={{ colorScheme: "light" }}
    >
      <div className="fixed inset-x-0 top-0 h-[5px] bg-accent-deep" aria-hidden />
      <div className="w-full max-w-sm">
        {sessionEnded && mode === "sign-in" && (
          <p className="mb-4 rounded border border-warning-line bg-warning-subtle px-4 py-2.5 text-center text-sm text-warning-ink">
            {t("Your session ended. Sign in to pick up where you left off.")}
          </p>
        )}
        <div className="mb-6 flex flex-col items-center gap-2">
          <BrandMark className="size-16 text-xl" />
          <span
            className="text-2xl font-semibold tracking-[0.02em] text-ink"
            style={{ fontFamily: "Montserrat, ui-sans-serif, system-ui, sans-serif" }}
          >
            {product()}
            {!whiteLabel() && <span className="ml-1.5 align-middle text-[10px] font-semibold tracking-[0.4em] text-accent-ink">
              PMS
            </span>}
          </span>
          <label className="mt-1 flex items-center gap-2 text-xs text-muted-ink">
            <span>{t("Language")}</span>
            <select
              className="rounded border border-control bg-surface px-2 py-1 text-xs text-secondary-ink"
              value={langState}
              onChange={(e) => {
                const next = e.target.value as Lang
                setLang(next)
                setLangState(next)
              }}
              aria-label={t("Language")}
            >
              {langs.map((l) => (
                <option key={l.code} value={l.code}>
                  {l.nativeLabel}
                </option>
              ))}
            </select>
          </label>
        </div>

        {mode === "sign-in" ? (
        <form
          className="space-y-3 rounded border border-control bg-surface p-6"
          onSubmit={(e) => {
            e.preventDefault()
            submit()
          }}
        >
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-secondary-ink">
              {t("Email or username")}
            </span>
            <input
              className={inputCls}
              type="text"
              autoComplete="username"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              value={usr}
              onChange={(e) => setUsr(e.target.value)}
              placeholder={t("Administrator or you@hotel.com")}
            />
          </label>
          <label className="block">
            <span className="mb-1 flex items-center justify-between text-xs font-medium text-secondary-ink">
              <span>{t("Password")}</span>
              <button
                type="button"
                className="font-medium text-accent-ink hover:underline"
                onClick={() => {
                  setMode("forgot")
                  setError(null)
                  setResetSent(false)
                }}
              >
                {t("Forgot password?")}
              </button>
            </span>
            <input
              className={inputCls}
              type="password"
              autoComplete="current-password"
              value={pwd}
              onChange={(e) => setPwd(e.target.value)}
            />
          </label>

          {error && (
            <div className="rounded border border-danger-line bg-danger-subtle px-3 py-2 text-sm text-danger-ink">
              {translate(error)}
            </div>
          )}

          <Button
            className="w-full justify-center rounded bg-primary-button py-2 text-on-solid hover:bg-primary-button-hover focus-visible:outline-focus"
            disabled={busy || !usr || !pwd}
            type="submit"
          >
            {busy ? t("Signing in...") : t("Sign in")}
          </Button>
        </form>
        ) : (
        <form
          className="space-y-3 rounded border border-control bg-surface p-6"
          onSubmit={(e) => {
            e.preventDefault()
            sendReset()
          }}
        >
          <h1 className="text-base font-semibold text-ink">{t("Reset password")}</h1>
          <p className="text-sm text-muted-ink">
            {t("Enter the email or username for your {product} account. If it is registered, we will send a reset link.")}
          </p>
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-secondary-ink">
              {t("Email or username")}
            </span>
            <input
              className={inputCls}
              type="text"
              autoComplete="username"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              value={usr}
              onChange={(e) => setUsr(e.target.value)}
              placeholder={t("Administrator or you@hotel.com")}
              required
            />
          </label>

          {error && (
            <div className="rounded border border-danger-line bg-danger-subtle px-3 py-2 text-sm text-danger-ink">
              {translate(error)}
            </div>
          )}
          {resetSent && (
            <div className="rounded border border-success-line bg-success-subtle px-3 py-2 text-sm text-success-ink">
              {t("If this email is registered with us, we have sent password reset instructions to it. Please check your inbox.")}
            </div>
          )}

          <Button
            className="w-full justify-center rounded bg-primary-button py-2 text-on-solid hover:bg-primary-button-hover"
            disabled={busy || !usr.trim() || resetSent}
            type="submit"
          >
            {busy ? t("Sending…") : t("Send reset link")}
          </Button>
          <button
            type="button"
            className="w-full text-center text-sm text-muted-ink hover:text-accent-ink"
            onClick={() => {
              setMode("sign-in")
              setError(null)
              setResetSent(false)
            }}
          >
            {t("Back to sign in")}
          </button>
        </form>
        )}

        {!whiteLabel() && demoMode && mode === "sign-in" && (
        <div className="mt-4 rounded border border-dashed border-control p-4">
          <p className="mb-3 rounded bg-warning-subtle px-3 py-2 text-center text-xs leading-relaxed text-warning-ink">
            {t("Shared playground, not a live hotel. Play data is wiped every night and the sample hotel is seeded again. Don't put real guests, payments or API keys here.")}
          </p>
          <p className="mb-2 text-center text-xs text-muted-ink">
            {t("Demo accounts - one tap to try each role")}
          </p>
          <div className="grid grid-cols-2 gap-2">
            {DEMO_ACCOUNTS.map((a) => (
              <button
                key={a.usr}
                disabled={busy}
                onClick={() => submit(a.usr, a.pwd)}
                className="rounded border border-control bg-surface px-2 py-1.5 text-xs font-medium text-secondary-ink hover:border-focus hover:text-accent-ink"
              >
                {t(a.label)}
              </button>
            ))}
          </div>
        </div>
        )}
        {!whiteLabel() && version && (
          <p className="mt-6 text-center text-[11px] text-muted-ink">
            {product()} PMS v{version}
          </p>
        )}
      </div>
    </div>
  )
}
