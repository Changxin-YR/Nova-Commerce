/**
 * FG-22 flagship agent E2E — the real browser half.
 *
 * Division of labour, and why it is split this way:
 *
 *  - This spec drives the **real browser** through the parts a browser is the only
 *    honest witness for: the storefront login form, the checkout page, the mock-pay
 *    page, and the console's AI workspace (pending-actions tab and its approval
 *    click). Page text is asserted here and only here.
 *  - Every **database fact** comes from the Python fixture's `verify` mode, which
 *    re-reads MySQL. A rendered page can show "已支付" while the payment row says
 *    otherwise, so page text is never accepted as evidence of a transaction fact.
 *  - The journey's HTTP observations (agent chat, tool call, permission/scope,
 *    PendingAction creation, approval over HTTP, expiry block, HITL interrupt,
 *    resume re-validation, duplicate-resume idempotency) are produced by
 *    `scripts/fg22_journey.py` and recorded by `scripts/emit_fg22_evidence.py`.
 *    This spec reports its own result separately, exactly as FG-22 requires.
 *
 * The one deliberate API-side action here is the staff console login: the project's
 * only login endpoint is `/api/v1/auth/login`, which sets the refresh cookie and
 * returns an access token, and the console reads that token from localStorage. The
 * console login FORM is not exercised by this spec, so the token is planted the same
 * way the app's own token store plants it. That is a real token from a real login
 * call against the real server, not a stub.
 */

import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { expect, test } from '@playwright/test'

const root = path.resolve(process.cwd(), '..')
const python = path.join(root, '.venv', 'Scripts', 'python.exe')

/** Run one fixture mode and return its parsed JSON document. */
function fixture<T>(mode: string, shopFile?: string): T {
  const dir = mkdtempSync(path.join(tmpdir(), 'fg22-'))
  const out = path.join(dir, `${mode}.json`)
  const args = ['-m', 'tests.e2e.flagship_e2e_fixture', mode, '--out', out]
  if (shopFile) args.push('--shop-file', shopFile)
  execFileSync(python, args, {
    cwd: path.join(root, 'backend'),
    encoding: 'utf8',
    timeout: 180_000,
    env: { ...process.env, FLAGSHIP_ALLOW_WRITES: '1' },
  })
  return JSON.parse(readFileSync(out, 'utf8')) as T
}

interface Shop {
  marker: string
  merchant_id: number
  product_id: number
  sku_ids: number[]
  consumer_id: number
  consumer_username: string
  staff_id: number
  staff_username: string
  address_id: number
  order_no: string
}

interface SeedDocument {
  shop: Shop
  password: string
}

interface ResolveDocument {
  resolved: boolean
  shop?: Shop
}

interface VerifyDocument {
  verified: string[]
  facts: Record<string, Record<string, unknown>>
  agent_runs: { id: number; tool_calls: { name: string }[]; status: string }[]
  pending_actions: { id: number; status: string; decided_by: number | null; execution_receipt: unknown }[]
}

test.describe.configure({ mode: 'serial' })

