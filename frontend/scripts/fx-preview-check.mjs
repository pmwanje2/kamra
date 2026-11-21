import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
const src = file => readFileSync(new URL('../src/'+file,import.meta.url),'utf8')
for (const file of ['screens/FolioView.tsx','components/BookingDialog.tsx','components/DepositPanel.tsx']) {
  assert.ok(src(file).includes('<SettlementPreview'), `${file}: show the settlement before posting`)
  assert.doesNotMatch(src(file), /exchange_rate:\s*(?:fx|rate)/)
}
const preview=src('components/SettlementPreview.tsx')
for(const label of ['Amount tendered','Exchange rate','Effective rate date','Company-currency equivalent','Remaining balance','Change due']) assert.ok(preview.includes(label),label)
assert.ok(preview.includes('formatDate('))
assert.ok(src('screens/FolioView.tsx').includes('<RowCorrection'))
