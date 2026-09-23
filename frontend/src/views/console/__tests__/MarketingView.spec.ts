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
const promotionsMock = vi.fn()
const previewPromotionMock = vi.fn()
const createPromotionMock = vi.fn()

vi.mock('@/api', () => ({
  marketingAdminApi: {
    coupons: (...args: unknown[]) => couponsMock(...args),
    promotions: (...args: unknown[]) => promotionsMock(...args),
    previewCoupon: (...args: unknown[]) => previewCouponMock(...args),
    createCoupon: (...args: unknown[]) => createCouponMock(...args),
    previewPromotion: (...args: unknown[]) => previewPromotionMock(...args),
    createPromotion: (...args: unknown[]) => createPromotionMock(...args),
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
  await wrapper.find('.marketing__fields input').setValue('满 1000 减 10')
  const money = wrapper.findAll('.marketing__fields input[inputmode="decimal"]')
  await money.at(0)?.setValue('10')
  await money.at(1)?.setValue('1000')
  const dates = wrapper.findAll('.marketing__fields input[type="datetime-local"]')
  await dates.at(0)?.setValue('2026-10-01T00:00')
  await dates.at(1)?.setValue('2026-10-31T23:59')
}

function couponPreview(token: string, warnings: string[] = []) {
  return {
    preview_token: token,
    estimated_impact: {
      affected_sku_count: 3,
      affected_order_count_30d: 0,
      estimated_issue_count: 0,
      estimated_discount_amount: 0,
    },
    warnings,
  }
}

async function findButton(wrapper: ReturnType<typeof mountView>, label: string) {
  return wrapper.findAll('button').find((button) => button.text().trim() === label)
}

describe('console Marketing — coupon creation is preview then confirm (§47, §12.1)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    couponsMock.mockResolvedValue(emptyPage())
    promotionsMock.mockResolvedValue(emptyPage())
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
    previewCouponMock.mockResolvedValue(couponPreview('tok-1'))

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
    previewCouponMock.mockResolvedValue(couponPreview('tok-abc'))
    createCouponMock.mockResolvedValue({ id: 1, template_no: 'NVC20260924000001' })

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
    expect(payload.coupon_type).toBe('FIXED_AMOUNT')
    expect(payload.face_value_amount).toBe(1000)
    expect(payload.total_quota).toBe(1000)
  })

  it('renders the SERVER preview warnings instead of swallowing them', async () => {
    // A preview that cannot disagree with the form would not be worth a round trip.
    previewCouponMock.mockResolvedValue(couponPreview('tok-2', ['预估未计入活动叠加']))

    const wrapper = mountView()
    await flushPromises()
    ;(await findButton(wrapper, '新建优惠券'))?.trigger('click')
    await flushPromises()
    await fillForm(wrapper)
    ;(await findButton(wrapper, '预览'))?.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('预估未计入活动叠加')
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
    previewCouponMock.mockResolvedValue(couponPreview('tok-stale'))

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
    const money = wrapper.findAll('.marketing__fields input[inputmode="decimal"]')
    await money.at(0)?.setValue('99')

    // The confirm button is gone because the server preview was invalidated with the edit.
    expect(wrapper.text()).not.toContain('确认提交')
    expect(createCouponMock).not.toHaveBeenCalled()
  })
})