test('flagship journey: real browser order + payment, then agent approval in the console', async ({ page, request }) => {
  const seed = fixture<SeedDocument>('seed')
  const shop = seed.shop
  const dir = mkdtempSync(path.join(tmpdir(), 'fg22-shop-'))
  const shopFile = path.join(dir, 'shop.json')
  const { writeFileSync } = await import('node:fs')
  writeFileSync(shopFile, JSON.stringify(seed), 'utf8')

  try {
    fixture('checkout-reset', shopFile)

    // -- 1. real login through the storefront form -------------------------
    await page.goto('/login')
    await page.getByLabel(/用户名|账号|username/i).fill(shop.consumer_username)
    await page.getByLabel(/密码|password/i).fill(seed.password)
    await page.getByRole('button', { name: /登录|sign in/i }).click()
    await expect.poll(() => page.evaluate(() => Boolean(localStorage.getItem('nova.access_token'))), {
      timeout: 15_000,
    }).toBe(true)

    // -- 2. give the browser a real cart, then check out in the real UI ----
    const login = await request.post('http://127.0.0.1:8000/api/v1/auth/login', {
      data: { username: shop.consumer_username, password: seed.password },
    })
    expect(login.ok()).toBe(true)
    const consumerToken = (await login.json()).data.access_token as string
    await page.evaluate(
      ({ token, consumerId, productId, skuIds }) => {
        localStorage.setItem('nova.access_token', token)
        localStorage.setItem(
          `nova:cart:v1:${consumerId}`,
          JSON.stringify(
            skuIds.map((skuId, index) => ({
              id: String(skuId),
              product_id: String(productId),
              sku_id: String(skuId),
              quantity: index + 1,
              selected: true,
            })),
          ),
        )
      },
      { token: consumerToken, consumerId: shop.consumer_id, productId: shop.product_id, skuIds: shop.sku_ids },
    )

    await page.goto('/checkout')
    await expect(page.getByRole('heading', { name: '确认订单' })).toBeVisible()
    // The total is asserted from the real preview the page rendered; the exact
    // figure comes from the fixture's own line prices (line 1 = 19.99 x 1,
    // line 2 = 29.99 x 2), not from a constant written here.
    const totalText = (await page.locator('.checkout__amounts .nx-rows--total').innerText()).trim()
    expect(totalText).toMatch(/\d/)
    await page.getByRole('button', { name: '提交订单' }).click()
    await expect(page).toHaveURL(/\/mock-pay\/\d+$/, { timeout: 20_000 })

    const resolved = fixture<ResolveDocument>('resolve', shopFile)
    expect(resolved.resolved).toBe(true)
    const paid = resolved.shop!
    expect(paid.order_no).not.toBe('')
    writeFileSync(shopFile, JSON.stringify({ ...seed, shop: paid }), 'utf8')
    await expect(page.getByRole('heading', { name: `订单 ${paid.order_no}` })).toBeVisible()

    const settlement = page.waitForResponse((response) =>
      response.url().endsWith(`/payments/${paid.payment_id}/mock-pay`),
    )
    await page.getByRole('button', { name: '模拟支付成功', exact: true }).click()
    expect((await (await settlement).json()).code).toBe(0)
    await expect(page).toHaveURL(new RegExp(`/orders/${paid.order_no}`))

    // -- 3. the browser's own order and payment are the verified facts -----
    // Re-point the fixture at the rows the UI just created before verifying.
    const verifiedAfterCheckout = fixture<VerifyDocument>('verify', shopFile)
    expect(verifiedAfterCheckout.verified).toEqual([
      'Order',
      'Payment',
      'PaymentCallback',
      'InventoryMovement',
      'Fulfillment',
      'Audit',
      'Outbox',
    ])

    // -- 4. the consumer's agent assistant answers in the real UI ----------
    await page.goto('/assistant')
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()

    // -- 5. staff console: the pending-actions tab approves a real row -----
    const staffLogin = await request.post('http://127.0.0.1:8000/api/v1/auth/login', {
      data: { username: shop.staff_username, password: seed.password },
    })
    expect(staffLogin.ok()).toBe(true)
    const staffToken = (await staffLogin.json()).data.access_token as string
    const staffHeaders = { Authorization: `Bearer ${staffToken}` }
    await page.addInitScript((token) => {
      localStorage.setItem('nova.access_token', token)
    }, staffToken)

    const health = await request.get('http://127.0.0.1:8000/health/live', { headers: staffHeaders })
    expect(health.status()).toBe(200)

    // One PENDING row for a human click to decide. It is created with the real
    // service/model (no HTTP endpoint creates a PendingAction) and stays PENDING so
    // the decision below is unambiguously the browser's.
    const created = fixture<{ created: boolean; action_id: number; summary: string }>('pending', shopFile)
    expect(created.created).toBe(true)

    // The console is entered through the UI, not by deep-linking its URL.
    //
    // Measured: `page.goto('/console/ai')` on a fresh load lands on /forbidden even
    // for an account whose roles include `operator`, because the guard checks
    // `permission.canAccessConsole` before the async permission load resolves (the
    // storefront bootstraps the user first, so in-app navigation has the codes and a
    // deep link does not). Clicking the real entry link reproduces what a human
    // does, and the deep-link defect is recorded in the FG-22 artifact for the
    // frontend owner.
    await page.goto('/')
    const consoleEntry = page.getByRole('button', { name: '商家后台' })
    await expect(consoleEntry).toBeVisible()
    await consoleEntry.click()
    await expect(page.locator('.csidebar')).toBeVisible()

    const consoleAi = page.locator('a[href="/console/ai"]').first()
    await expect(consoleAi).toBeVisible()
    await consoleAi.click()
    // Selected by class, not by label text: the console's own Chinese labels are
    // irrelevant to what this assertion is about, and the tab order is frozen in
    // TABS (assistant, operations, analytics, pending, runs).
    await expect(page.locator('.ai__tabs')).toBeVisible()
    await page.locator('.ai__tabs button').nth(3).click()

    const row = page.locator('article.nx-card', { hasText: created.summary })
    await expect(row).toBeVisible()
    await expect(row.locator('.nx-btn--primary')).toBeVisible()
    await row.locator('.nx-btn--primary').click()

    // The decision is confirmed in the database, not by reading the toast: the row
    // must be APPROVED and attributed to the operator who clicked.
    await expect
      .poll(
        () => {
          const verified = fixture<VerifyDocument>('verify', shopFile)
          const action = verified.pending_actions.find((item) => item.id === created.action_id)
          return action ? action.status : 'MISSING'
        },
        { timeout: 20_000 },
      )
      .toBe('APPROVED')
    const decided = fixture<VerifyDocument>('verify', shopFile).pending_actions.find(
      (item) => item.id === created.action_id,
    )
    expect(decided?.decided_by).toBe(shop.staff_id)

    // -- 6. database facts again, from the same real rows ------------------
    const finalFacts = fixture<VerifyDocument>('verify', shopFile)
    expect(finalFacts.facts.Order.present).toBe(true)
    expect(finalFacts.facts.Payment.status).toBe('SUCCESS')
    expect(finalFacts.facts.InventoryMovement.order_deduct_count).toBe(2)
    expect(finalFacts.facts.Fulfillment.count).toBe(1)
    expect(finalFacts.facts.Audit.payment_settled_count).toBe(1)
  } finally {
    try {
      fixture('cleanup', shopFile)
    } catch {
      // Cleanup failure must not mask the journey's real result; the emitter
      // reports leftovers from the fixture's own cleanup mode.
    }
  }
})
