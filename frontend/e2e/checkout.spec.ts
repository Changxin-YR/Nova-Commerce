import { expect, test, type Route } from '@playwright/test'

function reply(route: Route, data: unknown): Promise<void> {
  return route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ code: 0, message: 'OK', data, trace_id: 'e2e-checkout' }),
  })
}

test('checkout previews the server total and sends one order to payment', async ({ page }) => {
  const previews: Record<string, unknown>[] = []
  const orders: { body: Record<string, unknown>; idempotencyKey: string | undefined }[] = []
  const payments: Record<string, unknown>[] = []

  await page.addInitScript(() => {
    localStorage.setItem('nova.access_token', 'browser-checkout-test')
    localStorage.setItem('nova:cart:v1:17', JSON.stringify([{
      id: '42', product_id: '7', sku_id: '42', quantity: 2, selected: true,
    }]))
  })

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/v1/auth/users/me') {
      return reply(route, { id: 17, username: 'buyer', display_name: 'Buyer', roles: ['customer'] })
    }
    if (path === '/api/v1/auth/users/me/permissions') {
      return reply(route, { roles: ['customer'], permissions: [] })
    }
    if (path === '/api/v1/auth/users/addresses') {
      return reply(route, { items: [{
        id: 3, receiver_name: 'Buyer', receiver_phone: '13800000000',
        province: '浙江省', city: '杭州市', district: '西湖区', detail: '1号', is_default: true,
      }], meta: { page: 1, page_size: 20, total: 1, total_pages: 1 } })
    }
    if (path === '/api/v1/marketing/coupons/mine') {
      return reply(route, [{
        id: 55, template_id: 8, merchant_id: 1, status: 'UNUSED',
        valid_from: '2020-01-01T00:00:00Z', valid_to: '2099-01-01T00:00:00Z',
        order_id: null, locked_at: null, used_at: null,
      }])
    }
    if (path === '/api/v1/orders/preview' && request.method() === 'POST') {
      const body = request.postDataJSON() as Record<string, unknown>
      previews.push(body)
      const couponDiscount = body.coupon_id === 55 ? 100 : 0
      return reply(route, {
        items: [{
          sku_id: 42, product_id: 7, product_name: 'Browser Test Product',
          sku_name: 'Standard', image_url: null, unit_price: 500, quantity: 2,
          original_amount: 1000, promotion_discount_amount: 0,
          coupon_discount_amount: couponDiscount,
          allocated_discount_amount: couponDiscount, payable_amount: 1000 - couponDiscount,
        }],
        original_amount: 1000, promotion_discount_amount: 0,
        coupon_discount_amount: couponDiscount, shipping_amount: 0,
        payable_amount: 1000 - couponDiscount, warnings: [],
      })
    }
    if (path === '/api/v1/orders' && request.method() === 'POST') {
      orders.push({
        body: request.postDataJSON() as Record<string, unknown>,
        idempotencyKey: request.headers()['idempotency-key'],
      })
      return reply(route, { order_no: 'N-E2E-1' })
    }
    if (path === '/api/v1/payments/customer/payments' && request.method() === 'POST') {
      payments.push(request.postDataJSON() as Record<string, unknown>)
      return reply(route, { id: '99', order_no: 'N-E2E-1' })
    }
    if (path === '/api/v1/payments/customer/payments/99') {
      return reply(route, {
        id: '99', order_no: 'N-E2E-1', amount: 900, status: 'PENDING',
      })
    }
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })

  await page.goto('/checkout')
  await expect(page.getByRole('heading', { name: '确认订单' })).toBeVisible()
  await expect(page.getByText('Browser Test Product')).toBeVisible()
  await expect(page.getByText('浙江省杭州市西湖区1号')).toBeVisible()
  await expect(page.locator('.checkout__amounts .nx-rows--total')).toContainText('10.00')

  await page.locator('select.checkout__coupon').selectOption('55')
  await expect(page.locator('.checkout__amounts .nx-rows--total')).toContainText('9.00')
  await page.getByRole('button', { name: '提交订单' }).click()
  await expect(page).toHaveURL(/\/mock-pay\/99$/)
  await expect(page.getByRole('heading', { name: '订单 N-E2E-1' })).toBeVisible()

  expect(previews.at(-1)).toMatchObject({
    items: [{ sku_id: 42, quantity: 2 }], address_id: 3, coupon_id: 55,
  })
  expect(orders).toHaveLength(1)
  expect(orders[0].body).toMatchObject({
    items: [{ sku_id: 42, quantity: 2 }], address_id: 3, coupon_id: 55,
  })
  expect(orders[0].idempotencyKey).toBe(orders[0].body.client_request_id)
  expect(payments).toHaveLength(1)
  expect(payments[0]).toMatchObject({ order_no: 'N-E2E-1', channel: 'MOCK' })
})
