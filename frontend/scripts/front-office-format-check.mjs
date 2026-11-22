import assert from 'node:assert/strict'
import fs from 'node:fs'
import ts from 'typescript'
const read = (path) => fs.readFileSync(new URL(`../src/${path}`, import.meta.url),'utf8')
const mod = {}; new Function('exports',ts.transpileModule(read('lib/utils.ts'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText)(mod)
globalThis.window={kamra_date_format:'dd-mm-yyyy'}
assert.equal(mod.formatDate?.('2031-03-12'),'12-03-2031','display follows the site date format')
window.kamra_date_format='mm/dd/yyyy'; assert.equal(mod.formatDate('2031-03-12'),'03/12/2031')
assert.equal(mod.roomLabel?.({room:'North-Inn-A-12',room_number:'A-12'}),'A-12')
for(const file of ['components/BookingDialog.tsx','screens/Billing.tsx','screens/InHouse.tsx'])assert.match(read(file),/\bformatDate\(/,file)

// Keep the front-office views on the same civil-date and room-label contract.
const { test } = await import('node:test')
for (const [label, file, expression] of [
  ['Today heading', 'screens/Today.tsx', /formatDate\(snap\??\.date\)/],
  ['charge date', 'screens/FolioView.tsx', /formatDate\(c\.posting_date\)/],
  ['payment date', 'screens/FolioView.tsx', /formatDate\(p\.posting_date\)/],
  ['folio room', 'screens/FolioView.tsx', /roomLabel\(stay\)/],
]) test(label, () => assert.ok(expression.test(read(file)), label))
test('loading a room never guesses a number from its document ID', () => {
  assert.ok(!read('components/RoomDrawer.tsx').includes('props.room.split('))
})
test('successful recovery announces the business date; refusal leaves it alone', async () => {
  const events = new EventTarget()
  let changes = 0
  events.addEventListener('kamra:business-date', () => changes++)
  const recovered = { ok: true, next_business_date: '2031-03-13' }
  const api = { call: async () => recovered }
  const eod = {}
  const js = ts.transpileModule(read('lib/eod.ts'), {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText
  new Function('exports', 'require', 'window', js)(eod, () => api, events)
  assert.equal(await eod.recoverStuckBusinessDate('North Inn'), recovered)
  assert.equal(changes, 1)
  api.call = async () => { throw new Error('refused') }
  await assert.rejects(eod.recoverStuckBusinessDate('North Inn'), /refused/)
  assert.equal(changes, 1)
})
