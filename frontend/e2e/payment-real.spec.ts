import { execFileSync } from 'node:child_process'
import path from 'node:path'
import { expect, test } from '@playwright/test'

const root = path.resolve(process.cwd(), '..')
const python = path.join(root, '.venv', 'Scripts', 'python.exe')

function fixture(mode: string, shop?: Record<string, unknown>): string {
  return execFileSync(python, ['-m', 'tests.e2e.payment_browser_fixture', mode,
    ...(shop ? ['--shop', JSON.stringify(shop)] : [])], {
    cwd: path.join(root, 'backend'), encoding: 'utf8', timeout: 30_000,
  }).trim().split(/\r?\n/).at(-1) ?? ''
}

test('real browser payment persists all seven transaction facts once', async ({ page, request }) => {
  const seeded = JSON.parse(fixture('seed-checkout'))
  let shop = seeded.shop
  try {
    const login = await request.post('http://127.0.0.1:8000/api/v1/auth/login', {
      data: { username: shop.consumer_username, password: seeded.password },
    })
    expect(login.ok()).toBe(true)
    const token = (await login.json()).data.access_token
    await page.addInitScript(({ accessToken, consumerId, productId, skuIds }) => {
      localStorage.setItem('nova.access_token', accessToken)
      localStorage.setItem(`nova:cart:v1:${consumerId}`, JSON.stringify(skuIds.map((skuId: number, index: number) => ({
        id: String(skuId), product_id: String(productId), sku_id: String(skuId), quantity: index + 1, selected: true,
      }))))
    }, { accessToken: token, consumerId: shop.consumer_id, productId: shop.product_id, skuIds: shop.sku_ids })
    await page.goto('/checkout')
    await expect(page.getByRole('heading', { name: '确认订单' })).toBeVisible()
    await expect(page.locator('.checkout__amounts .nx-rows--total')).toContainText('79.97')
    await page.getByRole('button', { name: '提交订单' }).click()
    await expect(page).toHaveURL(/\/mock-pay\/\d+$/)
    shop = JSON.parse(fixture('resolve', shop))
    await expect(page.getByRole('heading', { name: `订单 ${shop.order_no}` })).toBeVisible()
    const settlementResponse = page.waitForResponse(response => response.url().endsWith(`/payments/${shop.payment_id}/mock-pay`))
    await page.getByRole('button', { name: '模拟支付成功', exact: true }).click()
    expect((await (await settlementResponse).json()).code).toBe(0)
    await expect(page).toHaveURL(new RegExp(`/orders/${shop.order_no}`))
    expect(JSON.parse(fixture('verify', shop)).verified).toHaveLength(7)
    const replay = await request.post(`http://127.0.0.1:8000/api/v1/payments/customer/payments/${shop.payment_id}/mock-pay`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    expect(replay.status()).toBe(200)
    expect((await replay.json()).code).toBe(0)
    expect(JSON.parse(fixture('verify', shop)).verified).toHaveLength(7)
  } finally {
    fixture('cleanup', shop)
  }
})
