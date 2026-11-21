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
