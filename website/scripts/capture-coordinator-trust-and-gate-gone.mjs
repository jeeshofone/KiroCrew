/**
 * Frames for two approval states that only a live run shows (#16389):
 *   22-composer-native-trust: a chat runner's own request in the composer
 *       approval bar, with Trust beside Allow once and Reject.
 *   23-composer-coordinator-no-trust: a coordinator approval parked in the
 *       chat, decided one-shot by its own target: no Trust, and a muted line
 *       under the buttons saying why. Read against frame 22.
 *   24-project-gate-gone: a running project with two tasks at their gates.
 *       Task 1's gate is listed with no instance, so nothing can decide it:
 *       the notice names task 1, and only task 2 offers Approve/Deny.
 * Each frame asserts its text and controls before it is written.
 *
 * Drives the isolated capture entries (website/capture/composer-coordinator-trust.html
 * and website/capture/project-gate-gone.html), which mount the REAL ChatInput
 * and ProjectDetailPage.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6825 --strictPort   # in another shell
 *   node scripts/capture-coordinator-trust-and-gate-gone.mjs http://127.0.0.1:6825 ../temp-screenshots/notification-approval-refusal
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6825'
const OUT = process.argv[3] || '../temp-screenshots/notification-approval-refusal'
mkdirSync(OUT, { recursive: true })

const NO_TRUST = "This request is decided once here, so Trust can't be saved for it."
const GATE_GONE = 'Task 1 "Migrate the database": this approval has expired or was already decided'
const browser = await chromium.launch()
let failed = false

function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${JSON.stringify(detail)}`)
  if (!ok) failed = true
  return ok
}

// Gateway-free: answer every REAL API call the mounted page makes. Predicate
// on the pathname, so vite-served source modules under /src/api/ still load.
async function stub(page, approvals = []) {
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/approvals') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(approvals) })
    const isList = /commands|skills|agents|sessions|files|history|models|tasks|runs/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
}

for (const kind of ['native', 'coordinator']) {
  const page = await browser.newPage({ viewport: { width: 720, height: 360 }, deviceScaleFactor: 2 })
  await stub(page)
  await page.goto(`${BASE}/capture/composer-coordinator-trust.html?theme=dark&kind=${kind}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('Allow once').waitFor()
  await page.waitForTimeout(400)
  const s = {
    trust: await page.getByRole('button', { name: /^Trust/ }).count(),
    line: await page.getByTestId('approval-one-shot-only').count(),
    lineText: (await page.getByTestId('approval-one-shot-only').allInnerTexts()).join(''),
  }
  const name = kind === 'native' ? '22-composer-native-trust' : '23-composer-coordinator-no-trust'
  const ok = kind === 'native' ? s.trust >= 1 && s.line === 0 : s.trust === 0 && s.lineText === NO_TRUST
  if (check(name, ok, s)) await page.screenshot({ path: `${OUT}/${name}.png` })
  await page.close()
}

{
  const page = await browser.newPage({ viewport: { width: 1100, height: 640 } })
  await stub(page, [
    { id: 'task-gate-1-aaaa', source: 'taskrunner', slot: '', task_run: 'run-1' },
    { id: 'task-gate-2-bbbb', source: 'taskrunner', slot: '', instance: 'inst-2', task_run: 'run-1' },
  ])
  await page.goto(`${BASE}/capture/project-gate-gone.html?theme=dark`)
  await page.waitForSelector('[data-capture-root]')
  const notice = page.getByTestId('project-detail-gate-gone')
  await notice.waitFor()
  await page.waitForTimeout(800)
  const s = {
    notice: await notice.innerText(),
    approve: await page.getByRole('button', { name: /^Approve/ }).count(),
  }
  const ok = s.notice.includes(GATE_GONE) && !s.notice.includes('Rotate the API keys') && s.approve >= 1
  if (check('24-project-gate-gone', ok, s)) await page.screenshot({ path: `${OUT}/24-project-gate-gone.png` })
  await page.close()
}

await browser.close()
process.exit(failed ? 1 : 0)
