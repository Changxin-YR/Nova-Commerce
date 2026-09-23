/**
 * Console · Marketing — the preview -> confirm guarantee (§47, §12.1).
 *
 * WHAT THIS LOCKS DOWN, and why it is a test rather than a comment:
 *  §47 requires promotion/coupon creation to be previewed first. The mechanism §12.1 describes is a
 *  `preview_token`: the create call must echo the token the preview returned, so the server can prove
 *  the operator approved the thing being written. If the UI could create without a token the rule
 *  would be a convention, and conventions drift silently.
 *
 *  The tests below assert the FLOW property, not a particular layout: nothing is written from the
 *  form step, and what IS written carries the token from the preview response.
 *
 * Mocked at the API MODULE boundary (`vi.mock('@/api')`), so these keep working once the real module
 * lands.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import MarketingView from '@/views/console/MarketingView.vue'
import { useNotificationStore } from '@/stores/notification'

const couponsMock = vi.fn()
const previewCouponMock = vi.fn()
const createCouponMock = vi.fn()

vi.mock('@/api', () => ({
  marketingAdminApi: {
    coupons: (...args: unknown[]) => couponsMock(...args),
    promotions: vi.fn().mockResolvedValue({ items: [], meta: { page: 1, page_size: 20, total: 0, total_pages: 0 } }),
    previewCoupon: (...args: unknown[]) => previewCouponMock(...args),
    createCoupon: (...args: unknown[]) => createCouponMock(...args),
    publishPromotion: vi.fn(),
    unpublishPromotion: vi.fn(),
  },
}))

function emptyPage() {
  return { items: [], meta: { page: 1, page_size: 20, total: 0, total_pages: 0 } }
}

function mountView() {
  const pinia = createPinia()
  setActivePinia(pinia)
  return mount(MarketingView, { global: { plugins: [pinia] } })
}

/** Fill the coupon form with a valid payload. */
async function fillForm(wrapper: ReturnType<typeof mountView>): Promise<void> {
  const inputs = wrapper.findAll('.marketing__fields input')
  await inputs[0]?.setValue('NOVA100')
  await inputs[1]?.setValue('满 1000 减 10')
  await inputs[2]?.setValue('10')
  await inputs[3]?.setValue('1000')
}

async function findButton(wrapper: ReturnType<typeof mountView>, label: string) {
  return wrapper.findAll('button').find((button) => button.text().trim() === label)
}

describe('console Marketing — coupon creation is preview then confirm (§47, §12.1)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    couponsMock.mockResolvedValue(emptyPage())
  })

  it('opens the form without writing anything', async () => {
    const wrapper = mountView()
    await flushPromises()

    const openButton = await findButton(wrapper, '新建优惠券')
    await openButton?.trigger('click')
    await flushPromises()
    await fillForm(wrapper)

    // Typing is not submitting: no network write may have happened yet.
    expect(previewCouponMock).not.toHaveBeenCalled()
    expect(createCouponMock).not.toHaveBeenCalled()
  })

  it('预览 calls the PREVIEW endpoint and still writes nothing', async () => {
    previewCouponMock.mockResolvedValue({ preview_token: 'tok-1' })

    const wrapper = mountView()
    await flushPromises()
    ;(await findButton(wrapper, '新建优惠券'))?.trigger('click')
    await flushPromises()
    await fillForm(wrapper)

    ;(await findButton(wrapper, '预览'))?.trigger('click')
    await flushPromises()

    expect(previewCouponMock).toHaveBeenCalledTimes(1)
    // The preview is not a write, so this is still 0.
    expect(createCouponMock).not.toHaveBeenCalled()
    // The operator now sees a review step.
    expect(wrapper.text()).toContain('确认提交')
  })

  it('确认提交 carries the preview_token the server returned', async () => {
    previewCouponMock.mockResolvedValue({ preview_token: 'tok-abc' })
    createCouponMock.mockResolvedValue({ id: '1', code: 'NOVA100' })

    const wrapper = mountView()
    await flushPromises()
    ;(await findButton(wrapper, '新建优惠券'))?.trigger('click')
    await flushPromises()
    await fillForm(wrapper)
    ;(await findButton(wrapper, '预览'))?.trigger('click')
    await flushPromises()
    ;(await findButton(wrapper, '确认提交'))?.trigger('click')
    await flushPromises()

    expect(createCouponMock).toHaveBeenCalledTimes(1)
    const payload = createCouponMock.mock.calls[0]?.[0] as Record<string, unknown>
    // THE GUARANTEE: the write is bound to the approval the operator just saw.
    expect(payload.preview_token).toBe('tok-abc')
    expect(payload.code).toBe('NOVA100')
  })

  it('renders the SERVER preview warnings instead of swallowing them', async () => {
    // A preview that cannot disagree with the form would not be worth a round trip.
    previewCouponMock.mockResolvedValue({
      preview_token: 'tok-2',
      warnings: ['该券码已存在，将被覆盖'],
    })

    const wrapper = mountView()
    await flushPromises()
    ;(await findButton(wrapper, '新建优惠券'))?.trigger('click')
    await flushPromises()
    await fillForm(wrapper)
    ;(await findButton(wrapper, '预览'))?.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('该券码已存在，将被覆盖')
  })

  it('a preview failure surfaces an error and does NOT advance to the confirm step', async () => {
    previewCouponMock.mockRejectedValue({
      code: 90_003,
      message: '需要先预览',
      traceId: 'trace-90003',
      httpStatus: 409,
      retryable: false,
      forbidden: false,
      unauthenticated: false,
    })

    const wrapper = mountView()
    await flushPromises()
    ;(await findButton(wrapper, '新建优惠券'))?.trigger('click')
    await flushPromises()
    await fillForm(wrapper)

    const notifications = useNotificationStore()
    const errorSpy = vi.spyOn(notifications, 'error')

    ;(await findButton(wrapper, '预览'))?.trigger('click')
    await flushPromises()

    expect(errorSpy).toHaveBeenCalledTimes(1)
    // Staying on the form is the point: there is no token, so there is nothing to confirm.
    expect(wrapper.text()).not.toContain('确认提交')
    expect(createCouponMock).not.toHaveBeenCalled()
  })

  it('editing after a preview DISCARDS the approval, so a stale token cannot be reused', async () => {
    previewCouponMock.mockResolvedValue({ preview_token: 'tok-stale' })

    const wrapper = mountView()
    await flushPromises()
    ;(await findButton(wrapper, '新建优惠券'))?.trigger('click')
    await flushPromises()
    await fillForm(wrapper)
    ;(await findButton(wrapper, '预览'))?.trigger('click')
    await flushPromises()

    // Go back and change a value, then return to the review step.
    ;(await findButton(wrapper, '返回修改'))?.trigger('click')
    await flushPromises()
    const inputs = wrapper.findAll('.marketing__fields input')
    await inputs[2]?.setValue('99')

    // The confirm button is gone because the server preview was invalidated with the edit.
    expect(wrapper.text()).not.toContain('确认提交')
    expect(createCouponMock).not.toHaveBeenCalled()
  })
})