describe('console Marketing — promotion creation is preview then confirm (§47, §13.2)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    couponsMock.mockResolvedValue(emptyPage())
    promotionsMock.mockResolvedValue(emptyPage())
  })

  /** Switch to the promotions tab and open the create form. */
  async function openPromoForm(wrapper: ReturnType<typeof mountView>): Promise<void> {
    ;(await findButton(wrapper, '促销活动'))?.trigger('click')
    await flushPromises()
    ;(await findButton(wrapper, '新建促销活动'))?.trigger('click')
    await flushPromises()
  }

  async function fillPromoForm(wrapper: ReturnType<typeof mountView>): Promise<void> {
    await wrapper.find('.marketing__fields input').setValue('秋季焕新')
    const selects = wrapper.findAll('.marketing__fields select')
    await selects.at(0)?.setValue('FULL_REDUCTION')
    const dateInputs = wrapper.findAll('input[type="datetime-local"]')
    await dateInputs.at(0)?.setValue('2026-09-25T00:00')
    await dateInputs.at(1)?.setValue('2026-10-08T23:59')
    // The two FULL_REDUCTION rule fields are the ones rendered for this type.
    const moneyInputs = wrapper.findAll('.marketing__fields input[inputmode="decimal"]')
    await moneyInputs.at(0)?.setValue('3000')
    await moneyInputs.at(1)?.setValue('300')
  }

  it('PREVIEW builds the payload; nothing is written from the form step', async () => {
    previewPromotionMock.mockResolvedValue({
      preview_token: 'pv-1',
      expires_at: '2026-09-23T04:15:00.000Z',
      estimated_impact: {
        affected_sku_count: 3,
        affected_order_count_30d: 142,
        estimated_discount_amount_30d: 4260000,
      },
      conflicts: [],
      warnings: [],
    })

    const wrapper = mountView()
    await flushPromises()
    await openPromoForm(wrapper)
    await fillPromoForm(wrapper)

    expect(createPromotionMock).not.toHaveBeenCalled()

    ;(await findButton(wrapper, '预览'))?.trigger('click')
    await flushPromises()

    expect(previewPromotionMock).toHaveBeenCalledTimes(1)
    expect(createPromotionMock).not.toHaveBeenCalled()
    // The payload must be DISCRIMINATED by promotion_type (§13.1), not a nullable bag.
    const payload = previewPromotionMock.mock.calls[0]?.[0] as Record<string, unknown>
    expect(payload.promotion_type).toBe('FULL_REDUCTION')
    expect(payload.rule_config).toEqual({
      threshold_amount: 300000,
      reduction_amount: 30000,
      max_discount_amount: null,
    })
    // `scope` is EXPLICIT: all_products is stated rather than inferred from empty arrays.
    expect(payload.scope).toMatchObject({ all_products: true })
  })

  it('confirm carries the preview_token, binding the write to what was approved', async () => {
    previewPromotionMock.mockResolvedValue({
      preview_token: 'pv-abc',
      expires_at: '2026-09-23T04:15:00.000Z',
      estimated_impact: {
        affected_sku_count: 1,
        affected_order_count_30d: 2,
        estimated_discount_amount_30d: 100,
      },
      conflicts: [],
      warnings: [],
    })
    createPromotionMock.mockResolvedValue({ id: 12, promotion_no: 'NVP1' })

    const wrapper = mountView()
    await flushPromises()
    await openPromoForm(wrapper)
    await fillPromoForm(wrapper)
    ;(await findButton(wrapper, '预览'))?.trigger('click')
    await flushPromises()
    ;(await findButton(wrapper, '确认提交'))?.trigger('click')
    await flushPromises()

    expect(createPromotionMock).toHaveBeenCalledTimes(1)
    const payload = createPromotionMock.mock.calls[0]?.[0] as Record<string, unknown>
    expect(payload.preview_token).toBe('pv-abc')
  })

  it('renders server CONFLICTS instead of treating them as an error (§13.2)', async () => {
    // A conflict is information to decide with, not a 409 that stops the operator looking.
    previewPromotionMock.mockResolvedValue({
      preview_token: 'pv-2',
      expires_at: '2026-09-23T04:15:00.000Z',
      estimated_impact: {
        affected_sku_count: 1,
        affected_order_count_30d: 1,
        estimated_discount_amount_30d: 1,
      },
      conflicts: [
        { promotion_id: 9, promotion_no: 'NVP2026080100007', reason: 'OVERLAPPING_WINDOW_AND_SCOPE' },
      ],
      warnings: [],
    })

    const wrapper = mountView()
    await flushPromises()
    await openPromoForm(wrapper)
    await fillPromoForm(wrapper)
    ;(await findButton(wrapper, '预览'))?.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('NVP2026080100007')
    expect(wrapper.text()).toContain('OVERLAPPING_WINDOW_AND_SCOPE')
  })
})
