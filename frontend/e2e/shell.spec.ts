/**
 * e2e smoke: the shell boots, both shells render, and the SPA does not require a live
 * backend to show a page.
 *
 * Kept deliberately small. Its job is to prove the build is SERVABLE and that the
 * router works — not to duplicate the Vitest suites that own the contracts. Data-driven
 * flows belong to the phase-specific specs once the backend modules land.
 */

import { expect, test } from '@playwright/test'

test.describe('application shell', () => {
  test('consumer store boots and shows the home page', async ({ page }) => {
    await page.goto('/')

    await expect(
      page.getByRole('banner').getByRole('link', { name: /Nova Commerce/ }),
    ).toBeVisible()
    await expect(page.getByRole('heading', { name: '为你推荐' })).toBeVisible()

    // The document title is set by the router's afterEach hook.
    await expect(page).toHaveTitle(/Nova/)
  })

  test('client-side routing reaches the search page', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('link', { name: '全部商品' }).first().click()

    await expect(page).toHaveURL(/\/search/)
    await expect(page.locator('.search__input')).toBeVisible()
  })

  test('an unknown URL renders the 404 view instead of a blank screen', async ({ page }) => {
    await page.goto('/definitely-not-a-route')
    await expect(page.getByRole('heading', { name: '页面不存在' })).toBeVisible()
  })

  test('the console shell is reachable and shows its navigation', async ({ page }) => {
    await page.goto('/console/dashboard')

    // Either the console renders, or the guard redirects to login/forbidden. Both are
    // valid without a session; a blank page is not.
    await expect(
      page.locator('.console-sidebar, .login__card, .forbidden').first(),
    ).toBeVisible({ timeout: 15_000 })
  })

  test('theme toggle switches the dark class on <html>', async ({ page }) => {
    await page.goto('/')
    const html = page.locator('html')
    const initial = await html.getAttribute('class')
    const wasDark = (initial ?? '').includes('dark')

    await page.getByRole('button', { name: /^(深色|浅色)模式$/ }).first().click()
    await expect(html).toHaveClass(wasDark ? /^(?!.*dark)/ : /dark/)
  })
})
