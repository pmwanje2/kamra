// Run against a disposable local bench with a hyphenated room, open folio and
// a checkout-clean task. The task click below writes to this fixture.
import assert from 'node:assert/strict'
import { chromium } from 'playwright'
import { readFileSync } from 'node:fs'
const origin = new URL(process.env.KAMRA_TEST_URL)
assert.ok(['localhost','127.0.0.1'].includes(origin.hostname) && process.env.KAMRA_DISPOSABLE === '1')
const browser = await chromium.launch({executablePath:process.env.CHROMIUM})
const ctx = await browser.newContext({baseURL:origin.origin})
const login = await ctx.request.post('/api/method/login', {data:{usr:process.env.KAMRA_TEST_USER,pwd:process.env.KAMRA_TEST_PASSWORD}})
assert.equal((await login.json()).message, 'Logged In')
const page = await ctx.newPage(); page.setDefaultTimeout(5000)
const room = process.env.KAMRA_TEST_ROOM, folio = process.env.KAMRA_TEST_FOLIO
const failures = []
async function check(label, run) { try { await run(); console.log('PASS',label) } catch(e) {failures.push(label); console.error('FAIL',label,e.message.slice(0,350))} }
try {
for (const width of [360,375,412,414,1280]) {
 await page.setViewportSize({width,height:width===360?740:915})
 for (const [path, text] of [['/','Morning Guest'],['/in-house?tab=in-house','Morning Guest'],['/billing',folio],['/billing/'+folio,folio],['/housekeeping-board',room]]) {
  await check(`${width}px ${path}`,async()=>{
   await page.goto(path); await page.locator('main').getByText(text,{exact:false}).filter({visible:true}).first().waitFor()
   assert.ok(await page.evaluate(()=>document.scrollingElement.scrollWidth<=innerWidth),'page overflows')
   if(width<640 && (path==='/billing'||path.startsWith('/in-house'))) {
    const row=page.locator('tr').filter({hasText:text}).first(); assert.ok(await row.evaluate(e=>e.getBoundingClientRect().right<=innerWidth),'primary row needs sideways scrolling')
   }
  })
 }
}
await page.setViewportSize({width:375,height:812}); await page.goto('/in-house?tab=in-house'); await page.getByText('Morning Guest',{exact:true}).waitFor()
await check('business date and pending EOD in header',async()=>{await page.locator('header').getByText('Business date',{exact:false}).first().waitFor({state:'attached'}); await page.locator('header').getByText(/End of Day pending/).waitFor({state:'attached'})})
await check('authoritative room caption',async()=>assert.ok(await page.locator('tr').filter({hasText:'Morning Guest'}).getByText(room,{exact:true}).isVisible()))
await check('overdue tab',async()=>{await page.getByRole('tab',{name:/Overdue/}).click();await page.locator('main').getByText('Morning Guest',{exact:true}).waitFor()})
await check('booking validation messages and focus',async()=>{
 await page.getByRole('button',{name:'New booking',exact:true}).click();const dialog=page.getByRole('dialog'); await dialog.locator('input[type=tel]').first().fill('123'); const confirm=dialog.getByRole('button',{name:'Confirm booking'});assert.ok(await confirm.isEnabled(),'Confirm must explain all blockers'); let writes=0;page.on('request',r=>{if(r.url().endsWith('.create_booking')) writes++});await confirm.click();const errors=dialog.getByRole('region',{name:'Booking messages'});await errors.waitFor();await errors.getByRole('button',{name:/guest name/i}).click();assert.equal(await page.locator(':focus').getAttribute('aria-invalid'),'true');assert.equal(writes,0); await page.keyboard.press('Escape')
})
await check('linked clean primary action',async()=>{await page.goto('/housekeeping-board');await page.getByRole('button',{name:new RegExp(room)}).first().click();const sent=page.waitForResponse(r=>r.url().endsWith('.hk_update_task'));sent.catch(()=>{});await page.getByRole('button',{name:'Complete Checkout Clean',exact:true}).click();assert.equal((await (await sent).json()).message.status,'Done')})
await check('localized tax copy',()=>{for(const file of ['Billing.tsx','BanquetDocument.tsx','configs.ts'])assert.doesNotMatch(readFileSync(new URL(`../src/screens/${file}`,import.meta.url),'utf8'),/\bGST\b/)})
} finally {await browser.close()}
assert.deepEqual(failures,[])
