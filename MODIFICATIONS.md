# Modified files notice

This fork contains changes made by **pmwanje2 on 2026-10-30**, based on Kamra
commit `24f7a025`. The original copyright and licence notices are preserved.
These modifications remain under the GNU Affero General Public License,
version 3, supplied in `license.txt`.

The changes use native Frappe Website Settings for the SPA name, logo and
browser title; hide unconfigured or disabled assistant navigation and routes;
treat status failures as disabled; and make update wording product-neutral
without changing the reported versions.

Modified source files:

- `frontend/package.json`
- `frontend/src/App.tsx`
- `frontend/src/AppShell.tsx`
- `frontend/src/components/CommandPalette.tsx`
- `frontend/src/components/HelpPanel.tsx`
- `frontend/src/components/UpdateNag.tsx`
- `frontend/src/i18n/catalog.csv`
- `frontend/src/i18n/locales/ar.json`
- `frontend/src/lib/apps.ts`
- `frontend/src/lib/siteInfo.ts`
- `frontend/src/main.tsx`
- `frontend/src/screens/AppLauncher.tsx`
- `frontend/src/screens/Assistant.tsx`
- `kamra/public_api.py`
- `kamra/scripts/eval_harness.py`

Added test files:

- `frontend/scripts/branding-assistant-check.mjs`
- `kamra/tests/test_site_branding.py`

Added synthetic dev-server screenshots:

- `docs/screenshots/native-branding.png`
- `docs/screenshots/disabled-assistant-search.png`

The built files under `kamra/public/frontend/` were regenerated from this
branch's frontend sources and locked dependencies with `npm ci` and
`npm run build`; they were not copied from a later Kamra release or edited by
hand. This notice was also added on the date above.

The source changes are cherry-picked from upstream-proposal commits
`7c2394429507c2529aa9e8163b3ccd986dee86fe` (tests) and
`3d65e5f901c923e235190f2b6a5f97d1530ec625` (implementation).
The follow-up tests-only regression from
`f684fea30c1e93c12dccfab210c026caf61332a8` and cherry-picked correction
`4432e97b346d4ac68c75d16caaee2644f698da62` keep unmatched-route navigation
inside the same filtered registry.
The empty-registry regression from
`e3dfa5c327fec3cb7fe8d17ec5c1a26c796d2ca6` and cherry-picked correction
`b4492ea5ef0b81f2a067d68da1dd12279aba7505` prevent an empty visible-app
list from restoring hidden navigation. The same regression harness also
uses native list-shaped search fixtures, waits for completed searches,
and rejects unhandled browser errors.
The upstream review follow-ups use tests-only RED commits
`90f46eebe81a07cf79c32d62d94cfbed86b42228` and
`2c3346f2031d5ca852953a674a30b632bbe19a01`, followed by separate corrections
`9b2735fff85d5cf5423482355f535bcd16251d22` and
`da65110b820176fd5fe2bc556ea5c28bfbac2042`. Their equivalent changes here
key assistant status by both pathname and property before rendering a route,
and require a null-safe literal `true` in the help panel's own status check.
Conflict resolution preserves the pin's existing Arabic-only locale and
assistant/app-shell structure, without importing later features.

This branch is a proposal for later pin acceptance, not a release or deployment.
